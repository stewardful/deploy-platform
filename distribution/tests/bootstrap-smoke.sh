#!/usr/bin/env bash
# Exercise the released curl | bash entry on a fresh disposable Ubuntu node.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
source "$ROOT/distribution/tests/node-exec.sh"
UBUNTU_VERSION=${UBUNTU_VERSION:-26.04}
[[ $UBUNTU_VERSION == 24.04 || $UBUNTU_VERSION == 26.04 ]] || exit 2
UPDATE=${1:?pass absolute platform update installer}
CLI=${2:?pass absolute cloudctl artifact with adjacent entry and checksums}
[[ $UPDATE == /* && -f $UPDATE && $CLI == /* && -f $CLI ]] || exit 2
NAME=cloud-bootstrap-test-$$
WORK=$(mktemp -d)
cleanup() {
    local status=$?
    if (( status != 0 )); then
        node_exec "$NAME" journalctl -u 'cloudctl-install-*' -u deploy-agent -u deploy-control -u bootstrap-test-https -n 100 --no-pager || true
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf -- "$WORK"
}
trap cleanup EXIT
DEV_UPDATE=$(python3 "$ROOT/distribution/tests/dev-package.py" "$UPDATE" "$WORK/update")
extra=()
[[ -z ${3:-} ]] || extra+=(--runtime "$3")
[[ -z ${4:-} ]] || extra+=(--proxy "$4")
python3 "$ROOT/distribution/tests/bootstrap-runtime.py" "$DEV_UPDATE" "$CLI" "$WORK/fixture" "${extra[@]}"
[[ -z ${5:-} ]] || cp -R "$5" "$WORK/fixture/webhook"
cp "$ROOT/distribution/tests/bootstrap-runtime.py" "$WORK/fixture/"
docker build --build-arg "UBUNTU_VERSION=$UBUNTU_VERSION" -f "$ROOT/distribution/tests/Dockerfile.ubuntu" -t "cloud-installer-test:$UBUNTU_VERSION" "$ROOT" >/dev/null
docker run -d --name "$NAME" --privileged --cgroupns=private --cpus=2 --memory=2g --pids-limit=512 \
    --tmpfs /run:exec --tmpfs /run/lock --tmpfs /var/lib/deploy-platform:exec,mode=0700,size=1g "cloud-installer-test:$UBUNTU_VERSION" >/dev/null
docker cp "$WORK/fixture" "$NAME:/var/tmp/bootstrap-fixture"
node_exec "$NAME" touch /run/cloud-bootstrap-isolated-test
node_exec "$NAME" python3 -B /var/tmp/bootstrap-fixture/bootstrap-runtime.py setup
docker stats --no-stream "$NAME"
