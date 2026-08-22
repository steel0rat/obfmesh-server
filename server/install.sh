#!/usr/bin/env bash
# obfmesh server installer: Ubuntu 24.04, Python 3.12, systemd.
# Idempotent - a re-run with unchanged sources changes nothing and does not restart the service.
set -euo pipefail

SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${OBFMESH_PREFIX:-/opt/obfmesh}"
VENV="$PREFIX/.venv"
CONF_DIR=/etc/obfmesh
STATE_DIR=/var/lib/obfmesh
ADMIN_KEY_FILE="$CONF_DIR/admin.key"
UNIT_NAME=obfmesh-server.service
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
PYTHON_BIN="${PYTHON_BIN:-python3}"
API_PORT="${OBFMESH_API_PORT:-8080}"
LISTEN_URL="http://127.0.0.1:$API_PORT/api/status"
CTL_PATH=/usr/local/sbin/obfmesh-ctl
LOGROTATE_PATH=/etc/logrotate.d/obfmesh
DEFAULT_OBFUSCATOR_BIN=/usr/local/bin/wg-obfuscator

REQUIRED_SOURCES=(
    requirements.txt
    obfmesh-server.service
    logrotate.obfmesh
    obfmesh/__init__.py
    obfmesh/app.py
    obfmesh/api.py
    obfmesh/auth.py
    obfmesh/cli.py
    obfmesh/models.py
    obfmesh/db.py
    obfmesh/orchestrator.py
    obfmesh/config_gen.py
    obfmesh/wg.py
)

code_changed=0
unit_changed=0

