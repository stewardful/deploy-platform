#!/usr/bin/env bash
# Real Go SSE and protected dev worker on a disposable local Ubuntu node; no SSH.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
source "$ROOT/distribution/tests/node-exec.sh"
UBUNTU_VERSION=${UBUNTU_VERSION:-26.04}
[[ $UBUNTU_VERSION == 24.04 || $UBUNTU_VERSION == 26.04 ]] || exit 2
BASE=${1:?pass an absolute current-format offline package}
UPDATE=${2:?pass an absolute dev platform update package}
[[ $BASE == /* && -f $BASE && $UPDATE == /* && -f $UPDATE ]] || exit 2
WORK=$(mktemp -d)
NAME=cloud-notification-test-$$
cleanup() {
    local status=$?
    if (( status != 0 )); then
        node_exec "$NAME" journalctl -u deploy-control -u deploy-update -u notification-test-events -u notification-test-releases -n 100 --no-pager || true
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf -- "$WORK"
}
trap cleanup EXIT
DEV_UPDATE=$(python3 "$ROOT/distribution/tests/notification-package.py" "$UPDATE" "$WORK")
docker build --build-arg "UBUNTU_VERSION=$UBUNTU_VERSION" -f "$ROOT/distribution/tests/Dockerfile.ubuntu" -t "cloud-installer-test:$UBUNTU_VERSION" "$ROOT" >/dev/null
docker run -d --name "$NAME" --privileged --cgroupns=private --cpus=2 --memory=2g --pids-limit=512 \
    --tmpfs /run:exec --tmpfs /run/lock --tmpfs /var/lib/deploy-platform:exec,size=1g "cloud-installer-test:$UBUNTU_VERSION" >/dev/null
docker cp "$BASE" "$NAME:/var/tmp/base.run"
docker cp "$DEV_UPDATE" "$NAME:/var/tmp/new.run"
node_exec "$NAME" bash /var/tmp/base.run --skip-apt
node_exec "$NAME" touch /run/cloud-notification-isolated-test
docker cp "$ROOT/distribution/tests/notification-runtime.py" "$NAME:/var/tmp/notification-runtime.py"
node_exec "$NAME" python3 -B /var/tmp/notification-runtime.py setup
docker stats --no-stream "$NAME"
