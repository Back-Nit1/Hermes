# Remote deployment (optional)

Local mode is the default and needs no accounts: it binds to 127.0.0.1 and uses the one-time token link. Remote mode
is for running one HERMES server that a few trusted people reach over the internet, each with their own login and
their own separate data.

## Supported deployment path

The supported path is one Linux VM (Ubuntu 24.04 or similar) with Caddy terminating TLS, HERMES on loopback, systemd
supervising it, and data on local disk. Other setups (containers, other proxies) can work if they meet the same
requirements: TLS at the proxy, the app bound to loopback, `X-Forwarded-Proto` set by a trusted proxy, and a single
app process.

Follow these steps in order:

1. **Create the user and install the app:**

   ```bash
   sudo useradd --system --home /var/lib/hermes --create-home hermes
   sudo git clone <repo> /opt/hermes && cd /opt/hermes
   sudo python3.13 -m venv .venv
   sudo .venv/bin/pip install --require-hashes --no-deps -r requirements.lock  # includes pypdf (PDF research)
   ```

2. **Configure** `/etc/hermes/hermes.env` (owner root, mode 0600, readable via systemd only):

   ```ini
   HERMES_MODE=remote
   HERMES_PUBLIC_URL=https://hermes.example.org
   HERMES_SECRET_KEY=<output of: .venv/bin/python -m app.ops gen-secret-key>
   DATA_ROOT=/var/lib/hermes/data
   APP_HOST=127.0.0.1
   APP_PORT=8765
   # Optional: an operator-paid key used by users who have not set their own. Omit it to require per-user keys
   # (users without one get demo data).
   OPENAI_API_KEY=
   # Optional, for "Connect Gmail": a Google OAuth client of type *Web application* with the redirect URI
   # https://hermes.example.org/auth/gmail/callback
   GMAIL_CREDENTIALS=/etc/hermes/gmail-web-client.json
   ```

   Store `HERMES_SECRET_KEY` somewhere other than the server too (for example, a password manager). It decrypts
   users' stored credentials and is deliberately excluded from backups. Do not set `APP_TOKEN`; the app refuses to
   start in remote mode if it is set.

3. **TLS proxy:** install Caddy, copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile`, set your hostname, and reload.
   Caddy obtains and renews certificates automatically. Only ports 80 and 443 should be reachable from outside. The proxy body limit (15 MiB) is sized above the base64
   form of the 10 MiB attachment limit; raise both together.
   Port 8765 stays on loopback.

4. **Service:** copy `deploy/hermes.service` to `/etc/systemd/system/`, then run:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now hermes
   ```

   Check it with `curl -s http://127.0.0.1:8765/readyz` and `journalctl -u hermes` (logs are JSON lines).

5. **Accounts:** there is no self-signup. Operator commands run as the `hermes` user with the service's environment
   loaded. A small shell helper does this:

   ```bash
   hermes-ops() { sudo sh -c 'set -a; . /etc/hermes/hermes.env; cd /opt/hermes; exec setpriv --reuid=hermes \
     --regid=hermes --init-groups .venv/bin/python -m app.ops "$@"' hermes-ops "$@"; }
   hermes-ops user-add alice
   ```

   The command prompts for a password (at least 12 characters). Also available: `user-passwd`, `user-disable`,
   `user-enable`, `user-delete`, and `user-list`. Changing a password or disabling a user ends that user's sessions.

