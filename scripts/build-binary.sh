#!/usr/bin/env bash
# Build a standalone one-file newr binary with PyInstaller (M7).
#
# Usage:
#   scripts/build-binary.sh
#
# Requires PyInstaller on PATH (pip install pyinstaller==6.22.3).
# Output: dist/newr-binary (one file, no Python needed to run it).
#
# The seven compiler/newr_rt.* runtime files MUST be bundled with
# --add-data: the emitters read them from disk at build time via
# importlib.resources, and PyInstaller only bundles imported modules,
# not data files. A naive build passes `check` but fails `build`
# with FileNotFoundError: compiler/newr_rt.py.
#
# Env overrides (CI and local builds):
#   NEWR_BINARY_DISTPATH  output dir, default dist
#   NEWR_BINARY_WORKPATH  PyInstaller work dir, default build/binary
#   PYINSTALLER           pyinstaller executable, default pyinstaller
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

PYINSTALLER="${PYINSTALLER:-pyinstaller}"
DISTPATH="${NEWR_BINARY_DISTPATH:-dist}"
WORKPATH="${NEWR_BINARY_WORKPATH:-build/binary}"

# NOTE: --add-data sources must be absolute paths. PyInstaller
# resolves relative --add-data paths against the spec directory
# (WORKPATH here), not the current directory.
# The --add-data separator is ":" on Linux/macOS and ";" on
# Windows (Git Bash reports MINGW64/MSYS/CYGWIN in uname).
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) SEP=";" ;;
  *) SEP=":" ;;
esac
"$PYINSTALLER" \
  --onefile \
  --clean \
  --noconfirm \
  --name newr-binary \
  --distpath "$DISTPATH" \
  --workpath "$WORKPATH" \
  --specpath "$WORKPATH" \
  --add-data "$REPO_ROOT/compiler/newr_rt.py${SEP}compiler" \
  --add-data "$REPO_ROOT/compiler/newr_rt.js${SEP}compiler" \
  --add-data "$REPO_ROOT/compiler/newr_rt.java${SEP}compiler" \
  --add-data "$REPO_ROOT/compiler/newr_rt.cs${SEP}compiler" \
  --add-data "$REPO_ROOT/compiler/newr_rt.rs${SEP}compiler" \
  --add-data "$REPO_ROOT/compiler/newr_rt.kt${SEP}compiler" \
  --add-data "$REPO_ROOT/compiler/newr_rt.dart${SEP}compiler" \
  newr

echo "built: $DISTPATH/newr-binary"
ls -l "$DISTPATH/newr-binary"
