#!/usr/bin/env python3
"""Fill the shared action archive cache on mac-mini-cc with every action@ref the given repositories'
default-branch workflows use (owner 2026-10-25). The runners there read it through
ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE (each runner's .env) and unpack `<owner>_<repo>/<sha>.tar.gz` instead of
downloading the action from GitHub at job start. A miss only means the runner downloads as before, so run this
after a PR changes an action version. Runs on the workstation (cc has no gh login).

    python3 scripts/refresh_runner_action_cache.py kingcharleslzy-ai/ai-trader kingcharleslzy-ai/biz-data-hub ...
"""
import base64, json, re, subprocess, sys, tempfile
from pathlib import Path

HOST, CACHE = "mac-mini-cc", "/Users/cc/.cache/github-actions-archive"
USES = re.compile(r"uses:\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[^@\s'\"]*)?@([^\s'\"#]+)")


def gh(*args: str) -> bytes:
    return subprocess.run(["gh", "api", *args], check=True, capture_output=True).stdout


def uses(repo: str) -> set[tuple[str, str]]:
    found = set()
    for item in json.loads(gh(f"repos/{repo}/contents/.github/workflows")):
        if item["name"].endswith((".yml", ".yaml")):
            text = base64.b64decode(json.loads(gh(f"repos/{repo}/contents/{item['path']}"))["content"]).decode()
            found |= set(USES.findall(text))
    return found


def main(repos: list[str]) -> None:
    wanted = {(slug, gh(f"repos/{slug}/commits/{ref}", "--jq", ".sha").decode().strip(), ref)
              for repo in repos for slug, ref in uses(repo)}
    have = set(subprocess.run(["ssh", HOST, f"mkdir -p {CACHE} && cd {CACHE} && find . -name '*.tar.gz' -size +0"],
                              check=True, capture_output=True, text=True).stdout.split())
    with tempfile.TemporaryDirectory() as stage:
        added = 0
        for slug, sha, ref in sorted(wanted):
            name = f"{slug.replace('/', '_')}/{sha}.tar.gz"
            if f"./{name}" in have or Path(stage, name).exists():
                continue
            target = Path(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(gh(f"repos/{slug}/tarball/{sha}"))
            print(f"added {slug}@{ref} -> {sha}")
            added += 1
        if added:
            subprocess.run(["rsync", "-a", f"{stage}/", f"{HOST}:{CACHE}/"], check=True)
    print(f"{len(wanted)} action versions in use, {added} added, cache {HOST}:{CACHE}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["kingcharleslzy-ai/ai-trader"])
