#!/usr/bin/env bash
# Explicit integration test: needs a Linux Docker daemon with privileged containers.
# All node changes occur inside a disposable test container, never the build host.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
UBUNTU_VERSION=${UBUNTU_VERSION:-24.04}
[[ $UBUNTU_VERSION == 24.04 || $UBUNTU_VERSION == 26.04 ]] || { echo 'test Ubuntu must be 24.04 or 26.04' >&2; exit 2; }
STATEFUL=${STATEFUL:-0}
INSTALLER=${1:?pass an absolute .run installer path matching the Docker host architecture}
[[ $INSTALLER == /* && -f $INSTALLER ]] || exit 2
NAME=cloud-installer-test-$$
IMAGE=cloud-installer-test:$UBUNTU_VERSION
docker build --build-arg "UBUNTU_VERSION=$UBUNTU_VERSION" -f "$ROOT/distribution/tests/Dockerfile.ubuntu" -t "$IMAGE" "$ROOT"
cleanup() {
    local status=$?
    if (( status != 0 )); then
        # Only this disposable node's smoke containers exist here. Retry start
        # directly to expose the OCI error that the production API redacts.
        docker exec "$NAME" /opt/deploy-platform/nerdctl ps --all || true
        local instance
        while IFS= read -r instance; do
            [[ $instance =~ ^[a-f0-9]{12,64}$ ]] || continue
            docker exec "$NAME" timeout 15 /opt/deploy-platform/nerdctl start -- "$instance" || true
        done < <(docker exec "$NAME" /opt/deploy-platform/nerdctl ps --all --no-trunc --format '{{.ID}}' 2>/dev/null)
        docker exec "$NAME" journalctl -u deploy-proxy -u deploy-agent -u deploy-control -u deploy-containerd -n 100 --no-pager || true
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Separate exec-enabled storage avoids overlay-on-overlay limitations in nested tests.
docker run -d --name "$NAME" --privileged --cgroupns=private --cpus=2 --memory=2g --pids-limit=512 \
    --tmpfs /run:exec --tmpfs /run/lock --tmpfs /var/lib/deploy-platform:exec,size=1g "$IMAGE" >/dev/null
docker cp "$INSTALLER" "$NAME:/var/tmp/install.run"
docker exec "$NAME" bash /var/tmp/install.run --skip-apt
# Reinstallation must work with external HTTPS blocked: dependencies are reused
# from the verified installed release/cache, or already bundled offline.
docker exec "$NAME" iptables -I OUTPUT -p tcp --dport 443 -j REJECT
BEFORE=$(docker exec "$NAME" sha256sum /etc/deploy-platform/control.token)
docker exec "$NAME" bash /var/tmp/install.run --skip-apt
AFTER=$(docker exec "$NAME" sha256sum /etc/deploy-platform/control.token)
[[ $BEFORE == "$AFTER" ]] || { echo 'reinstall changed the token' >&2; exit 1; }
docker exec "$NAME" iptables -D OUTPUT -p tcp --dport 443 -j REJECT
echo 'PASS: reinstall with external HTTPS blocked (no dependency download)'
if [[ $STATEFUL == 1 ]]; then
    docker exec "$NAME" cloudctl stateful-smoke
else
    docker exec "$NAME" cloudctl smoke
fi
# Self-tests must not add bytecode files to an immutable, verified release.
docker exec "$NAME" cloudctl fullstack-smoke --help >/dev/null
docker exec "$NAME" python3 -B /opt/deploy-platform/current/lib/archive.py verify /opt/deploy-platform/current >/dev/null
docker exec "$NAME" bash /var/tmp/install.run --skip-apt
AFTER=$(docker exec "$NAME" sha256sum /etc/deploy-platform/control.token)
[[ $BEFORE == "$AFTER" ]] || { echo 'post-test reinstall changed the token' >&2; exit 1; }
docker stats --no-stream "$NAME"
echo 'PASS: fresh install, repeat install, post-self-test reinstall, token preservation and real project recovery'
