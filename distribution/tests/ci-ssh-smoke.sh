#!/usr/bin/env bash
# Disposable SSH target only; never contacts a user server or reads a real key.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=distribution/tests/node-exec.sh
source "$ROOT/distribution/tests/node-exec.sh"
UBUNTU_VERSION=${UBUNTU_VERSION:-24.04}
[[ $UBUNTU_VERSION == 24.04 || $UBUNTU_VERSION == 26.04 ]] || { echo 'test Ubuntu must be 24.04 or 26.04' >&2; exit 2; }
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
docker build --build-arg "UBUNTU_VERSION=$UBUNTU_VERSION" -f "$ROOT/distribution/tests/Dockerfile.ubuntu" -t "cloud-upgrade-test:$UBUNTU_VERSION" "$ROOT" >/dev/null
docker build --build-arg "UBUNTU_VERSION=$UBUNTU_VERSION" -f "$ROOT/distribution/tests/Dockerfile.ci-ssh" -t "cloud-ci-ssh-test:$UBUNTU_VERSION" "$ROOT" >/dev/null
docker run -d --name "$NAME" --privileged --cgroupns=private --cpus=2 --memory=2g --pids-limit=512 \
    -p 127.0.0.1::22 --tmpfs /run:exec --tmpfs /run/lock --tmpfs /var/lib/deploy-platform:exec,size=2g "cloud-ci-ssh-test:$UBUNTU_VERSION" >/dev/null
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
node_exec "$NAME" mkdir -p -m 0700 /root/.ssh
docker cp "$work/key.pub" "$NAME:/root/.ssh/authorized_keys" >/dev/null
node_exec "$NAME" chown root:root /root/.ssh/authorized_keys
node_exec "$NAME" chmod 0600 /root/.ssh/authorized_keys
# The base Docker root account is locked; enable key-only auth in this disposable node.
node_exec "$NAME" passwd -d root >/dev/null
node_exec -i "$NAME" sh -c 'cat > /etc/ssh/sshd_config.d/00-ci-test.conf' <<'SSHD'
PermitRootLogin prohibit-password
PasswordAuthentication no
UsePAM no
SSHD
node_exec "$NAME" systemctl start ssh.service
TEST_SERVER_PORT=$(docker port "$NAME" 22/tcp | sed 's/.*://')
public=$(node_exec "$NAME" cat /etc/ssh/ssh_host_ed25519_key.pub)
TEST_SSH_KNOWN_HOSTS="[127.0.0.1]:$TEST_SERVER_PORT $public"
TEST_SSH_PRIVATE_KEY=$(cat "$work/key")
TEST_SERVER_HOST=127.0.0.1 TEST_SERVER_USER=root
CI_ARTIFACT_DIR=$(dirname -- "$INSTALLER")
GITHUB_RUN_ID=$(date +%s)$$ GITHUB_RUN_ATTEMPT=1
GITHUB_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
GITHUB_STEP_SUMMARY=$work/summary
export RELEASE_VERSION TEST_SERVER_ARCH TEST_SERVER_PORT TEST_SERVER_HOST TEST_SERVER_USER TEST_SSH_KNOWN_HOSTS TEST_SSH_PRIVATE_KEY CI_ARTIFACT_DIR GITHUB_RUN_ID GITHUB_RUN_ATTEMPT GITHUB_SHA GITHUB_STEP_SUMMARY
# Fixture archive is delivered by local Docker only. The runner transport never sends it over SSH.
python3 - "$INSTALLER" "$work" <<'PYARCHIVE'
from pathlib import Path
import hashlib,json,sys,zipfile
source=Path(sys.argv[1]);work=Path(sys.argv[2])
archive=work/'artifact.zip'
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as handle:
    handle.write(source,source.name)
    handle.write(source.with_suffix('.sha256'),source.with_suffix('.sha256').name)
def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(256*1024),b''):value.update(chunk)
    return value.hexdigest()
