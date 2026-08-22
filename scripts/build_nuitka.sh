#!/usr/bin/env bash
# Build Nuitka binaire Linux (x64)
# Usage: ./scripts/build_nuitka.sh [--onefile]
set -e
PYTHON=${PYTHON:-python3}
ONEFILE=""
if [[ "$1" == "--onefile" ]]; then ONEFILE="--onefile"; fi
echo "Building opencode with Nuitka..."
$PYTHON -m nuitka \
  --standalone \
  --python-flag=no_site \
  --python-flag=no_warnings \
  --include-package=config \
  --include-package=dashboard \
  --include-package=vpn_manager \
  --include-package=free_ip_pool \
  --include-data-dir=static=static \
  --include-data-file=config.yaml=config.yaml \
  --enable-plugin=anti-bloat \
  --lto=yes \
  --jobs=$(nproc) \
  $ONEFILE \
  opencode.py
echo "Done: opencode.dist/opencode.bin"
