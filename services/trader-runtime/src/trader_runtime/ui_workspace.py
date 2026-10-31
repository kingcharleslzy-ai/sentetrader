"""Stable user workspace identity and text-only design jobs, independent of panels."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from .store import utc_now
from .strategy_design_errors import design_failure
from .strategy_designer import design_plan

LOGGER = logging.getLogger(__name__)


class UiWorkspace:
    def __init__(self, root: Path):
        self.root = Path(root) / "ui-workspace"
        self.root.mkdir(parents=True, exist_ok=True)
        identity = self.root / "identity"
        if not identity.exists():
            try:
                with identity.open("x") as file:
                    file.write(uuid4().hex)
                identity.chmod(0o600)
            except FileExistsError:
                pass
        self.identity = identity.read_text()
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.jobs: dict[tuple[str, str], dict] = {}

    def scope(self, user: str) -> str:
        return hashlib.sha256(f"{self.identity}:{user}".encode()).hexdigest()

    def path(self, user: str, identifier: str) -> Path:
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9-]{32,36}", identifier):
            raise HTTPException(404, "design_job_not_found")
        return self.root / f"{self.scope(user)}-{identifier}.json"

    def persist(self, user: str, job: dict):
        path = self.path(user, job["id"])
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("x") as file:
                temporary.chmod(0o600)
                json.dump(job, file, ensure_ascii=False)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, user: str, identifier: str) -> dict:
        key = (user, identifier)
        if key in self.jobs:
            return self.jobs[key]
        try:
            job = json.loads(self.path(user, identifier).read_text())
        except FileNotFoundError:
            raise HTTPException(404, "design_job_not_found") from None
        if job["status"] == "running":
            job.update(status="cancelled", stage="应用服务已重启，生成已中断", finishedAt=utc_now())
            self.persist(user, job)
        return job

    def start(self, user: str, body: dict, adapter) -> dict:
        if not isinstance(body, dict):
            raise HTTPException(422, "invalid_design_request")
        identifier = body.get("id") or uuid4().hex
        self.path(user, identifier)
        description = body.get("description")
        constraints = body.get("constraints", {})
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 20000 or not isinstance(constraints, dict):
            raise HTTPException(422, "请填写策略描述，最多 20000 字")
        round_number = constraints.get("clarificationRound", 0)
        answers = constraints.get("clarificationAnswers", [])
        if (not isinstance(round_number, int) or not 0 <= round_number <= 100 or
            not isinstance(answers, list) or len(answers) > 100 or any(
                not isinstance(a, dict) or not isinstance(a.get("question"), str) or
                not isinstance(a.get("answer"), str) or not 1 <= len(a["question"]) <= 500 or
                not 1 <= len(a["answer"].strip()) <= 2000 for a in answers)):
            raise HTTPException(422, "补充问题或答案格式无效")
        try:
            existing = self.get(user, identifier)
        except HTTPException as error:
            if error.status_code != 404:
                raise
        else:
            if existing["description"] != description or existing["constraints"] != constraints:
                raise HTTPException(409, "design_request_changed")
            return existing
        if any(owner == user for owner, _ in self.tasks):
            raise HTTPException(409, "已有策略正在生成，请等待或停止后再试")
        job = dict(id=identifier, description=description, constraints=constraints,
                   status="running", stage="等待模型响应", summary="", startedAt=utc_now())
        self.persist(user, job)
        key = (user, identifier)
        self.jobs[key] = job

        def progress(**changes):
            changed = changes.get("stage") != job.get("stage")
            job.update(changes, lastProgressAt=utc_now())
            if changed:
                self.persist(user, job)

        async def run():
            try:
                result = await design_plan(adapter, description, constraints, user_id=user,
                                          on_progress=progress)
                job.update(status="completed", stage="策略已生成，草稿已保留", result=result)
            except asyncio.CancelledError:
                job.update(status="cancelled", stage="已停止生成")
            except Exception as error:
                failure = design_failure(error, job.get("stage", "等待模型响应"))
                job.update(status="failed", stage=failure["message"], error=failure)
                LOGGER.warning("strategy_design_failed job=%s phase=%s code=%s exception=%s rpc=%s",
                               identifier, failure["phase"], failure["code"],
                               failure["exceptionType"], failure.get("rpcCode"))
            finally:
                job["finishedAt"] = utc_now()
                self.persist(user, job)
                self.tasks.pop(key, None)
                self.jobs.pop(key, None)

        self.tasks[key] = asyncio.create_task(run())
        return job

    async def cancel(self, user: str, identifier: str) -> dict:
        job = self.get(user, identifier)
        task = self.tasks.get((user, identifier))
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if job["status"] == "running":
                job.update(status="cancelled", stage="已停止生成", finishedAt=utc_now())
                self.persist(user, job)
            self.tasks.pop((user, identifier), None)
            self.jobs.pop((user, identifier), None)
        return self.get(user, identifier) if task else job

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