log() { printf '[obfmesh] %s\n' "$*"; }
die() { printf '[obfmesh] ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preconditions

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "$PYTHON_BIN not found"

"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11+ is required, found $("$PYTHON_BIN" -V 2>&1)"
"$PYTHON_BIN" -c 'import venv, ensurepip' >/dev/null 2>&1 \
    || die "the venv module is missing: apt-get install -y python3-venv"

# Without these the service still starts and answers, while every reconcile fails with
# rc=127 deep in the log and not a single spoke comes up. Checked here, loudly.
command -v ip >/dev/null 2>&1 || die "the 'ip' tool is missing: apt-get install -y iproute2"
command -v wg >/dev/null 2>&1 \
    || die "the 'wg' tool is missing (not installed on a bare Ubuntu 24.04): apt-get install -y wireguard-tools"
command -v iptables >/dev/null 2>&1 || die "iptables is missing: apt-get install -y iptables"
modprobe wireguard >/dev/null 2>&1 || true
[ -d /sys/module/wireguard ] || log "WARNING: the wireguard kernel module is not loaded yet; 'ip link add type wireguard' will load it on demand"

# The path is only a default here: the effective one lives in the settings row and is
# changeable through PATCH /api/settings, so a mismatch is a warning, not a failure.
OBF_BIN_CHECK="${OBFMESH_OBFUSCATOR_BIN:-$DEFAULT_OBFUSCATOR_BIN}"
if [ ! -x "$OBF_BIN_CHECK" ]; then
    log "WARNING: no executable obfuscator at $OBF_BIN_CHECK"
    log "         build/copy wg-obfuscator there, or set OBFMESH_OBFUSCATOR_BIN before running this script"
elif ! "$OBF_BIN_CHECK" --help 2>&1 | grep -qi masking; then
    log "WARNING: $OBF_BIN_CHECK does not mention 'masking' in --help"
    log "         obfmesh refuses to start a spoke without masking support (ISP throttles unmasked flows)"
fi

missing=()
for rel in "${REQUIRED_SOURCES[@]}"; do
    [ -f "$SRC_DIR/$rel" ] || missing+=("$rel")
done
if [ "${#missing[@]}" -gt 0 ]; then
    die "missing source files: ${missing[*]}"
fi

# ---------------------------------------------------------------- layout

install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/obfmesh" "$PREFIX/obfmesh/static"
install -d -m 0700 -o root -g root "$CONF_DIR" "$STATE_DIR"

# Copies only when content or mode differ and reports it in FILE_CHANGED, so the restart
# decision at the end stays honest. A failing copy aborts the script through set -e.
FILE_CHANGED=0
install_file() {
    local src=$1 dst=$2 mode=$3
    FILE_CHANGED=0
    if [ -f "$dst" ] && cmp -s "$src" "$dst" && [ "$(stat -c '%a' "$dst")" = "${mode#0}" ]; then
        return 0
    fi
    install -m "$mode" -o root -g root "$src" "$dst"
    FILE_CHANGED=1
    log "updated $dst"
}

shopt -s nullglob
for src in "$SRC_DIR"/obfmesh/*.py; do
    install_file "$src" "$PREFIX/obfmesh/$(basename "$src")" 0644
    if [ "$FILE_CHANGED" -eq 1 ]; then code_changed=1; fi
done
for src in "$SRC_DIR"/obfmesh/static/*; do
    [ -f "$src" ] || continue
    install_file "$src" "$PREFIX/obfmesh/static/$(basename "$src")" 0644
    if [ "$FILE_CHANGED" -eq 1 ]; then code_changed=1; fi
done
shopt -u nullglob
install_file "$SRC_DIR/requirements.txt" "$PREFIX/requirements.txt" 0644
if [ "$FILE_CHANGED" -eq 1 ]; then code_changed=1; fi

# ---------------------------------------------------------------- log rotation

# The obfuscators write to /var/log/obfmesh/obf{i}.log through an inherited fd, so a
# rotation that renames the file would leave them writing into a deleted inode:
# copytruncate is the only correct mode here.
install_file "$SRC_DIR/logrotate.obfmesh" "$LOGROTATE_PATH" 0644

# ---------------------------------------------------------------- control helper

ctl_tmp="$(mktemp)"
cat >"$ctl_tmp" <<EOF
#!/bin/sh
# obfmesh local control: teardown | reconcile | status
# PYTHONPATH, because the package is not pip-installed into the venv.
exec env PYTHONPATH="$PREFIX" "$VENV/bin/python" -m obfmesh.cli "\$@"
EOF
install_file "$ctl_tmp" "$CTL_PATH" 0755
rm -f "$ctl_tmp"

# ---------------------------------------------------------------- admin key

if [ ! -s "$ADMIN_KEY_FILE" ]; then
    (
        umask 077
        "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))' >"$ADMIN_KEY_FILE"
    )
    log "generated admin key: $ADMIN_KEY_FILE (the value is never printed by this script)"
    code_changed=1
fi
chown root:root "$ADMIN_KEY_FILE"
chmod 600 "$ADMIN_KEY_FILE"

# ---------------------------------------------------------------- virtualenv

if [ ! -x "$VENV/bin/python" ]; then
    log "creating virtualenv $VENV"
    "$PYTHON_BIN" -m venv "$VENV"
    code_changed=1
fi

req_hash="$(sha256sum "$SRC_DIR/requirements.txt" | cut -d' ' -f1)"
req_stamp="$VENV/.requirements.sha256"
if [ ! -f "$req_stamp" ] || [ "$(cat "$req_stamp")" != "$req_hash" ]; then
    log "installing python dependencies"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet --requirement "$SRC_DIR/requirements.txt"
    printf '%s\n' "$req_hash" >"$req_stamp"
    code_changed=1
else
    log "dependencies already match requirements.txt"
fi

# ---------------------------------------------------------------- systemd unit

install_file "$SRC_DIR/$UNIT_NAME" "$UNIT_PATH" 0644
if [ "$FILE_CHANGED" -eq 1 ]; then
    unit_changed=1
fi

# OBFMESH_EXTERNAL_HOST and OBFMESH_OBFUSCATOR_BIN are read by the core only while the settings
# row is being created, so they have to reach the service, not just this script. Without them
# the address is taken from the default route and the binary path keeps its built-in default;
# both are changeable later through PATCH /api/settings.
if [ -n "${OBFMESH_EXTERNAL_HOST:-}" ] || [ -n "${OBFMESH_OBFUSCATOR_BIN:-}" ]; then
    dropin_dir="$UNIT_PATH.d"
    install -d -m 0755 -o root -g root "$dropin_dir"
    dropin_tmp="$(mktemp)"
    {
        printf '# Written by install.sh: first-boot values for the settings row.\n'
        printf '[Service]\n'
        if [ -n "${OBFMESH_EXTERNAL_HOST:-}" ]; then
            printf 'Environment=OBFMESH_EXTERNAL_HOST=%s\n' "$OBFMESH_EXTERNAL_HOST"
        fi
        if [ -n "${OBFMESH_OBFUSCATOR_BIN:-}" ]; then
            printf 'Environment=OBFMESH_OBFUSCATOR_BIN=%s\n' "$OBFMESH_OBFUSCATOR_BIN"
        fi
    } >"$dropin_tmp"
    install_file "$dropin_tmp" "$dropin_dir/10-bootstrap.conf" 0644
    rm -f "$dropin_tmp"
    if [ "$FILE_CHANGED" -eq 1 ]; then unit_changed=1; fi
fi

if [ "$unit_changed" -eq 1 ]; then
    systemctl daemon-reload
fi

if ! systemctl is-enabled --quiet "$UNIT_NAME"; then
    systemctl enable --quiet "$UNIT_NAME"
    log "enabled $UNIT_NAME"
fi

if [ "$code_changed" -eq 1 ] || [ "$unit_changed" -eq 1 ]; then
    log "restarting $UNIT_NAME"
    systemctl restart "$UNIT_NAME"
elif ! systemctl is-active --quiet "$UNIT_NAME"; then
    log "starting $UNIT_NAME"
    systemctl start "$UNIT_NAME"
else
    log "nothing changed, $UNIT_NAME left running"
fi

# ---------------------------------------------------------------- verification

# Two checks. First: an anonymous request must be answered with 401 - that proves the app is
# up and that authentication is enforced. Second: an authenticated /api/status must report a
# reconcile without errors, otherwise "installed" would mean nothing more than "FastAPI boots".
"$VENV/bin/python" - "$LISTEN_URL" "$ADMIN_KEY_FILE" <<'PY' || die "health check failed, see: journalctl -u obfmesh-server -n 80"
import json, sys, time, urllib.error, urllib.request

url, key_file = sys.argv[1], sys.argv[2]

deadline = time.monotonic() + 20
last = "no answer"
ready = False
while time.monotonic() < deadline:
    try:
        urllib.request.urlopen(url, timeout=2)
        last = "unauthenticated request was accepted"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("[obfmesh] health check: 401 on an anonymous request, as expected")
            ready = True
            break
        last = f"HTTP {exc.code}"
    except OSError as exc:
        last = str(exc)
    time.sleep(0.5)

if not ready:
    print(f"[obfmesh] health check failed: {last}", file=sys.stderr)
    sys.exit(1)

with open(key_file, "r", encoding="utf-8") as handle:
    admin_key = handle.read().strip()

request = urllib.request.Request(url, headers={"X-API-Key": admin_key})
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
except (urllib.error.HTTPError, OSError, ValueError) as exc:
    print(f"[obfmesh] authenticated /api/status failed: {exc}", file=sys.stderr)
    sys.exit(1)

spokes = payload.get("spokes") or []
running = [s for s in spokes if (s.get("obfuscator") or {}).get("running")]
up = [s for s in spokes if s.get("iface_up")]
print(
    f"[obfmesh] status: spokes={len(spokes)} interfaces_up={len(up)} "
    f"obfuscators_running={len(running)} config_version={payload.get('config_version')}"
)

# The report of the boot-time reconcile lives in the journal; here the observable state is
# what decides. A configured spoke without a live interface or a live obfuscator is a failure.
problems = []
for spoke in spokes:
    if not spoke.get("desired"):
        continue
    index = spoke.get("index")
    if not spoke.get("iface_up"):
        problems.append(f"spoke {index}: {spoke.get('iface')} is not up")
    if not (spoke.get("obfuscator") or {}).get("running"):
        problems.append(f"spoke {index}: obfuscator is not running ({(spoke.get('obfuscator') or {}).get('log_path')})")
if not spokes:
    problems.append("no spokes at all: reconcile() never got through, see the journal")

if problems:
    for line in problems:
        print(f"[obfmesh] PROBLEM: {line}", file=sys.stderr)
    sys.exit(1)
print("[obfmesh] health check: every configured spoke is up and its obfuscator is running")
PY

systemctl --no-pager --lines=0 status "$UNIT_NAME" || true

# The client reaches the API through a reverse proxy on 443; without one the router cannot
# talk to this server at all, and the loopback health check above says nothing about it.
if ! ss -Hltn 'sport = :443' 2>/dev/null | grep -q .; then
    log "WARNING: nothing is listening on :443 - the router cannot reach this server yet."
    log "         Set up the reverse proxy (snippet below) before configuring the client."
fi

# The exact snippet is also dropped next to the config so it can be copied on the server
# itself instead of from this terminal.
CADDY_SNIPPET="$CONF_DIR/caddy-snippet.txt"
cat >"$CADDY_SNIPPET" <<EOF
# obfmesh: publish the API and the UI at https://<this-host>/obfmesh
# Put this inside the site block of /etc/caddy/Caddyfile, then: systemctl reload caddy
handle_path /obfmesh/* {
    reverse_proxy 127.0.0.1:$API_PORT {
        flush_interval -1          # required: /api/events is a streaming response
    }
}
EOF
chmod 0644 "$CADDY_SNIPPET"

cat <<EOF

[obfmesh] installed under $PREFIX, state in $STATE_DIR, listening on 127.0.0.1:$API_PORT.

Admin key (read it yourself, it is not printed here):
    sudo cat $ADMIN_KEY_FILE

Local control (no HTTP):
    $CTL_PATH status
    $CTL_PATH reconcile
    $CTL_PATH teardown          # stops the obfuscators that survive systemctl stop

First-boot values, optional, applied only while the settings row does not exist yet:
    OBFMESH_EXTERNAL_HOST=45.136.127.10 OBFMESH_OBFUSCATOR_BIN=/usr/local/bin/wg-obfuscator sudo -E ./install.sh

Reverse proxy is a MANDATORY separate step - the service listens on loopback only.
The snippet is in $CADDY_SNIPPET :

$(cat "$CADDY_SNIPPET")

For a bare IP address Caddy issues a certificate from its internal CA, which curl on the
router will not trust. Copy the CA to the router and point ca_file at it:

    scp /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt root@<router>:/etc/obfmesh/server-ca.crt
    ssh root@<router> "uci set obfmesh.main.ca_file=/etc/obfmesh/server-ca.crt; uci commit obfmesh"

Logs:      journalctl -u $UNIT_NAME -f
           /var/log/obfmesh/obf{i}.log (rotated by $LOGROTATE_PATH)
Restart:   systemctl restart $UNIT_NAME
EOF
