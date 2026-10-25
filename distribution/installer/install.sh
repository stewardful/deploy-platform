#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1
SOURCE='' SKIP_APT=0 UPGRADE=0 SMOKE=0 CIDR='' DRAIN_SECONDS=300 VERIFY_SECONDS=120 CHECKS_FILE=/etc/deploy-platform/upgrade-checks.json
while (( $# )); do
    case $1 in
        --source) [[ -z $SOURCE && $# -ge 2 ]] || exit 2; SOURCE=$2; shift 2;;
        --skip-apt) SKIP_APT=1; shift;;
        --upgrade) UPGRADE=1; shift;;
        --drain-seconds) [[ $# -ge 2 && $2 =~ ^[0-9]+$ ]] || exit 2; DRAIN_SECONDS=$2; shift 2;;
        --verify-seconds) [[ $# -ge 2 && $2 =~ ^[0-9]+$ ]] || exit 2; VERIFY_SECONDS=$2; shift 2;;
        --checks-file) [[ $# -ge 2 && $2 == /* ]] || exit 2; CHECKS_FILE=$2; shift 2;;
        --smoke-test) SMOKE=1; shift;;
        --bridge-cidr) [[ $# -ge 2 ]] || exit 2; CIDR=$2; shift 2;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2;;
    esac
done
(( DRAIN_SECONDS >= 10 && DRAIN_SECONDS <= 3600 && VERIFY_SECONDS >= 10 && VERIFY_SECONDS <= 3600 )) || exit 2
[[ $SOURCE == /* && -f $SOURCE/lib/host.sh ]] || { echo 'validated release directory required' >&2; exit 2; }
# shellcheck source=lib/host.sh
source "$SOURCE/lib/host.sh"
preflight_host
exec 9>/run/lock/deploy-platform-install.lock
flock -n 9 || die 'another installer is running'
# Validate the update and host before fetching dependencies. Assembly runs under
# the installer lock, before maintenance, backup or service changes.
python3 "$SOURCE/lib/archive.py" verify "$SOURCE" --arch "$HOST_ARCH" >/dev/null
install_dependencies
timeout --kill-after=5 960 python3 - "$SOURCE" "$HOST_ARCH" <<'PYASSEMBLE'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1])/'lib'))
import archive
archive.verify(Path(sys.argv[1]), sys.argv[2])
import dependencies
dependencies.materialize(Path(sys.argv[1]))
PYASSEMBLE
VERSION=$(python3 "$SOURCE/lib/archive.py" verify "$SOURCE" --arch "$HOST_ARCH")
python3 "$SOURCE/lib/state.py" recover --source "$SOURCE"
python3 "$SOURCE/lib/state.py" check
if [[ ! -f /etc/deploy-platform/install.json && -f /var/lib/deploy-platform/agent.sqlite ]]; then
    die 'unmanaged Agent database found; use a planned migration, not fresh installation'
fi
if [[ -e /usr/local/bin/cloudctl || -L /usr/local/bin/cloudctl ]]; then
        python3 - "$SOURCE" <<'PYCLI'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1])/'lib'))
from first_install import assert_cli_launcher
assert_cli_launcher()
PYCLI
fi
if [[ -e /opt/deploy-platform/current || -L /opt/deploy-platform/current ]]; then
    [[ -f /etc/deploy-platform/install.json && -L /opt/deploy-platform/current && $(readlink /opt/deploy-platform/current) == /opt/deploy-platform/releases/* ]] || die 'unmanaged current release path'
fi
RELEASE_HASH=$(sha256sum "$SOURCE/manifest.json" | cut -d ' ' -f 1)
RELEASE_ID=$VERSION-${RELEASE_HASH:0:12}
if [[ -f /etc/deploy-platform/install.json ]]; then
    python3 - "$SOURCE/manifest.json" "$UPGRADE" <<'PY'
import json,sys,hashlib
old=json.load(open('/etc/deploy-platform/install.json'));new=json.load(open(sys.argv[1]))
sha=hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()
if old['manifest_sha256']!=sha and sys.argv[2]!='1': raise ValueError('different release: pass --upgrade after reviewing the release; a backup will be made')
if old['runtime']!=new['runtime']: raise ValueError('runtime version changes require a separate planned migration; not supported by this upgrader')
PY
    SAVED_CIDR=$(python3 -c 'import json; print(json.load(open("/etc/deploy-platform/install.json"))["bridge_cidr"])')
    [[ -z $CIDR || $CIDR == "$SAVED_CIDR" ]] || die 'cannot change an installed bridge CIDR in-place'
    CIDR=$SAVED_CIDR
fi
if ip link show cloud0 >/dev/null 2>&1 && [[ ! -f /etc/deploy-platform/install.json ]]; then die 'unmanaged cloud0 bridge exists'; fi
python3 "$SOURCE/lib/upgrade.py" preflight --source "$SOURCE" --checks-file "$CHECKS_FILE"
WORK=$(mktemp -d /var/tmp/cloud-install.XXXXXXXX)
trap 'rm -rf -- "$WORK"' EXIT
args=(); [[ -z $CIDR ]] || args=(--requested "$CIDR")
CIDR=$(ip -j -4 route show table all | python3 "$SOURCE/lib/network.py" "${args[@]}")
log "Ubuntu $HOST_VERSION / $HOST_ARCH; dedicated bridge $CIDR; installing prebuilt release $VERSION"
# shellcheck source=lib/activate.sh
source "$SOURCE/lib/activate.sh"
activate
if [[ $SMOKE == 1 ]]; then
    python3 /opt/deploy-platform/current/smoke.py project
fi
log 'Web: http://127.0.0.1:8088/ (use an SSH tunnel from your computer)'
log 'Read your existing/private login token with: sudo cloudctl token'
