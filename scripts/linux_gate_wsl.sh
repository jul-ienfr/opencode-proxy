#!/usr/bin/env bash
# Gate de release sur Linux/WSL.
#
# SQLite WAL ne fonctionne pas sur le montage DrvFs (/mnt/c) : la copie de
# travail doit vivre sur le filesystem Linux. Le script copie le dépôt, crée
# un venv Python 3.12 avec `uv`, installe les dépendances puis lance le gate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${TMPDIR:-/tmp}/opencode-proxy-one-gate"
VENV="${TMPDIR:-/tmp}/ocvenv312"
export PATH="${HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv est requis : python3 -m pip install --user uv" >&2
  exit 2
fi
if ! uv python find 3.12 >/dev/null 2>&1; then
  uv python install 3.12
fi

rm -rf "${WORK}"
mkdir -p "${WORK}"
(
  cd "${ROOT}"
  tar \
    --exclude=.git \
    --exclude=node_modules \
    --exclude=.venv \
    --exclude=logs \
    --exclude=__pycache__ \
    --exclude=.pytest_cache \
    -cf - .
) | (
  cd "${WORK}"
  tar -xf -
)

if [ ! -x "${VENV}/bin/python" ]; then
  uv venv --python 3.12 "${VENV}"
fi
uv pip install --python "${VENV}/bin/python" -r "${ROOT}/requirements.txt" -r "${ROOT}/requirements-dev.txt"

cd "${WORK}"
"${VENV}/bin/python" scripts/quality_gate.py --with-soak
