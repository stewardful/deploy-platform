#!/usr/bin/env bash
# GitHub runner transport only. Never embed Secrets in remote command strings.
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
: "${TEST_SSH_PRIVATE_KEY:?Set TEST_SSH_PRIVATE_KEY in GitHub Secrets}"
: "${TEST_SSH_KNOWN_HOSTS:?Set verified TEST_SSH_KNOWN_HOSTS in GitHub Secrets}"
: "${TEST_SERVER_HOST:?Set TEST_SERVER_HOST in GitHub Secrets}"
: "${TEST_SERVER_USER:?Set TEST_SERVER_USER in GitHub Secrets}"
: "${CI_ARTIFACT_DIR:=$ROOT/dist}"
: "${TEST_SERVER_ARCH:=amd64}" "${TEST_SERVER_PORT:=22}" "${DEPLOY_WAIT_SECONDS:=1800}"
: "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}" "${GITHUB_SHA:?}" "${RELEASE_VERSION:?}"
[[ $TEST_SERVER_ARCH == amd64 || $TEST_SERVER_ARCH == arm64 ]]
[[ $TEST_SERVER_PORT =~ ^[0-9]+$ && ${#TEST_SERVER_PORT} -le 5 ]] && (( 10#$TEST_SERVER_PORT >= 1 && 10#$TEST_SERVER_PORT <= 65535 ))
[[ $GITHUB_RUN_ID =~ ^[0-9]{1,20}$ && $GITHUB_RUN_ATTEMPT =~ ^[0-9]{1,6}$ && $GITHUB_SHA =~ ^[a-f0-9]{40}$ ]]
[[ $RELEASE_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $TEST_SERVER_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
[[ $DEPLOY_WAIT_SECONDS =~ ^[0-9]{1,5}$ ]] && (( 10#$DEPLOY_WAIT_SECONDS >= 60 && 10#$DEPLOY_WAIT_SECONDS <= 7200 ))
python3 - <<'PY'
import ipaddress, os, re
host=os.environ['TEST_SERVER_HOST']
if '%' in host: raise SystemExit('scoped IPv6 is not supported')
try: ipaddress.ip_address(host)
except ValueError:
    if len(host)>253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in host.split('.')):
        raise SystemExit('invalid TEST_SERVER_HOST')
PY
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
printf '%s\n' "$TEST_SSH_PRIVATE_KEY" | tr -d '\r' > "$work/key"
printf '%s\n' "$TEST_SSH_KNOWN_HOSTS" | tr -d '\r' > "$work/known_hosts"
unset TEST_SSH_PRIVATE_KEY TEST_SSH_KNOWN_HOSTS
ssh-keygen -y -P '' -f "$work/key" >/dev/null
ssh-keygen -l -f "$work/known_hosts" >/dev/null
stem=deploy-platform-$RELEASE_VERSION-linux-$TEST_SERVER_ARCH
python3 - "$CI_ARTIFACT_DIR/$stem.run" "$CI_ARTIFACT_DIR/$stem.sha256" "$work/SHA256SUMS" <<'PY'
import hashlib, re, sys
from pathlib import Path
installer, checksum, output = map(Path, sys.argv[1:])
if installer.is_symlink() or not installer.is_file(): raise SystemExit('installer missing or linked')
digest=hashlib.sha256()
with installer.open('rb') as handle:
    for chunk in iter(lambda: handle.read(256*1024),b''): digest.update(chunk)
actual=digest.hexdigest()
lines=checksum.read_text().splitlines()
expected=[line.split('  ',1)[0] for line in lines if re.fullmatch(r'[a-f0-9]{64}  '+re.escape(installer.name),line)]
if expected!=[actual]: raise SystemExit('CI artifact checksum mismatch')
output.write_text(actual+'  installer.run\n')
PY
options=(-i "$work/key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$work/known_hosts" -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
destination=$TEST_SERVER_USER@$TEST_SERVER_HOST
copy_host=$TEST_SERVER_HOST
[[ $copy_host != *:* ]] || copy_host="[$copy_host]"
copy_destination=$TEST_SERVER_USER@$copy_host
run=$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT
stage=/var/tmp/deploy-platform-ci-$run
printf '%s\n' 'Checking server OS, architecture, systemd, cgroup and sudo before uploading installer'
{
cat "$ROOT/distribution/installer/lib/os.sh"
cat <<'PREPARE'
set -Eeuo pipefail
umask 077
command -v python3 >/dev/null
command -v systemd-run >/dev/null
sudo -n true
check_supported_os
case $(uname -m) in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo 'unsupported server architecture' >&2; exit 1;; esac
[[ $arch == "$2" ]] || { echo "server architecture is $arch, installer architecture is $2" >&2; exit 1; }
[[ -f /sys/fs/cgroup/cgroup.controllers && -d /run/systemd/system ]] || { echo 'systemd and cgroup v2 required' >&2; exit 1; }
systemctl show-environment >/dev/null
available=$(df -Pk /opt | awk 'NR==2 {print $4}')
(( available >= 2097152 )) || { echo 'at least 2 GiB available disk required' >&2; exit 1; }
printf 'PASS: supported OS and architecture %s; upload can begin\n' "$arch"
mkdir -m 0700 -- "/var/tmp/deploy-platform-ci-$1"
PREPARE
} | ssh "${options[@]}" -p "$TEST_SERVER_PORT" "$destination" bash -s -- "$run" "$TEST_SERVER_ARCH"
printf '%s\n' 'Uploading verified installer'
scp "${options[@]}" -P "$TEST_SERVER_PORT" "$CI_ARTIFACT_DIR/$stem.run" "$copy_destination:$stage/installer.run"
scp "${options[@]}" -P "$TEST_SERVER_PORT" "$work/SHA256SUMS" "$ROOT/distribution/ci/remote-install.py" "$copy_destination:$stage/"
ssh "${options[@]}" -p "$TEST_SERVER_PORT" "$destination" bash -s -- "$run" "$GITHUB_SHA" <<'START'
set -Eeuo pipefail
stage=/var/tmp/deploy-platform-ci-$1
# Hand off to a root-owned directory before the independent root service opens files.
sudo -n chown -R root:root "$stage"
sudo -n chmod -R go-rwx "$stage"
sudo -n systemd-run --no-block --unit="deploy-platform-ci-$1" --property=Type=oneshot --property=TimeoutStartSec=infinity --property=UMask=0077 --property=Nice=10 --property=MemoryHigh=256M /usr/bin/python3 "$stage/remote-install.py" "$2"
START
end=$((SECONDS + 10#$DEPLOY_WAIT_SECONDS))
while (( SECONDS < end )); do
    if status=$(ssh "${options[@]}" -p "$TEST_SERVER_PORT" "$destination" sudo -n cat "$stage/result.json" 2>/dev/null); then
        phase=$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])')
        case $phase in
            succeeded)
                printf '%s\n' 'PASS: test server installer committed and application verification passed'
                if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then printf 'Test deployment succeeded: %s (run %s)\n' "$GITHUB_SHA" "$run" >> "$GITHUB_STEP_SUMMARY"; fi
                exit 0;;
            failed)
                printf '%s\n' 'Test deployment failed; private installer log follows.' >&2
                ssh "${options[@]}" -p "$TEST_SERVER_PORT" "$destination" sudo -n tail -n 100 "$stage/install.log" || true
                exit 1;;
        esac
    fi
    sleep 5
done
printf '%s\n' "Timed out waiting; remote unit deploy-platform-ci-$run may still be running. Reconnect and inspect it before retrying." >&2
exit 1
