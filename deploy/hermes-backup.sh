#!/bin/sh
# Nightly encrypted backup, run as root by hermes-backup.service (passphrase arrives as a systemd credential).
# Lifecycle: stop HERMES (graceful, releases the data-root lock) -> exclusive backup as the hermes user -> verify the
# artifact -> retention -> ALWAYS restart HERMES and wait for /readyz. Exit is non-zero if the backup, the
# verification, or the restart/readiness failed, so the unit goes "failed" and monitoring sees it.
set -u
DEST=${BACKUP_DIR:-/var/backups/hermes}
KEEP=${BACKUP_KEEP:-14}
APP_DIR=${HERMES_DIR:-/opt/hermes}
PORT=${APP_PORT:-8765}
READY_TRIES=${READY_TRIES:-30}
READY_SLEEP=${READY_SLEEP:-2}

pass_file="${CREDENTIALS_DIRECTORY:?run under systemd LoadCredential}/backup-passphrase"
HERMES_BACKUP_PASSPHRASE="$(cat "$pass_file")" || exit 1
export HERMES_BACKUP_PASSPHRASE
umask 0077
out="$DEST/$(date -u +%Y-%m-%dT%H%M%SZ).hbk"

# Everything that touches data or the passphrase runs unprivileged, like the app itself.
as_hermes() { setpriv --reuid=hermes --regid=hermes --init-groups --inh-caps=-all "$@"; }
ops() { (cd "$APP_DIR" && as_hermes "$APP_DIR/.venv/bin/python" -m app.ops "$@"); }

rc=0
was_active=0
systemctl is-active --quiet hermes && was_active=1

# Restart on every exit path (failure, SIGTERM from a stop/timeout) so a bad backup never leaves HERMES down.
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
  systemctl stop hermes || { echo "FAILED: could not stop hermes; no backup taken" >&2; exit 1; }
fi

if ops backup "$out" && ops verify-backup "$out"; then
  echo "backup ok: $out"
else
  echo "FAILED: backup or verification failed; discarding $out" >&2
  rm -f -- "$out" "$out.part"
  rc=1
fi

# Retention only after a verified backup, so failures never shrink the set of good files.
if [ "$rc" = 0 ]; then
  # Keep the newest $KEEP; the sort key is the UTC timestamp in the name.
  ls -1 "$DEST"/*.hbk | sort -r | tail -n +$((KEEP + 1)) | while read -r old; do rm -f -- "$old"; done
fi
exit "$rc"
