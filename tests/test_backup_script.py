"""Contract tests for deploy/hermes-backup.sh and the unit files, using fake systemctl/setpriv/curl/python."""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKE = {
    "systemctl": """#!/bin/sh
echo "systemctl $*" >> "$LOG"
case "$1" in
  is-active) [ -f "$STATE/up" ] ;;
  stop) [ -z "${FAIL_STOP:-}" ] || exit 1; rm -f "$STATE/up" ;;
  start) [ -z "${FAIL_START:-}" ] || exit 1; touch "$STATE/up" ;;
esac
""",
    "setpriv": '#!/bin/sh\nwhile [ "${1#--}" != "$1" ]; do shift; done; exec "$@"\n',
    "curl": '#!/bin/sh\n[ -z "${FAIL_READY:-}" ]\n',
    "python": """#!/bin/sh
# argv: -m app.ops CMD FILE
echo "ops $3" >> "$LOG"
[ ! -f "$STATE/up" ] || { echo "ops ran while hermes was up" >> "$LOG"; exit 9; }
case "$3" in
  backup) [ -z "${FAIL_BACKUP:-}" ] || exit 1; echo data > "$4" ;;
  verify-backup) [ -z "${FAIL_VERIFY:-}" ] || exit 1 ;;
esac
""",
}


@pytest.fixture
def env(tmp_path):
    bin_, dest, app = tmp_path / "bin", tmp_path / "dest", tmp_path / "app"
    for d in (bin_, dest, app / ".venv/bin", tmp_path / "state", tmp_path / "creds"):
        d.mkdir(parents=True)
    for name, body in FAKE.items():
        p = (app / ".venv/bin" if name == "python" else bin_) / name
        p.write_text(body)
        p.chmod(0o755)
    (tmp_path / "creds/backup-passphrase").write_text("correct horse battery")
    (tmp_path / "state/up").touch()
    return {
        **os.environ,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "LOG": str(tmp_path / "log"),
        "STATE": str(tmp_path / "state"),
        "CREDENTIALS_DIRECTORY": str(tmp_path / "creds"),
        "BACKUP_DIR": str(dest),
        "HERMES_DIR": str(app),
        "READY_TRIES": "2",
        "READY_SLEEP": "0",
    }


def run(env, **extra):
    r = subprocess.run(  # noqa: S603, S607
        ["sh", str(ROOT / "deploy/hermes-backup.sh")], env={**env, **extra}, capture_output=True, text=True
    )
    log = Path(env["LOG"]).read_text().splitlines() if Path(env["LOG"]).exists() else []
    return r, log, Path(env["STATE"], "up").exists(), sorted(Path(env["BACKUP_DIR"]).glob("*.hbk"))


def test_success_stops_backs_up_verifies_restarts(env):
    r, log, up, files = run(env)
    assert r.returncode == 0, r.stderr
    assert log.index("systemctl stop hermes") < log.index("ops backup") < log.index("ops verify-backup")
    assert log.index("ops verify-backup") < log.index("systemctl start hermes")
    assert up and len(files) == 1


@pytest.mark.parametrize("fail", ["FAIL_BACKUP", "FAIL_VERIFY"])
def test_failed_backup_still_restarts_and_leaves_no_artifact(env, fail):
    r, log, up, files = run(env, **{fail: "1"})
    assert r.returncode != 0
    assert up and "systemctl start hermes" in log and files == []


def test_failed_verify_does_not_prune_good_backups(env):
    old = [Path(env["BACKUP_DIR"], f"2020-01-0{i}T000000Z.hbk") for i in range(1, 4)]
    for p in old:
        p.write_text("x")
    r, _, _, files = run(env, FAIL_VERIFY="1", BACKUP_KEEP="1")
    assert r.returncode != 0 and files == old


def test_retention_keeps_newest(env):
    for i in range(1, 4):
        Path(env["BACKUP_DIR"], f"2020-01-0{i}T000000Z.hbk").write_text("x")
    r, _, _, files = run(env, BACKUP_KEEP="2")
    assert r.returncode == 0 and len(files) == 2 and files[0].name.startswith("2020-01-03")


def test_failed_stop_takes_no_backup_and_fails(env):
    r, log, _, files = run(env, FAIL_STOP="1")
    assert r.returncode != 0 and "ops backup" not in log and files == []


def test_restart_failure_is_visible(env):
    r, _, up, files = run(env, FAIL_START="1")
    assert r.returncode != 0 and "could not start" in r.stderr and not up and len(files) == 1


def test_readiness_failure_is_visible(env):
    r, _, _, _ = run(env, FAIL_READY="1")
    assert r.returncode != 0 and "not ready" in r.stderr


def test_not_started_if_it_was_already_stopped(env):
    Path(env["STATE"], "up").unlink()
    r, log, up, files = run(env)
    assert r.returncode == 0 and not up and "systemctl start hermes" not in log and len(files) == 1


def test_unit_contract():
    svc = (ROOT / "deploy/hermes-backup.service").read_text()
    app = (ROOT / "deploy/hermes.service").read_text()
    # Root-run orchestrator: only user that may stop/start units; data access drops to hermes in the script.
    assert re.search(r"^User=root$", svc, re.M)
    assert not re.search(r"^User=hermes", svc, re.M), "hermes cannot systemctl stop; nightly run would hit the lock"
    assert re.search(r"^CapabilityBoundingSet=CAP_SETUID CAP_SETGID$", svc, re.M)
    assert "AF_UNIX" in svc and "ExecStart=/opt/hermes/deploy/hermes-backup.sh" in svc
    assert "ReadWritePaths=" in svc and "/var/lib/hermes" in svc  # data-root .lock must be creatable
    assert "TimeoutStartSec=" in svc
    # Credential isolation: passphrase only via the backup unit's credential.
    assert "backup-passphrase" not in app and "LoadCredential" not in app
    sh = (ROOT / "deploy/hermes-backup.sh").read_text()
    assert "trap on_exit EXIT" in sh and "verify-backup" in sh and "umask 0077" in sh
    assert "systemctl stop hermes" in sh and "readyz" in sh
    timer = (ROOT / "deploy/hermes-backup.timer").read_text()
    assert "Persistent=true" in timer and "OnCalendar=" in timer
