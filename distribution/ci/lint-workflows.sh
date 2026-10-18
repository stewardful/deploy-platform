#!/usr/bin/env bash
# Give the linter YAML only: no checkout credentials, private source or network.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
files=()
for path in "$ROOT"/.github/workflows/*.yml; do files+=("/workflows/${path##*/}"); done
docker run --rm --network none -v "$ROOT/.github/workflows:/workflows:ro" \
    rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 -no-color "${files[@]}"
