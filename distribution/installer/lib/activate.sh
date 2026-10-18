# shellcheck shell=bash
stop_application_units() {
    local unit
    for unit in deploy-control.service deploy-agent.service deploy-proxy.service; do
        if systemctl cat "$unit" >/dev/null 2>&1; then systemctl stop "$unit"; fi
    done
}
# shellcheck shell=bash
# Expects validated SOURCE, VERSION, CIDR, RELEASE_ID and installer host helpers.
rollback() {
    local code=$?
    trap - ERR
    set +e
    if [[ ${ACTIVATING:-0} == 1 ]]; then
        log 'activation failed; restoring previous application files and service configuration'
        python3 "$SOURCE/lib/state.py" recover --source "$SOURCE" || log 'automatic recovery incomplete; retry installer to resume recovery'
        log "diagnostics: journalctl -u deploy-agent -u deploy-control -u deploy-containerd -n 80; backup: $BACKUP"
        log 'application database was backed up but never automatically downgraded'
    fi
    exit "$code"
}
activate() {
    local destination=/opt/deploy-platform/releases/$RELEASE_ID unit generated=$WORK/generated
    install -d -m 0755 /opt/deploy-platform/releases
    if [[ -e $destination ]]; then
        python3 "$SOURCE/lib/archive.py" verify "$destination" --arch "$HOST_ARCH" >/dev/null
    else
        local staging=$destination.staging-$$
        install -d -m 0755 "$staging"
        cp -a "$SOURCE/." "$staging/"
        chmod -R go-w "$staging"
        mv "$staging" "$destination"
    fi
    # Public release assets must be traversable by the DynamicUser proxy.
    # Node secrets and writable state are stored outside the release directory.
    python3 - "$destination" <<'PYPERMISSIONS'
from pathlib import Path
import sys
root=Path(sys.argv[1])
root.chmod(0o755)
for path in root.rglob('*'):
    if path.is_dir(): path.chmod(0o755)
PYPERMISSIONS
    python3 "$SOURCE/lib/configure.py" "$generated" "$CIDR"
    BACKUP=/var/lib/deploy-platform/backups/$(date -u +%Y%m%dT%H%M%SZ)-$$
    install -d -m 0700 "$BACKUP"
    python3 "$SOURCE/lib/state.py" snapshot --destination "$BACKUP"
    python3 "$SOURCE/lib/state.py" begin --destination "$BACKUP" --cidr "$CIDR"
    ACTIVATING=1
    trap rollback ERR
    stop_application_units
    if [[ -f /var/lib/deploy-platform/agent.sqlite ]]; then
        python3 - "$BACKUP/agent.sqlite" <<'PY'
import sqlite3, sys
source = sqlite3.connect('file:/var/lib/deploy-platform/agent.sqlite?mode=ro', uri=True)
target = sqlite3.connect(sys.argv[1])
source.backup(target)
target.close()
source.close()
PY
    fi
    [[ ! -L /etc/deploy-platform-proxy ]] || die 'proxy config parent cannot be a symlink'
    install -d -m 0755 /etc/deploy-platform-proxy
    install -m 0644 "$generated/proxy-bootstrap.json" /etc/deploy-platform-proxy/bootstrap.json
    if [[ -f /var/lib/deploy-platform/ingress/ingress.sqlite ]]; then
        python3 - "$BACKUP/ingress.sqlite" <<'PYBACKUP'
import sqlite3,sys
with sqlite3.connect('file:/var/lib/deploy-platform/ingress/ingress.sqlite?mode=ro',uri=True) as src:
    with sqlite3.connect(sys.argv[1]) as dst: src.backup(dst)
PYBACKUP
    fi
    install -d -m 0700 /etc/deploy-platform /etc/deploy-platform/cni /var/lib/deploy-platform
    install -m 0600 "$generated/containerd.toml" /etc/deploy-platform/containerd.toml
    install -m 0600 "$generated/nerdctl.toml" /etc/deploy-platform/nerdctl.toml
    install -m 0600 "$generated/10-cloud.conflist" /etc/deploy-platform/cni/10-cloud.conflist
    install -m 0755 "$generated/nerdctl" /opt/deploy-platform/nerdctl
    for unit in deploy-containerd.service deploy-agent.service deploy-control.service deploy-proxy.service; do
        install -m 0644 "$SOURCE/systemd/$unit" "/etc/systemd/system/$unit"
    done
    printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/deploy-platform.conf
    grep -qw overlay /proc/filesystems || modprobe overlay
    if [[ ! -d /proc/sys/net/bridge ]]; then modprobe br_netfilter || log 'br_netfilter unavailable; checking available bridge sysctls'; fi
    printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/90-deploy-platform.conf
    if [[ -f /proc/sys/net/bridge/bridge-nf-call-iptables ]]; then
        printf 'net.bridge.bridge-nf-call-iptables = 1\n' >> /etc/sysctl.d/90-deploy-platform.conf
    fi
    sysctl -p /etc/sysctl.d/90-deploy-platform.conf >/dev/null
    if [[ ! -e /etc/deploy-platform/control.token ]]; then
        (set -o noclobber; openssl rand -hex 32 > /etc/deploy-platform/control.token)
        chmod 0600 /etc/deploy-platform/control.token
    fi
    python3 - <<'PY'
from pathlib import Path
import re, stat
p=Path('/etc/deploy-platform/control.token')
if p.is_symlink() or not p.is_file() or stat.S_IMODE(p.stat().st_mode)&0o077 or not re.fullmatch('[A-Za-z0-9_-]{32,128}',p.read_text().strip()):
    raise ValueError('existing token must be a private regular file containing a valid token; it was not replaced')
PY
    ln -s "$destination" /opt/deploy-platform/current.next
    mv -Tf /opt/deploy-platform/current.next /opt/deploy-platform/current
    ln -sfn /opt/deploy-platform/current/cloudctl /usr/local/bin/cloudctl
    python3 "$SOURCE/lib/state.py" receipt --source "$SOURCE" --cidr "$CIDR" --phase activating
    systemctl daemon-reload
    systemctl enable --now deploy-containerd.service
    local ready=0 attempt
    for ((attempt=0; attempt<30; attempt++)); do
        if /opt/deploy-platform/current/runtime/bin/ctr --address /run/deploy-platform-containerd/containerd.sock version >/dev/null 2>&1; then ready=1; break; fi
        sleep 1
    done
    [[ $ready == 1 ]]
    local namespaces
    namespaces=$(/opt/deploy-platform/current/runtime/bin/ctr --address /run/deploy-platform-containerd/containerd.sock namespaces list --quiet)
    if ! grep -qx deploy-platform <<< "$namespaces"; then
        /opt/deploy-platform/current/runtime/bin/ctr --address /run/deploy-platform-containerd/containerd.sock namespaces create deploy-platform >/dev/null
    fi
    systemctl enable --now deploy-proxy.service deploy-agent.service deploy-control.service
    python3 "$SOURCE/smoke.py" health --wait 45
    python3 "$SOURCE/lib/state.py" receipt --source "$SOURCE" --cidr "$CIDR" --phase ready
    python3 "$SOURCE/lib/state.py" commit
    ACTIVATING=0
    trap - ERR
    log "installed $VERSION ($HOST_ARCH); database/config backup: $BACKUP"
}
