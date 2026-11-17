"""Isolated post-validation installer regression; no network or real services."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/vps/install-runtime-certificate.sh"

@unittest.skipIf(sys.platform == "win32", "the certificate installer runs on Linux")
class CertificateInstallTests(unittest.TestCase):
    def run_case(self, case):
        source = SCRIPT.read_text()
        body = source[source.index("# A matching leaf serial"):]
        body = body.replace("/opt/ai-trader/backups/", "$root/backups/")
        body = body.replace('openssl x509 -in "$live/cert.pem" -noout -serial -enddate', ":")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live, new = root / "live", root / "new"
            live.mkdir(); new.mkdir(); (root / "backups").mkdir()
            for name in ("cert", "chain", "fullchain"):
                version = "new" if case == "current" or (case == "interrupted_retry" and name == "cert") else "old"
                (live / (name + ".pem")).write_text(version + "-" + name)
            (live / "privkey.pem").write_text("synthetic-key")
            for name, value in (("cert.pem", "new-cert"), ("test.crt", "new-fullchain"), ("test.issuer.crt", "new-chain")):
                (new / name).write_text(value)
            pre = r'''#!/bin/bash
set -euo pipefail
root="$1"; mode="$2"; live="$root/live"; tmp="$root/new"; domain=test
new_serial=new; old_serial=old
if [ "$mode" = current ] || [ "$mode" = interrupted_retry ]; then old_serial=new; fi
trap 'rm -rf "$tmp"' EXIT
listener_serial() { if [ "$mode" = listener ]; then echo old; else cat "$live/fullchain.pem" | sed s/new-fullchain/new/; fi; }
nginx() { echo nginx >> "$root/events"; if [ "$mode" = nginx ] && [ ! -f "$root/failed" ]; then touch "$root/failed"; return 1; fi; }
systemctl() { echo reload >> "$root/events"; if [ "$mode" = reload ] && [ ! -f "$root/failed" ]; then touch "$root/failed"; return 1; fi; }
curl() { if [ "$mode" = health ] || [ "$mode" = rollback_fail ]; then return 22; fi; }
sleep() { :; }
install() {
 if [[ "$*" == *new/chain.pem* ]]; then
   case "$mode" in term) kill -TERM $$;; hup) kill -HUP $$;; interrupt) kill -INT $$;; esac
   if [ "$mode" = partial ]; then return 7; fi
 fi
 if [ "$mode" = rollback_fail ] && [[ "$*" == *backups*cert.pem* ]]; then return 73; fi
 command install "$@"
}
'''
            script = root / "run.sh"
            script.write_text(pre + body)
            result = subprocess.run(["bash", str(script), directory, case], capture_output=True, text=True, timeout=10)
            success = case in ("success", "current", "interrupted_retry")
            self.assertEqual(result.returncode == 0, success, result.stderr)
            version = "new" if success or case == "rollback_fail" else "old"
            for name in ("cert", "chain", "fullchain"):
                self.assertEqual((live / (name + ".pem")).read_text(), version + "-" + name)
            self.assertEqual((live / "privkey.pem").read_text(), "synthetic-key")
            self.assertFalse(new.exists())
            if case == "current":
                self.assertFalse((root / "events").exists())
            if case == "rollback_fail":
                self.assertEqual(result.returncode, 22)
                self.assertIn("rollback incomplete", result.stderr)

    def test_install_recovery(self):
        for case in ("success", "current", "interrupted_retry", "partial", "nginx", "reload", "listener", "health", "term", "hup", "interrupt", "rollback_fail"):
            with self.subTest(case=case):
                self.run_case(case)

if __name__ == "__main__":
    unittest.main()
