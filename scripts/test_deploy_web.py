import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Fictional values that carry every quoting hazard the old argv construction had: single quote,
# double quote, dollar, backslash and backtick. Proving these arrive verbatim on the site host is
# the point; no real Keychain entry is ever read.
FAKE_ID = "fixture-ak-id"
FAKE_SECRET = "fx'secret\"with$x\\back`tick"


def deploy_web_env(tmp_path):
    return {**os.environ, "USER": os.environ.get("USER", "fixture-user"),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "TEST_NPM_LOG": str(tmp_path / "npm.log"),
        "TEST_SSH_CALLS": str(tmp_path / "ssh.calls"),
        "TEST_SSH_STDIN": str(tmp_path / "ssh.stdin"),
        "TEST_LOG": str(tmp_path / "oss.log"),
        "TEST_STUBBIN": str(tmp_path / "ossbin"),
        "TEST_FAKE_ID": FAKE_ID,
        "TEST_FAKE_SECRET": FAKE_SECRET,
        "TEST_EXEC_OSS": "1",
        "TEST_SHA": subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path / "repo",
            check=True, capture_output=True, text=True).stdout.strip()}


def run_deploy_web(tmp_path, sha=None, env_extra=None):
    env = deploy_web_env(tmp_path)
    target = sha or env["TEST_SHA"]
    result = subprocess.run(["sh", "deploy/web/deploy-web.sh", target], cwd=tmp_path / "repo",
        env={**env, **(env_extra or {})}, capture_output=True, text=True, timeout=60, check=False)
    return result, env["TEST_SHA"]


