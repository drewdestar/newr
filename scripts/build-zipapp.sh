#!/usr/bin/env bash
# Build a CLEAN zipapp single file for newr (M7 step 2).
#
# Usage:
#   scripts/build-zipapp.sh
#
# Output: dist/newr.pyz (default). Run it with `python3 newr.pyz ...`
# or `./newr.pyz` (the shebang is /usr/bin/env python3).
#
# Why a staging dir instead of `python3 -m zipapp .`: zipping the
# repo root bundles tests, goldens, conformance, benchmarks, stress,
# and docs, and the result is 51.7 MB. The clean build stages only
# the runtime (the `compiler/` package plus `__main__.py`) and
# lands around 1 to 3 MB compressed. The seven
# `compiler/newr_rt.*` runtime files ARE included (they live inside
# the package and the emitters read them via importlib.resources,
# which works from inside a zip); `__pycache__` is excluded.
#
# Env overrides (CI and local builds):
#   NEWR_PYZ_DISTPATH  output dir, default dist
#   NEWR_PYZ_WORKPATH  staging dir, default build/zipapp
#   NEWR_PYZ_NAME      output file name, default newr.pyz
#   PYTHON             python executable, default python3
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

PYTHON="${PYTHON:-python3}"
DISTPATH="${NEWR_PYZ_DISTPATH:-dist}"
WORKPATH="${NEWR_PYZ_WORKPATH:-build/zipapp}"
PYZ_NAME="${NEWR_PYZ_NAME:-newr.pyz}"

rm -rf "$WORKPATH"
mkdir -p "$WORKPATH" "$DISTPATH"

# Stage the runtime only. rsync keeps the tree clean; fall back to
# cp when rsync is absent (not all CI images ship it).
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude __pycache__ compiler/ "$WORKPATH/compiler/"
else
  mkdir -p "$WORKPATH/compiler"
  cp compiler/*.py compiler/newr_rt.* "$WORKPATH/compiler/"
fi
cp __main__.py "$WORKPATH/__main__.py"

"$PYTHON" -m zipapp "$WORKPATH" -o "$DISTPATH/$PYZ_NAME" \
  -p "/usr/bin/env python3" --compress

echo "built: $DISTPATH/$PYZ_NAME"
ls -l "$DISTPATH/$PYZ_NAME"
"$PYTHON" "$DISTPATH/$PYZ_NAME" version
