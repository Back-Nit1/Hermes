"""Contract tests for deploy/hermes-restore.sh using fake systemctl/setpriv/curl/python (no real restore or systemctl)."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SECRET = "correct horse battery staple"
FAKE = {
    "systemctl": """#!/bin/sh
echo "systemctl $*" >> "$LOG"
case "$1" in
  is-active) [ -f "$STATE/up" ] ;;
  stop) rm -f "$STATE/up" ;;
  start) [ -z "${FAIL_START:-}" ] || exit 1; touch "$STATE/up" ;;
esac
""",
    "setpriv": '#!/bin/sh\nwhile [ "${1#--}" != "$1" ]; do shift; done; exec "$@"\n',
    "curl": '#!/bin/sh\n[ -z "${FAIL_READY:-}" ]\n',
    "python": """#!/bin/sh
# argv: -m app.ops restore FILE
echo "ops $*" >> "$LOG"
[ ! -f "$STATE/up" ] || { echo "ops ran while hermes was up" >> "$LOG"; exit 9; }
[ "$HERMES_BACKUP_PASSPHRASE" = "$EXPECT_PASS" ] || exit 8
[ -z "${TERM_SELF:-}" ] || kill -TERM "$PPID"
exit "${RESTORE_RC:-0}"
""",
}


@pytest.fixture
def env(tmp_path):
    bin_, app, state = tmp_path / "bin", tmp_path / "app", tmp_path / "state"
    for d in (bin_, app / ".venv/bin", state):
        d.mkdir(parents=True)
    for name, body in FAKE.items():
        p = (app / ".venv/bin" if name == "python" else bin_) / name
        p.write_text(body)
        p.chmod(0o755)
    (tmp_path / "pass").write_text(SECRET + "\n")
    (tmp_path / "hermes.env").write_text("SOME_SETTING=1\n")
    (tmp_path / "x.hbk").write_text("backup")
    (state / "up").touch()
    e = {k: v for k, v in os.environ.items() if k != "HERMES_BACKUP_PASSPHRASE"}
    return {
        **e,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "LOG": str(tmp_path / "log"),
        "STATE": str(state),
        "EXPECT_PASS": SECRET,
        "HERMES_DIR": str(app),
        "HERMES_PASSPHRASE_FILE": str(tmp_path / "pass"),
        "HERMES_ENV_FILE": str(tmp_path / "hermes.env"),
        "READY_TRIES": "2",
        "READY_SLEEP": "0",
        "BACKUP": str(tmp_path / "x.hbk"),
    }


def run(env, **extra):
    e = {**env, **extra}
    r = subprocess.run(
        ["sh", str(ROOT / "deploy/hermes-restore.sh"), e["BACKUP"]], env=e, capture_output=True, text=True
    )  # noqa: S603, S607
    log = Path(env["LOG"]).read_text().splitlines() if Path(env["LOG"]).exists() else []
    return r, log, Path(env["STATE"], "up").exists()


def test_success_stops_restores_restarts(env):
    r, log, up = run(env)
    assert r.returncode == 0, r.stderr
    assert log.index("systemctl stop hermes") < log.index(f"ops -m app.ops restore {env['BACKUP']}")
    assert log.index(f"ops -m app.ops restore {env['BACKUP']}") < log.index("systemctl start hermes")
    assert up and "restore ok" in r.stdout and "hermes ready" in r.stdout


def test_restore_failure_restarts_and_keeps_exit_code(env):
    r, log, up = run(env, RESTORE_RC="7")
    assert r.returncode == 7 and up and "systemctl start hermes" in log and "restore exited 7" in r.stderr


def test_sigterm_during_restore_still_restarts(env):
    r, log, up = run(env, TERM_SELF="1")
    assert r.returncode == 143 and up and "systemctl start hermes" in log


def test_previously_stopped_service_stays_stopped(env):
    Path(env["STATE"], "up").unlink()
    r, log, up = run(env)
    assert r.returncode == 0 and not up
    assert "systemctl stop hermes" not in log and "systemctl start hermes" not in log
    assert any(line.startswith("ops ") for line in log)


def test_previously_stopped_service_stays_stopped_on_failure(env):
    Path(env["STATE"], "up").unlink()
    r, log, up = run(env, RESTORE_RC="5")
    assert r.returncode == 5 and not up and "systemctl start hermes" not in log


def test_restart_failure_is_visible(env):
    r, _, up = run(env, FAIL_START="1")
    assert r.returncode == 1 and "could not start" in r.stderr and not up


def test_restart_failure_after_restore_failure_is_nonzero(env):
    r, _, _ = run(env, FAIL_START="1", RESTORE_RC="7")
    assert r.returncode == 1 and "could not start" in r.stderr


def test_readiness_failure_is_visible(env):
    r, _, up = run(env, FAIL_READY="1")
    assert r.returncode == 1 and up and "not ready" in r.stderr


def test_missing_backup_file_touches_nothing(env):
    r, log, up = run(env, BACKUP=env["BACKUP"] + ".nope")
    assert r.returncode == 1 and up and log == []


def test_unreadable_passphrase_touches_nothing(env):
    r, log, up = run(env, HERMES_PASSPHRASE_FILE=env["BACKUP"] + ".nope")
    assert r.returncode != 0 and up and "systemctl stop hermes" not in log


def test_passphrase_never_in_output_argv_or_log(env):
    for extra in ({}, {"RESTORE_RC": "7"}, {"FAIL_START": "1"}, {"FAIL_READY": "1"}):
        r, log, _ = run(env, **extra)
        assert SECRET not in r.stdout + r.stderr + "\n".join(log), extra
        Path(env["LOG"]).unlink(missing_ok=True)
        Path(env["STATE"], "up").touch()
    assert "set -x" not in (ROOT / "deploy/hermes-restore.sh").read_text()


def test_preset_passphrase_env_wins_over_file(env):
    (Path(env["HERMES_PASSPHRASE_FILE"])).write_text("wrong\n")
    r, _, _ = run(env, HERMES_BACKUP_PASSPHRASE=SECRET)
    assert r.returncode == 0, r.stderr


def test_trap_contract_matches_backup_script():
    sh = (ROOT / "deploy/hermes-restore.sh").read_text()
    assert "trap on_exit EXIT" in sh and "trap 'exit 143' INT TERM" in sh and "readyz" in sh
