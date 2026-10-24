#!/usr/bin/env bash
# Native Linux release build; intended for CI/build machines, never target nodes.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
[[ $(uname -s) == Linux ]] || { echo 'Build on Linux (or use Dockerfile.agent for Agent binaries)' >&2; exit 1; }
case $(uname -m) in x86_64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; *) exit 1;; esac
VERSION=${VERSION:-0.5.3}
CACHE=${RELEASE_CACHE:-$ROOT/target/release-cache}
mkdir -p "$CACHE" "$ROOT/target/release-bin/$ARCH"
PACKAGE_KIND=${PACKAGE_KIND:-offline}
[[ $PACKAGE_KIND == offline || $PACKAGE_KIND == platform-update ]] || exit 2
args=(--kind "$PACKAGE_KIND")
if [[ $PACKAGE_KIND == offline ]]; then
URL=$(python3 - "$ARCH" <<'PY'
import json,sys
r=json.load(open('distribution/release/runtime.lock.json'));print(r['archive_url_template'].format(arch=sys.argv[1]))
PY
)
RUNTIME=$CACHE/${URL##*/}
if [[ ! -f $RUNTIME ]]; then
    curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --connect-timeout 20 --max-time 600 "$URL" -o "$RUNTIME.part"
    mv "$RUNTIME.part" "$RUNTIME"
fi
# Reject a damaged cache before running expensive compilation.
python3 - "$ARCH" "$RUNTIME" <<'PYHASH'
import hashlib,json,sys
with open(sys.argv[2], 'rb') as source:
    actual=hashlib.file_digest(source, 'sha256').hexdigest()
expected=json.load(open('distribution/release/runtime.lock.json'))['archives'][sys.argv[1]]
if actual != expected: raise ValueError('runtime archive checksum mismatch; remove the damaged cache and retry')
PYHASH
PROXY=$CACHE/caddy_2.11.7_linux_$ARCH.tar.gz
if [[ ! -f $PROXY ]]; then
    curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --connect-timeout 20 --max-time 300 "https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_linux_$ARCH.tar.gz" -o "$PROXY.part"
    mv "$PROXY.part" "$PROXY"
fi
args+=(--runtime-archive "$RUNTIME" --proxy-archive "$PROXY")
fi

npm ci
npm run web:build
cargo build --release --locked -j 1 -p node-agent --bins
cp target/release/agentd target/release/agentctl "target/release-bin/$ARCH/"
CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o "target/release-bin/$ARCH/controld" ./apps/controlplane/cmd/controld
python3 distribution/release/package.py --arch "$ARCH" --version "$VERSION" --bin-dir "target/release-bin/$ARCH" --output "${RELEASE_OUTPUT:-target/dist}" "${args[@]}"
