#!/bin/sh
# Usage: sudo deploy/hermes-restore.sh /var/backups/hermes/<file>.hbk
# Stops the service, restores (old data root kept as <root>.pre-restore-<timestamp>), then starts it again.
# Passphrase: read from /etc/hermes/backup-passphrase, or HERMES_BACKUP_PASSPHRASE if already set.
# If the service was running, it is restarted on EVERY exit path (restore failure, SIGTERM/Ctrl-C), like
# hermes-backup.sh. Exit status: the restore's own failure code, or 1 if restart/readiness failed (or the
# restore succeeded but the service did not come back). A service that was stopped beforehand stays stopped.
set -u
APP_DIR=${HERMES_DIR:-/opt/hermes}
PORT=${APP_PORT:-8765}
READY_TRIES=${READY_TRIES:-30}
READY_SLEEP=${READY_SLEEP:-2}
PASS_FILE=${HERMES_PASSPHRASE_FILE:-/etc/hermes/backup-passphrase}
ENV_FILE=${HERMES_ENV_FILE:-/etc/hermes/hermes.env}

file=${1:?usage: $0 BACKUP.hbk}
[ -r "$file" ] || { echo "cannot read $file" >&2; exit 1; }
[ -n "${HERMES_BACKUP_PASSPHRASE:-}" ] || HERMES_BACKUP_PASSPHRASE="$(cat "$PASS_FILE")" || exit 1
export HERMES_BACKUP_PASSPHRASE
# Fail on bad config before taking the service down.
[ -r "$ENV_FILE" ] || { echo "cannot read $ENV_FILE" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

was_active=0
systemctl is-active --quiet hermes && was_active=1

restart() {
  [ "$was_active" = 1 ] || return 0
  was_active=0
  systemctl start hermes || { echo "FAILED: could not start hermes" >&2; return 1; }
  i=0
  while [ "$i" -lt "$READY_TRIES" ]; do
    curl -fsS "http://127.0.0.1:$PORT/readyz" >/dev/null 2>&1 && { echo "hermes ready"; return 0; }
    i=$((i + 1)); sleep "$READY_SLEEP"
  done
  echo "FAILED: hermes not ready after restart; check journalctl -u hermes" >&2
  return 1
}
on_exit() { code=$?; restart || code=1; exit "$code"; }
trap on_exit EXIT
trap 'exit 143' INT TERM

if [ "$was_active" = 1 ]; then
  systemctl stop hermes || { echo "FAILED: could not stop hermes; nothing restored" >&2; exit 1; }
fi

cd "$APP_DIR" || exit 1
setpriv --reuid=hermes --regid=hermes --init-groups --inh-caps=-all .venv/bin/python -m app.ops restore "$file"
rc=$?
if [ "$rc" = 0 ]; then
  echo "restore ok"
else
  echo "FAILED: restore exited $rc" >&2
fi
exit "$rc"