6. **Backups:** put a passphrase in `/etc/hermes/backup-passphrase` (root, mode 0600). It is loaded as a systemd
   credential by the backup unit only; do not add it to `hermes.env`. Then enable the nightly timer:

   ```bash
   sudo install -d -o hermes -g hermes -m 0700 /var/backups/hermes
   sudo cp deploy/hermes-backup.service deploy/hermes-backup.timer /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now hermes-backup.timer
   ```

   The timer runs `deploy/hermes-backup.sh` as root (the only way to stop a unit; it drops to the `hermes` user for
   every data access). Each run: gracefully stops `hermes` (releasing the data-root lock, jobs checkpointed), takes the
   exclusive encrypted backup, runs `ops verify-backup` on the file (decrypt, hashes, SQLite integrity), prunes, and
   **always** starts `hermes` again and waits up to 60 s for `/readyz`. A failed or unverifiable backup is deleted and
   the unit fails; a failed restart or readiness check also fails the unit. If `hermes` was already stopped, it is left
   stopped. Backup files are `0600`, owned by `hermes`, in a `0700` directory.

   - **Downtime:** expect HERMES to be unavailable for the stop grace period (up to `TimeoutStopSec`, 60 s), the
     backup and verification time (roughly proportional to data size; seconds for small data roots), and startup.
     In-flight jobs are checkpointed and resume. The timer fires 03:15 plus up to 15 min random delay; adjust
     `OnCalendar` to your quiet hours.
   - **Monitoring:** alert on the unit, not on the log. `systemctl is-failed hermes-backup.service` (or
     `systemctl list-units --failed`) must be clean; check `systemctl list-timers hermes-backup.timer` shows a recent
     `LAST` and a future `NEXT`; alert when the newest `*.hbk` in `/var/backups/hermes` is older than ~26 h; and probe
     `/readyz` externally. `journalctl -u hermes-backup` shows `backup ok: <file>` or `FAILED: ...`.
   - **Retention:** the newest 14 verified files (`BACKUP_KEEP`) are kept locally; pruning happens only after a new
     backup verified, so failures never shrink the set. Set a longer off-machine retention to match your recovery needs
     and your deletion obligations (old backups keep deleted data until they expire).
   - **Off-machine replication is NOT automatic.** Nothing here copies backups elsewhere. A backup on the same disk is
     not a disaster-recovery copy. Add a separate job (for example `rclone copy /var/backups/hermes remote:hermes`
     or `rsync` to another host, run after the timer) with its own failure alerting; files are encrypted, so ordinary
     object storage is fine. Store the passphrase and `HERMES_SECRET_KEY` separately from the backups.
   - **Restore drill (do it quarterly, and after any backup change):** copy a recent `.hbk` (preferably from the
     off-machine copy) and the passphrase to a scratch VM with the same app version, then
     `sudo deploy/hermes-restore.sh <file>.hbk`, confirm `/readyz`, log in, and open a campaign. The script stops the
     service, restores (old root kept as `<root>.pre-restore-*`), restarts, and waits for `/readyz`.
     Check `python -m app.ops verify-backup <file>` also passes without touching any data root.
     See [operations.md](operations.md).

7. **Upgrades:** take a backup, `git pull`, reinstall from the lock, then `systemctl restart hermes`. Migrations run
   at startup and make their own `.bak` copies first.

The stdio MCP adapter is a local-mode tool. It authenticates with the local app token, which remote mode never
accepts.

## What remote mode enforces

- **HTTPS only:** requests whose scheme (after trusted proxy headers) is not `https` are rejected with 400.
  Responses carry HSTS (2 years). The Host header must match `HERMES_PUBLIC_URL`. `/healthz` and `/readyz` are
  exempt so the proxy can probe over loopback.
- **Authentication:** username and password. Passwords are hashed with scrypt (n=2^14, r=8, p=1, 16-byte salt).
  Unknown users go through the same hash work, so response timing does not reveal which usernames exist. After 5
  failed attempts, a username is locked for 15 minutes.
- **Sessions:** 256-bit random tokens, stored only as SHA-256 hashes. The cookie is
  `__Host-hermes_session; Secure; HttpOnly; SameSite=Strict; Path=/`. Sessions expire after 12 hours idle or 7 days
  total. Logout, a password change, disabling the user, or deleting the user revokes them.
- **CSRF:** every state-changing request needs the per-session `X-CSRF-Token` header. If an `Origin` header is
  present it must equal the public URL, and login only accepts JSON. The session cookie is also SameSite=Strict.
