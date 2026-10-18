#!/usr/bin/env bash
# Explicit destructive acceptance inside a disposable Docker node only.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
OLD=${1:?old absolute installer path}
NEW=${2:?new absolute installer path}
DEMO=${3:?absolute demo image archive; tag cloud-fullstack-demo:upgrade-check}
for file in "$OLD" "$NEW" "$DEMO"; do [[ $file == /* && -f $file ]] || exit 2; done
NAME=cloud-upgrade-test-$$
docker build -f "$ROOT/distribution/tests/Dockerfile.ubuntu" -t cloud-upgrade-test:24.04 "$ROOT"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$NAME" --privileged --cgroupns=private --cpus=2 --memory=2g --pids-limit=512 \
    --tmpfs /run:exec --tmpfs /run/lock --tmpfs /var/lib/deploy-platform:exec,size=2g cloud-upgrade-test:24.04 >/dev/null
docker cp "$OLD" "$NAME:/var/tmp/old.run"
docker cp "$NEW" "$NAME:/var/tmp/new.run"
docker cp "$DEMO" "$NAME:/var/tmp/demo.tar"
docker cp "$ROOT/distribution/tests/upgrade-runtime.py" "$NAME:/var/tmp/upgrade-runtime.py"
docker exec "$NAME" touch /run/cloud-upgrade-isolated-test
docker cp "$ROOT/distribution/tests/update-delivery.py" "$NAME:/var/tmp/update-delivery.py"
docker exec -e CLOUD_TEST_WEB_UPDATE="${CLOUD_TEST_WEB_UPDATE:-0}" "$NAME" python3 -B /var/tmp/upgrade-runtime.py
docker stats --no-stream "$NAME"
echo 'PASS: isolated 2CPU/2GiB upgrade, HTTPS data retention, failure rollback and native drain'
