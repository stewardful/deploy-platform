#!/usr/bin/env bash
# Disposable SSH target only; never contacts a user server or reads a real key.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
INSTALLER=${1:?absolute installer path with sibling .sha256}
[[ $INSTALLER == /* && -f $INSTALLER ]]
read -r RELEASE_VERSION TEST_SERVER_ARCH < <(python3 - "$INSTALLER" <<'PY'
from pathlib import Path
import re,sys
m=re.fullmatch(r'deploy-platform-(\d+\.\d+\.\d+)-linux-(amd64|arm64)\.run',Path(sys.argv[1]).name)
if not m: raise SystemExit('unexpected installer name')
print(*m.groups())
PY
)
NAME=cloud-ci-ssh-test-$$
work=$(mktemp -d)
runner_pid=''
cleanup() {
    if [[ -n $runner_pid ]]; then kill "$runner_pid" 2>/dev/null || true; fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf -- "$work"
}
trap cleanup EXIT
docker build -f "$ROOT/distribution/tests/Dockerfile.ubuntu" -t cloud-upgrade-test:24.04 "$ROOT" >/dev/null
docker build -f "$ROOT/distribution/tests/Dockerfile.ci-ssh" -t cloud-ci-ssh-test:24.04 "$ROOT" >/dev/null
docker run -d --name "$NAME" --privileged --cgroupns=private --cpus=2 --memory=2g --pids-limit=512 \
    -p 127.0.0.1::22 --tmpfs /run:exec --tmpfs /run/lock --tmpfs /var/lib/deploy-platform:exec,size=2g cloud-ci-ssh-test:24.04 >/dev/null
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
docker exec "$NAME" mkdir -p -m 0700 /root/.ssh
docker cp "$work/key.pub" "$NAME:/root/.ssh/authorized_keys" >/dev/null
docker exec "$NAME" chown root:root /root/.ssh/authorized_keys
docker exec "$NAME" chmod 0600 /root/.ssh/authorized_keys
# The base Docker root account is locked; enable key-only auth in this disposable node.
docker exec "$NAME" passwd -d root >/dev/null
docker exec -i "$NAME" sh -c 'cat > /etc/ssh/sshd_config.d/00-ci-test.conf' <<'SSHD'
PermitRootLogin prohibit-password
PasswordAuthentication no
UsePAM no
SSHD
docker exec "$NAME" systemctl start ssh.service
TEST_SERVER_PORT=$(docker port "$NAME" 22/tcp | sed 's/.*://')
public=$(docker exec "$NAME" cat /etc/ssh/ssh_host_ed25519_key.pub)
TEST_SSH_KNOWN_HOSTS="[127.0.0.1]:$TEST_SERVER_PORT $public"
TEST_SSH_PRIVATE_KEY=$(cat "$work/key")
TEST_SERVER_HOST=127.0.0.1 TEST_SERVER_USER=root
CI_ARTIFACT_DIR=$(dirname -- "$INSTALLER")
GITHUB_RUN_ID=$(date +%s)$$ GITHUB_RUN_ATTEMPT=1
GITHUB_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
GITHUB_STEP_SUMMARY=$work/summary
export RELEASE_VERSION TEST_SERVER_ARCH TEST_SERVER_PORT TEST_SERVER_HOST TEST_SERVER_USER TEST_SSH_KNOWN_HOSTS TEST_SSH_PRIVATE_KEY CI_ARTIFACT_DIR GITHUB_RUN_ID GITHUB_RUN_ATTEMPT GITHUB_SHA GITHUB_STEP_SUMMARY
wrong="[127.0.0.1]:$TEST_SERVER_PORT $(cat "$work/key.pub")"
if TEST_SSH_KNOWN_HOSTS="$wrong" bash "$ROOT/distribution/ci/deploy-test.sh" > "$work/wrong-host.log" 2>&1; then
    echo 'wrong host key was accepted' >&2; exit 1
fi
docker exec "$NAME" test ! -e /etc/deploy-platform/install.json
echo 'PASS: wrong SSH host key refused before upload/installation'
if ! bash "$ROOT/distribution/ci/deploy-test.sh"; then
    docker exec "$NAME" journalctl -u ssh -n 40 --no-pager
    exit 1
fi
echo 'PASS: real SSH upload, verified checksum and independent first installation'
# A second accepted deployment must finish even if the runner loses its session.
GITHUB_RUN_ATTEMPT=2
export GITHUB_RUN_ATTEMPT
bash "$ROOT/distribution/ci/deploy-test.sh" > "$work/disconnected.log" 2>&1 &
runner_pid=$!
stage=/var/tmp/deploy-platform-ci-$GITHUB_RUN_ID-2
started=0
for ((attempt=0; attempt<60; attempt++)); do
    if docker exec "$NAME" test -f "$stage/result.json"; then started=1; break; fi
    sleep 1
done
[[ $started == 1 ]]
kill "$runner_pid" 2>/dev/null || true
wait "$runner_pid" 2>/dev/null || true
runner_pid=''
finished=0
for ((attempt=0; attempt<180; attempt++)); do
    phase=$(docker exec "$NAME" python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["phase"])' "$stage/result.json")
    if [[ $phase == succeeded ]]; then finished=1; break; fi
    if [[ $phase == failed ]]; then docker exec "$NAME" cat "$stage/install.log"; exit 1; fi
    sleep 1
done
[[ $finished == 1 ]]
docker exec "$NAME" test ! -e /var/lib/deploy-platform/install-transaction.json
docker exec "$NAME" cloudctl maintenance status
echo 'PASS: SSH/runner disconnect does not cancel accepted same-version upgrade'
