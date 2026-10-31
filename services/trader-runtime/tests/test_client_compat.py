"""Old desktop-client compatibility for the Runtime REST contract.

The cloud Runtime keeps taking hotfixes while installed desktop clients stay on the
release whose snapshot lives in contracts/openapi-baseline.json. These tests hold the
four wire rules from contracts/README.md so a hotfix never ships an endpoint an
already-installed client can no longer talk to:

R1 no removed path+method      R2 no new required request field, no rejected request field
R3 no removed response field and no response type change      R4 no removed enum value
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from trader_runtime.main import create_app

BASELINE = Path(__file__).resolve().parents[1] / "contracts" / "openapi-baseline.json"
HTTP_METHODS = ("get", "put", "post", "delete", "patch")
COMPOSITE_KEYS = ("anyOf", "oneOf")


class ContractDocument:
    """Resolves ``#/components/schemas/...`` references inside one OpenAPI document."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})

    def resolve(self, node: Any) -> dict[str, Any] | None:
        """Follow a ``$ref`` chain; foreign or self-referencing chains resolve to None."""
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            name = ref.rsplit("/", 1)[-1]
            if not ref.startswith("#/components/schemas/") or name in seen:
                return None
            seen.add(name)
            node = self.schemas.get(name)
        return node if isinstance(node, dict) else None


def _request_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    schema = body.get("content", {}).get("application/json", {}).get("schema")
    return schema if isinstance(schema, dict) else None


