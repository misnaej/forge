#!/usr/bin/env bash
# Run the test suite across all supported Python versions.
#
# Forge supports Python >=3.11 (see pyproject.toml). This script creates
# (or reuses) one throwaway conda env per version, editable-installs
# forge with test deps, and runs `pytest tests/`.
#
# Idempotent: existing envs are reused. To wipe and rebuild:
#     conda env remove -n forge-test-py311   # etc.
#
# Usage:
#     ./dev/test-matrix.sh                # 3.11, 3.12, 3.13
#     PY_VERSIONS="3.12 3.13" ./dev/test-matrix.sh
#
# Forge-only — consumers run their own matrices (tox, nox, GitHub Actions).

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY_VERSIONS="${PY_VERSIONS:-3.11 3.12 3.13}"

if ! command -v conda >/dev/null 2>&1; then
    echo -e "${RED}conda not on PATH.${NC}" >&2
    exit 1
fi

# Source conda activate machinery (needed in non-interactive shells).
CONDA_BASE="$(conda info --base)"
# shellcheck source=/dev/null
source "$CONDA_BASE/etc/profile.d/conda.sh"

FAILED=()
for v in $PY_VERSIONS; do
    env="forge-test-py${v//./}"
    echo -e "\n${YELLOW}=== python ${v} (env: ${env}) ===${NC}"

    if ! conda env list | awk '{print $1}' | grep -qx "$env"; then
        echo -e "${YELLOW}→${NC} creating env"
        conda create -y -n "$env" "python=$v" >/dev/null
    fi

    conda run -n "$env" pip install -e ".[test]" --quiet
    if conda run -n "$env" pytest tests/ -q; then
        echo -e "${GREEN}✓ python ${v} passed${NC}"
    else
        echo -e "${RED}✗ python ${v} failed${NC}"
        FAILED+=("$v")
    fi
done

echo ""
if [ ${#FAILED[@]} -eq 0 ]; then
    echo -e "${GREEN}All Python versions passed.${NC}"
    exit 0
fi
echo -e "${RED}Failed versions: ${FAILED[*]}${NC}"
exit 1