(work/'request.json').write_text(json.dumps({'schema_version':1,'repository':'owner/repo','run_id':'1','attempt':'1','commit':'a'*40,'version':source.name.split('-')[2],'arch':source.name.split('-')[-1].split('.')[0],'artifact_id':99,'archive_sha256':digest(archive),'archive_size':archive.stat().st_size,'installer_sha256':digest(source),'installer_size':source.stat().st_size,'download_seconds':180}))
PYARCHIVE
node_exec "$NAME" touch /run/cloud-ci-delivery-isolated-test
docker cp "$work/artifact.zip" "$NAME:/var/tmp/artifact.zip" >/dev/null
docker cp "$ROOT/distribution/tests/ci-delivery-runtime.py" "$NAME:/var/tmp/ci-delivery-runtime.py" >/dev/null
node_exec "$NAME" python3 -B /var/tmp/ci-delivery-runtime.py setup
mkdir "$work/bin"
real_python=$(command -v python3)
# Stub only the CI-side GitHub authorization. Real SSH, TLS, download, archive validation,
# independent systemd and installer/rollback remain unchanged production code.
cat > "$work/bin/python3" <<'PYPROVIDER'
#!/usr/bin/env python3
PYPROVIDER
python3 - "$work/bin/python3" "$real_python" <<'PYWRAPPER'
from pathlib import Path
import sys
p=Path(sys.argv[1]);p.write_text('#!'+sys.argv[2]+'\n'+'''import json,os,sys
from pathlib import Path
if len(sys.argv)>1 and sys.argv[1].endswith('artifact-request.py'):
    if sys.argv[2]=='prepare':
        request=json.loads(Path(os.environ['CI_FIXTURE_REQUEST_FILE']).read_text())
        request.update(run_id=os.environ['GITHUB_RUN_ID'],attempt=os.environ['GITHUB_RUN_ATTEMPT'],commit=os.environ['GITHUB_SHA'])
        Path(sys.argv[3]).write_text(json.dumps(request))
    else:
        request=json.loads(Path(sys.argv[3]).read_text())
        root=Path(sys.argv[1]).parent
        url='https://ci-delivery-test.blob.core.windows.net/artifact.zip?mode='+os.environ['CI_FIXTURE_MODE']+'&revision='+os.environ.get('URL_REVISION','1')
        print(json.dumps({'request':request,'ticket':{'url':url,'revision':int(os.environ.get('URL_REVISION','1'))},'files':{n:(root/n).read_text() for n in ('remote-install.py','delivery.py')}}))
else:
    os.execv('''+repr(sys.argv[2])+''',['''+repr(sys.argv[2])+''',*sys.argv[1:]])
''')
PYWRAPPER
chmod 0700 "$work/bin/python3"
export PATH="$work/bin:$PATH" CI_FIXTURE_REQUEST_FILE="$work/request.json"
export CI_FIXTURE_MODE=corrupt
wrong="[127.0.0.1]:$TEST_SERVER_PORT $(cat "$work/key.pub")"
if TEST_SSH_KNOWN_HOSTS="$wrong" bash "$ROOT/distribution/vendor/deploy-delivery/scripts/deploy-test.sh" > "$work/wrong-host.log" 2>&1; then
    echo 'wrong host key was accepted' >&2; exit 1
fi
node_exec "$NAME" test ! -e /etc/deploy-platform/install.json
echo 'PASS: wrong SSH host key refused before download/installation'
if bash "$ROOT/distribution/vendor/deploy-delivery/scripts/deploy-test.sh" > "$work/corrupt.log" 2>&1; then echo 'corrupt artifact was installed' >&2; exit 1; fi
node_exec "$NAME" test ! -e /etc/deploy-platform/install.json
node_exec "$NAME" python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["error_code"]=="archive_digest_mismatch"' "/var/tmp/deploy-platform-ci-$GITHUB_RUN_ID-1/result.json"
echo 'PASS: corrupt HTTPS archive rejected before installation'
GITHUB_RUN_ATTEMPT=2 CI_FIXTURE_MODE=resume-expired
export GITHUB_RUN_ATTEMPT CI_FIXTURE_MODE
if ! bash "$ROOT/distribution/vendor/deploy-delivery/scripts/deploy-test.sh"; then
    node_exec "$NAME" journalctl -u "deploy-platform-ci-$GITHUB_RUN_ID-2" -n 40 --no-pager
    exit 1
fi
node_exec "$NAME" python3 -c 'from pathlib import Path; assert any(int(x)>0 for x in Path("/var/tmp/ci-range-offsets").read_text().splitlines())'
echo 'PASS: expired URL refreshed, interrupted HTTPS resumed, verified first install succeeded'
# A second accepted deployment must finish even if the Actions runner disconnects.
GITHUB_RUN_ATTEMPT=3 CI_FIXTURE_MODE=slow
export GITHUB_RUN_ATTEMPT CI_FIXTURE_MODE
bash "$ROOT/distribution/vendor/deploy-delivery/scripts/deploy-test.sh" > "$work/disconnected.log" 2>&1 &
runner_pid=$!
stage=/var/tmp/deploy-platform-ci-$GITHUB_RUN_ID-3
started=0
for ((attempt=0; attempt<60; attempt++)); do
    if node_exec "$NAME" test -f "$stage/result.json"; then started=1; break; fi
    sleep 1
done
[[ $started == 1 ]]
kill "$runner_pid" 2>/dev/null || true
wait "$runner_pid" 2>/dev/null || true
runner_pid=''
# Concurrent delivery must fail without touching an already accepted installation.
GITHUB_RUN_ATTEMPT=4
export GITHUB_RUN_ATTEMPT
if bash "$ROOT/distribution/vendor/deploy-delivery/scripts/deploy-test.sh" > "$work/busy.log" 2>&1; then echo 'concurrent delivery unexpectedly accepted' >&2; exit 1; fi
node_exec "$NAME" python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["error_code"]=="deployment_busy"' "/var/tmp/deploy-platform-ci-$GITHUB_RUN_ID-4/result.json"
echo 'PASS: parallel delivery rejected by independent worker lock'
finished=0
for ((attempt=0; attempt<180; attempt++)); do
    phase=$(node_exec "$NAME" python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["phase"])' "$stage/result.json")
    if [[ $phase == succeeded ]]; then finished=1; break; fi
    if [[ $phase == failed ]]; then node_exec "$NAME" cat "$stage/install.log"; exit 1; fi
    sleep 1
done
[[ $finished == 1 ]]
node_exec "$NAME" test ! -e /var/lib/deploy-platform/install-transaction.json
node_exec "$NAME" test ! -e "$stage/download.json"
node_exec "$NAME" cloudctl maintenance status
echo 'PASS: runner disconnect does not cancel HTTPS download or protected upgrade'