def _success_schema(operation: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The (status, schema) of the operation's 200 (preferred) or 201 JSON response."""
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    for status in ("200", "201"):
        node = responses.get(status)
        if isinstance(node, dict):
            schema = node.get("content", {}).get("application/json", {}).get("schema")
            if isinstance(schema, dict):
                return status, schema
    return None


def _branch_match(plain: dict[str, Any], branches: list[Any]) -> int | None:
    """Index of the union branch that actually corresponds to the plain schema."""
    best: tuple[int, int, int] | None = None
    for index, branch in enumerate(branches):
        if not isinstance(branch, dict):
            continue
        type_match = 1 if branch.get("type") == plain.get("type") else 0
        overlap = len(set(branch.get("properties", {})) & set(plain.get("properties", {})))
        if not type_match and not overlap:
            continue
        score = (type_match, overlap, -index)
        if best is None or score > best:
            best = score
    return best[-1] if best is not None else None


def compare_schema(
    base: ContractDocument,
    current: ContractDocument,
    baseline_node: Any,
    current_node: Any,
    context: str,
    where: str,
    label: str,
    violations: list[str],
    blind_spots: list[str],
    visited: set[tuple[int, int]],
) -> None:
    """Compare one baseline schema node against its current counterpart.

    ``context`` is "request" (R2/R4) or "response" (R3/R4); ``where``/``label`` only
    build the report line ``<METHOD> <path> <where> <rule> 基线值→当前值``.
    """
    baseline = base.resolve(baseline_node)
    latest = current.resolve(current_node)
    if baseline is None or latest is None:
        return
    key = (id(baseline), id(latest))
    if key in visited:  # self-referencing schemas: one pass per object pair
        return
    visited.add(key)

    base_union = next((k for k in COMPOSITE_KEYS if isinstance(baseline.get(k), list)), None)
    latest_union = next((k for k in COMPOSITE_KEYS if isinstance(latest.get(k), list)), None)

    if base_union and latest_union:
        pairs = min(len(baseline[base_union]), len(latest[latest_union]))
        for index in range(pairs):
            compare_schema(base, current, baseline[base_union][index], latest[latest_union][index],
                           context, f"{where}.{base_union}[{index}]", label, violations, blind_spots, visited)
        if len(baseline[base_union]) != len(latest[latest_union]):
            blind_spots.append(f"{label} {where} 联合分支数 基线{len(baseline[base_union])}→当前{len(latest[latest_union])}（盲区）")
        return

    if base_union or latest_union:
        union = base_union or latest_union
        branches = baseline[base_union] if base_union else latest[latest_union]
        plain_is_baseline = base_union is not None
        plain = baseline if plain_is_baseline else latest
        index = _branch_match(plain, branches)
        if index is None:
            blind_spots.append(f"{label} {where} {union} 无可对应分支（盲区）")
            return
        for position in range(len(branches)):
            if position != index:
                blind_spots.append(f"{label} {where} {union}[{position}] 未配对（盲区）")
        pair = (branches[index], plain) if plain_is_baseline else (plain, branches[index])
        compare_schema(base, current, pair[0], pair[1], context, f"{where}.{union}[{index}]",
                       label, violations, blind_spots, visited)
        return

    baseline_enum, latest_enum = baseline.get("enum"), latest.get("enum")
    if isinstance(baseline_enum, list) and isinstance(latest_enum, list):
        removed = [value for value in baseline_enum if value not in latest_enum]
        if removed:
            kind = "请求" if context == "request" else "响应"
            violations.append(f"{label} {where} R4 {kind}枚举删值 基线{baseline_enum}→当前{latest_enum}")

    if context == "request":
        base_required = {name for name in baseline.get("required", []) if isinstance(name, str)}
        latest_required = {name for name in latest.get("required", []) if isinstance(name, str)}
        added = sorted(latest_required - base_required)
        if added:
            violations.append(
                f"{label} {where} R2 请求新增必填字段 {added} "
                f"基线required={sorted(base_required)}→当前required={sorted(latest_required)}")

    if (context == "response" and isinstance(baseline.get("type"), str) and isinstance(latest.get("type"), str)
            and baseline["type"] != latest["type"]):
        violations.append(f"{label} {where} R3 响应类型改变 基线{baseline['type']}→当前{latest['type']}")

    base_properties = baseline.get("properties", {})
    latest_properties = latest.get("properties", {})
    if (isinstance(base_properties, dict) and base_properties
            and not latest_properties
            and latest.get("additionalProperties") is not False):
        # Typed object -> free object: no field can be proven removed and a free object
        # accepts every baseline request field, so this is a loosened schema promise,
        # not a client break (first case: 05d996a UiState.highlights/chartAnnotations).
        blind_spots.append(
            f"{label} {where} R3-loosened 类型化对象变自由对象"
            f"（{len(base_properties)} 个基线字段不再有 schema 承诺，运行时载荷未受影响；见 README）")
        return
    if isinstance(base_properties, dict) and isinstance(latest_properties, dict):
        for name in sorted(base_properties):
            if name not in latest_properties:
                if context == "response":
                    violations.append(f"{label} {where}.properties.{name} R3 响应属性缺失 基线有→当前无")
                elif latest.get("additionalProperties") is False:
                    violations.append(
                        f"{label} {where}.properties.{name} R2 请求属性被拒（additionalProperties=false） 基线有→当前无")
                continue
            compare_schema(base, current, base_properties[name], latest_properties[name], context,
                           f"{where}.properties.{name}", label, violations, blind_spots, visited)

    if isinstance(baseline.get("items"), dict) and isinstance(latest.get("items"), dict):
        compare_schema(base, current, baseline["items"], latest["items"], context,
                       f"{where}.items[]", label, violations, blind_spots, visited)


def compare_contracts(baseline_spec: dict[str, Any], current_spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Every violation of R1–R4, plus the union blind spots that are only reported."""
    base, current = ContractDocument(baseline_spec), ContractDocument(current_spec)
    violations: list[str] = []
    blind_spots: list[str] = []
    for path, path_item in sorted(baseline_spec.get("paths", {}).items()):
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            label = f"{method.upper()} {path}"
            latest_item = current_spec.get("paths", {}).get(path)
            latest_operation = latest_item.get(method) if isinstance(latest_item, dict) else None
            if not isinstance(latest_operation, dict):
                violations.append(f"{label} - R1 路径/方法缺失 基线有→当前无")
                continue
            visited: set[tuple[int, int]] = set()
            request_schema = _request_schema(operation)
            if request_schema is not None:
                compare_schema(base, current, request_schema, _request_schema(latest_operation),
                               "request", "请求体", label, violations, blind_spots, visited)
            success = _success_schema(operation)
            latest_success = _success_schema(latest_operation)
            if success is not None:
                if latest_success is None or latest_success[0] != success[0]:
                    violations.append(f"{label} {success[0]} R3 响应schema缺失 基线有→当前无")
                else:
                    compare_schema(base, current, success[1], latest_success[1], "response",
                                   f"{success[0]}响应", label, violations, blind_spots, visited)
    return violations, blind_spots


@pytest.fixture(scope="module")
def current_openapi(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("client-compat")
    app = create_app(root / "compat.sqlite3", local_token="compat-baseline",
                     agent_mode="fake", data_mode="fake", engineering_root=root / "project")
    return app.openapi()


def test_baseline_records_its_source_release() -> None:
    provenance = json.loads(BASELINE.read_text(encoding="utf-8"))["info"]["x-ai-trader"]
    assert provenance["version"], "baseline must record the release version it was exported from"
    assert len(provenance["commit"]) == 40, "baseline must record the full source commit"


def test_runtime_rest_contract_keeps_old_client_compatibility(current_openapi: dict[str, Any]) -> None:
    baseline_spec = json.loads(BASELINE.read_text(encoding="utf-8"))
    provenance = baseline_spec["info"].get("x-ai-trader", {})
    violations, blind_spots = compare_contracts(baseline_spec, current_openapi)
    for line in blind_spots:
        print(f"[compat-盲区] {line}")
    if blind_spots:
        print(f"[compat-盲区] 共 {len(blind_spots)} 处按尽力比较处理，不判失败（见 contracts/README.md）")
    if violations:
        pytest.fail(
            "旧客户端兼容规则被破坏（基线 "
            f"{provenance.get('version')} @ {str(provenance.get('commit'))[:7]}）：\n"
            + "\n".join(f"- {line}" for line in violations))


def _field_shape(node: Any) -> Any:
    """A schema's type structure: titles, descriptions and defaults dropped; enum values are R4's job.
    Bounds are not type: a wider range (dockWidth 520 → 1600 for the half-window 策略面板) keeps the shape."""
    if isinstance(node, dict):
        return {key: _field_shape(value) for key, value in node.items()
                if key not in {"title", "description", "default", "examples", "enum", "maxLength", "minLength",
                               "maximum", "minimum"}}
    if isinstance(node, list):
        return [_field_shape(item) for item in node]
    return node


def uistate_type_changes(baseline_spec: dict[str, Any], current_spec: dict[str, Any]) -> list[str]:
    """The observation snapshot (UiState) is read by the embedded AI and external agents: a field the
    baseline reports keeps its type (a new meaning gets a new field, e.g. strategySummary)."""
    base = baseline_spec["components"]["schemas"]["UiState"]["properties"]
    current = current_spec["components"]["schemas"]["UiState"]["properties"]
    return [f"UiState.{name} 类型改变或缺失" for name, schema in sorted(base.items())
            if name not in current or _field_shape(schema) != _field_shape(current[name])]


def test_snapshot_fields_keep_their_types(current_openapi: dict[str, Any]) -> None:
    baseline_spec = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert uistate_type_changes(baseline_spec, current_openapi) == []
    # strategyDetails stays {section: bool}; 详情's one sentence rides in its own field.
    details = current_openapi["components"]["schemas"]["UiState"]["properties"]["strategyDetails"]
    assert {"additionalProperties": {"type": "boolean"}, "type": "object"} in details["anyOf"]
    assert "strategySummary" in current_openapi["components"]["schemas"]["UiState"]["properties"]
    # The check fires on a loosened field.
    loosened = copy.deepcopy(current_openapi)
    loosened["components"]["schemas"]["UiState"]["properties"]["strategyDetails"] = {
        "anyOf": [{"additionalProperties": {"anyOf": [{"type": "boolean"}, {"type": "string"}]}, "type": "object"},
                  {"type": "null"}]}
    assert uistate_type_changes(baseline_spec, loosened) == ["UiState.strategyDetails 类型改变或缺失"]


# --- Self-tests: each rule fires on a hand-mutated copy of the current contract. ---


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, path_item in sorted(spec.get("paths", {}).items()):
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict):
                yield path, method, operation


def _walk(node: Any, document: ContractDocument, trail: str, walked: set[int]) -> Iterator[tuple[dict[str, Any], str]]:
    resolved = document.resolve(node)
    if resolved is None or id(resolved) in walked:
        return
    walked.add(id(resolved))
    yield resolved, trail
    properties = resolved.get("properties", {})
    if isinstance(properties, dict):
        for name in sorted(properties):
            yield from _walk(properties[name], document, f"{trail}.properties.{name}", walked)
    if isinstance(resolved.get("items"), dict):
        yield from _walk(resolved["items"], document, f"{trail}.items[]", walked)
    for key in COMPOSITE_KEYS:
        branches = resolved.get(key)
        if isinstance(branches, list):
            for index, branch in enumerate(branches):
                yield from _walk(branch, document, f"{trail}.{key}[{index}]", walked)


def _find_node(spec: dict[str, Any], contexts: set[str],
               predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    """First schema node reachable from any operation's request body or success response
    (per ``contexts``) whose resolved form satisfies ``predicate``."""
    document = ContractDocument(spec)
    for path, method, operation in _operations(spec):
        entries: list[dict[str, Any]] = []
        if "request" in contexts:
            request_schema = _request_schema(operation)
            if request_schema is not None:
                entries.append(request_schema)
        if "response" in contexts:
            success = _success_schema(operation)
            if success is not None:
                entries.append(success[1])
        for entry in entries:
            for resolved, _trail in _walk(entry, document, "", set()):
                if predicate(resolved):
                    return resolved
    return None


def _spec_pair(current_openapi: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return copy.deepcopy(current_openapi), copy.deepcopy(current_openapi)


def test_rule1_catches_a_removed_path(current_openapi: dict[str, Any]) -> None:
    baseline_spec, latest_spec = _spec_pair(current_openapi)
    removed = sorted(baseline_spec["paths"])[0]
    del latest_spec["paths"][removed]
    violations, _ = compare_contracts(baseline_spec, latest_spec)
    assert any("R1 路径/方法缺失" in line and removed in line for line in violations)


def test_rule2_catches_a_new_required_request_field(current_openapi: dict[str, Any]) -> None:
    baseline_spec, latest_spec = _spec_pair(current_openapi)
    node = _find_node(latest_spec, {"request"}, lambda candidate: candidate.get("type") == "object")
    assert node is not None, "no object request schema to mutate"
    node["required"] = sorted(set(node.get("required", [])) | {"compatSelfTestField"})
    violations, _ = compare_contracts(baseline_spec, latest_spec)
    assert any("R2 请求新增必填字段" in line for line in violations)


def test_rule3_catches_a_removed_response_property(current_openapi: dict[str, Any]) -> None:
    baseline_spec, latest_spec = _spec_pair(current_openapi)
    # Removing the only property of a typed object is the R3-loosened blind spot,
    # not a violation — pick a node where a removal stays detectable.
    node = _find_node(latest_spec, {"response"},
                      lambda candidate: isinstance(candidate.get("properties"), dict)
                      and len(candidate["properties"]) > 1)
    assert node is not None, "no response object schema with properties to mutate"
    removed = sorted(node["properties"])[0]
    del node["properties"][removed]
    violations, _ = compare_contracts(baseline_spec, latest_spec)
    assert any("R3 响应属性缺失" in line and removed in line for line in violations)


def test_rule3_allows_a_typed_object_becoming_a_free_object(current_openapi: dict[str, Any]) -> None:
    baseline_spec, latest_spec = _spec_pair(current_openapi)
    node = _find_node(latest_spec, {"response"},
                      lambda candidate: isinstance(candidate.get("properties"), dict) and candidate["properties"]
                      and not any(key in candidate for key in COMPOSITE_KEYS))
    assert node is not None, "no plain response object schema with properties to mutate"
    node.clear()
    node["type"] = "object"
    violations, blind_spots = compare_contracts(baseline_spec, latest_spec)
    assert violations == []
    assert any("R3-loosened" in line and "类型化对象变自由对象" in line for line in blind_spots)


def test_rule4_catches_a_removed_enum_value(current_openapi: dict[str, Any]) -> None:
    baseline_spec, latest_spec = _spec_pair(current_openapi)
    node = _find_node(latest_spec, {"request", "response"},
                      lambda candidate: isinstance(candidate.get("enum"), list) and candidate["enum"])
    assert node is not None, "no enum schema to mutate"
    removed = node["enum"][0]
    node["enum"] = node["enum"][1:]
    violations, _ = compare_contracts(baseline_spec, latest_spec)
    assert any("R4" in line and "枚举删值" in line and removed in line for line in violations)