- **Authorization and isolation:** each user has a separate data root (`DATA_ROOT/users/<id>/`) with its own SQLite
  database, campaign files, page cache, job workers, OpenAI client, and Gmail client. Every `/api` route resolves the
  service from the session's user, never from a request parameter, so another user's campaign ID is simply "not
  found". `tests/test_remote.py` checks every campaign-scoped route: campaigns, candidates, drafts, exports, jobs,
  files, and provider credentials.
- **Encrypted secrets:** each user's OpenAI key and Gmail OAuth token are Fernet-encrypted with `HERMES_SECRET_KEY`
  in `auth.sqlite3` (mode 0600). The API only reports whether they are set, and never returns them.
- **Gmail OAuth:** the web flow uses a random, single-use `state` value that expires after 10 minutes and is bound to
  the user who started the flow. PKCE is used when the library provides it. The app asks for the `gmail.compose`
  scope only, and it still never sends mail.
- **The local app token is never remote authentication:** in remote mode the token file is not written, the
  `X-App-Token` header is ignored, and the startup banner prints no link with a token in it.
- **Headers:** a strict Content-Security-Policy (scripts from this origin only, no inline scripts, `frame-ancestors
  'none'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` on
  API responses.

## Threat model

**Assets:** contacts and research profiles (personal data), draft emails, users' OpenAI keys and Gmail OAuth tokens,
account passwords, and API budget.

**Trust boundaries:** browser to Caddy (internet, TLS), Caddy to app (loopback), app to OpenAI, Google, and fetched web
pages (outbound), and operator shell access to the server.

| Threat | Mitigation |
|---|---|
| Network eavesdropping or tampering | TLS at Caddy, HSTS, Secure cookies, app reachable only on loopback |
| Password guessing | scrypt, 12-character minimum, per-username, per-client-address and global lockout (failed logins during a lockout carry `Retry-After`), no self-signup. The client address is the proxy-resolved peer: it is only correct when `HERMES_TRUSTED_PROXIES` lists exactly the proxy in front of the app (default `127.0.0.1`, which matches Caddy on the same host). Avoid `*`, which lets any peer choose its own address |
| Session theft through XSS | HttpOnly cookie, strict CSP, all rendered text escaped, web page text handled as untrusted data |
| Cross-site request forgery | SameSite=Strict, per-session CSRF header, Origin check, JSON-only login |
| One user reading or changing another's data | per-user data roots and services chosen by session only; isolation tests on every route |
| Stolen database or backup | provider secrets encrypted with a key kept outside data and backups; backups encrypted with a passphrase; session tokens stored hashed |
| Credential leakage through logs | redaction filter on every log record; no access logs with query strings; tests assert logs contain no keys, tokens, emails, or draft text |
| Prompt injection from fetched pages | unchanged from local mode: page text is quoted data, every claim needs a cited or fetched source, and a human approves every draft before Gmail |
| DNS rebinding and host-header attacks | exact Host match against the public URL |
| Clickjacking | `frame-ancestors 'none'` and `X-Frame-Options: DENY` |

**Out of scope, or accepted:**

- A malicious operator, or anyone with root on the server, can read everything. They hold the secret key and the
  data.
- Denial of service beyond login throttling is out of scope. Put rate limits in Caddy if the server is public.
- There are no roles. All users are equal, and none can administer others; that happens only from the CLI.
- If a user sets no personal key, users share the operator's OpenAI key when one is configured. Per-campaign budgets
  cap spend, but there is no per-user monthly quota.
- The login lockout counter lives in the process's memory and resets on restart.

## Known limitations of remote mode

- It runs as one process with one data root, so it scales up but not out.
- There is no multi-factor authentication, password reset by email, or single sign-on. Reset passwords with
  `user-passwd`.
- `HERMES_SECRET_KEY` rotation is not automated. To change the key, have users remove and re-add their credentials
  after the key is replaced.