def web_fixture(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy/web").mkdir(parents=True)
    (repo / "scripts").mkdir()
    # Only the site-root constant is rewritten into the fixture; the shared release-source check
    # (the real check_release_source.mjs, run by the real node) and the OSS stdin construction
    # under test are the shipped script verbatim.
    script = (REPO / "deploy/web/deploy-web.sh").read_text().replace("/var/www/ai-trader", str(tmp_path / "www"))
    (repo / "deploy/web/deploy-web.sh").write_text(script)
    shutil.copy(REPO / "deploy/web/.env.production.example", repo / "deploy/web/.env.production.example")
    shutil.copy(REPO / "scripts/check_release_source.mjs", repo / "scripts/check_release_source.mjs")
    # The build output is ignored in the real repo; without this the post-build clean check would
    # always see dist-web as untracked.
    (repo / ".gitignore").write_text("apps/client/dist-web/\n")
    for args in (["git", "init", "-q"], ["git", "config", "user.email", "t@example.test"],
                 ["git", "config", "user.name", "t"], ["git", "add", "-A"], ["git", "commit", "-qm", "fixture"]):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assets = tmp_path / "www" / "releases" / sha / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("console.log(1)")
    (assets / "app.js.gz").write_bytes(b"\x1f\x8bfixture")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in {
        "npm": '''#!/bin/sh
{ printf 'npm %s\\n' "$*"; printf 'AI_TRADER_BUILD_SHA=%s\\n' "${AI_TRADER_BUILD_SHA:-}"; } >> "$TEST_NPM_LOG"
case "$*" in
  *build:web*)
    mkdir -p apps/client/dist-web
    printf '<!doctype html><html><head><script type="module" crossorigin src="https://ai-trader-updates-hz.oss-cn-hangzhou.aliyuncs.com/web/assets/x.js"></script></head><body></body></html>\\n' > apps/client/dist-web/index.html
    # A concurrent edit landing while the build runs: the pre-build check passed, the post-build
    # one must catch it before anything is uploaded.
    [ -z "${TEST_DIRTY_DURING_BUILD:-}" ] || printf '# mid-build edit\\n' >> deploy/web/.env.production.example
    ;;
esac
exit 0
''',
        "security": '''#!/bin/sh
printf '%s:%s\\n' "$TEST_FAKE_ID" "$TEST_FAKE_SECRET"
exit 0
''',
        # Records every ssh argv; executes only the OSS upload command remotely (with the stub
        # aliyun on PATH and the ssh stdin forwarded) to prove the read/export/quoting works.
        "ssh": '''#!/bin/sh
printf 'ssh-argv: %s\\n' "$*" >> "$TEST_SSH_CALLS"
if [ "${TEST_EXEC_OSS:-}" = 1 ]; then
  case "$2" in
    *"read -r oss_key_id"*)
      tee "$TEST_SSH_STDIN" | PATH="$TEST_STUBBIN:$PATH" sh -c "$2"
      exit
      ;;
  esac
fi
exit 0
''',
        "curl": '''#!/bin/sh
case "$*" in
  *version.json*) printf '{"version":"1.6.11","commit":"%s","liveTrading":"disabled"}\\n' "$TEST_SHA" ;;
  *) printf 'HTTP/1.1 200 OK\\naccess-control-allow-origin: https://qilinshuzhi.com\\n' ;;
esac
exit 0
''',
    }.items():
        path = stubs / name
        path.write_text(body)
        path.chmod(0o755)
    ossbin = tmp_path / "ossbin"
    ossbin.mkdir()
    aliyun = ossbin / "aliyun"
    aliyun.write_text('''#!/bin/sh
printf 'aliyun-argv: %s\\n' "$*" >> "$TEST_LOG"
env | grep '^ALIBABA_CLOUD_' | sed 's/^/aliyun-env: /' >> "$TEST_LOG"
exit 0
''')
    aliyun.chmod(0o755)
    return repo, sha, stubs, ossbin


def test_deploy_web_clean_tree_full_flow_keeps_secrets_out_of_argv(tmp_path):
    web_fixture(tmp_path)
    result, sha = run_deploy_web(tmp_path)
    assert result.returncode == 0, result.stderr
    ssh_calls = (tmp_path / "ssh.calls").read_text()
    # The credentials never appear in any ssh command line (visible to ps on both machines, #715).
    assert FAKE_SECRET not in ssh_calls and FAKE_ID not in ssh_calls
    # They travel verbatim on stdin: every quoting hazard of the fictional key survives.
    assert (tmp_path / "ssh.stdin").read_text() == f"{FAKE_ID}\n{FAKE_SECRET}\n"
    oss_log = (tmp_path / "oss.log").read_text()
    assert f"aliyun-env: ALIBABA_CLOUD_ACCESS_KEY_ID={FAKE_ID}" in oss_log
    assert f"aliyun-env: ALIBABA_CLOUD_ACCESS_KEY_SECRET={FAKE_SECRET}" in oss_log
    assert "aliyun-env: ALIBABA_CLOUD_REGION_ID=cn-hangzhou" in oss_log
    # Upload semantics unchanged: the precompressed .gz goes up under the .js name with immutable
    # caching, explicit content type and Content-Encoding: gzip.
    assert any("cp app.js.gz oss://ai-trader-updates-hz/web/assets/app.js" in line for line in oss_log.splitlines())
    assert any("Cache-Control:public,max-age=31536000,immutable#Content-Type:application/javascript; charset=utf-8#Content-Encoding:gzip" in line
        for line in oss_log.splitlines())
    npm_log = (tmp_path / "npm.log").read_text()
    assert f"AI_TRADER_BUILD_SHA={sha}" in npm_log and "build:web" in npm_log
    assert any("mv -Tf" in line and sha in line for line in ssh_calls.splitlines())


def test_deploy_web_rejects_dirty_tracked_tree_before_building(tmp_path):
    web_fixture(tmp_path)
    (tmp_path / "repo/deploy/web/.env.production.example").write_text("VITE_APP_BASE_PATH=/trader/\n")
    result, _ = run_deploy_web(tmp_path)
    assert result.returncode == 2
    assert "release_source_dirty" in result.stderr
    assert not (tmp_path / "npm.log").exists()


def test_deploy_web_rejects_untracked_files_before_building(tmp_path):
    web_fixture(tmp_path)
    (tmp_path / "repo/stray-file.js").write_text("// must not ship as the stamped commit\n")
    result, _ = run_deploy_web(tmp_path)
    assert result.returncode == 2
    assert "release_source_dirty" in result.stderr
    assert not (tmp_path / "npm.log").exists()


def test_deploy_web_rejects_head_mismatch(tmp_path):
    web_fixture(tmp_path)
    result, _ = run_deploy_web(tmp_path, "c" * 40)
    assert result.returncode == 2
    assert "the checked-out tree is not" in result.stderr
    assert not (tmp_path / "npm.log").exists()


def test_deploy_web_rejects_mid_build_modification_before_upload(tmp_path):
    # A clean pre-build check is not enough: an edit landing while the build runs must stop the
    # release at the post-build re-check, before any upload or the site switch.
    web_fixture(tmp_path)
    result, _ = run_deploy_web(tmp_path, env_extra={"TEST_DIRTY_DURING_BUILD": "1"})
    assert result.returncode == 2, result.stderr
    assert "release_source_dirty" in result.stderr
    assert (tmp_path / "npm.log").exists(), "the build itself must have run"
    assert not (tmp_path / "oss.log").exists()
    ssh_calls = (tmp_path / "ssh.calls").read_text() if (tmp_path / "ssh.calls").exists() else ""
    assert "mv -Tf" not in ssh_calls and "aliyun" not in ssh_calls
