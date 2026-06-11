#!/usr/bin/env bash
# Forge dev environment setup.
#
# Creates a conda env, editable-installs forge with test deps, wires the
# dogfood pre-commit hook via .githooks/install.sh, and runs forge-doctor to
# verify the install. This is the self-test rig: forge uses it to prove its
# own standards (pre-commit hook, ruff config, doctor) work end-to-end.
#
# Not shipped via pip. Lives under dev/ so it stays clearly out of the
# consumer-facing surface.
#
# Usage:
#   ./dev/setup.sh                                  # env name: forge
#   CONDA_ENV_NAME=forge-dev ./dev/setup.sh         # override env name
#   PYTHON_VERSION=3.12 ./dev/setup.sh              # override python version
#
# Re-run safe: skips steps already done.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_NAME="${CONDA_ENV_NAME:-forge}"
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"

# 1. Require conda.
if ! command -v conda >/dev/null 2>&1; then
    echo -e "${RED}conda not on PATH.${NC} Install Miniconda or Anaconda first:"
    echo "  https://docs.conda.io/projects/miniconda/en/latest/"
    exit 1
fi

# Source conda activate machinery (needed in non-interactive shells).
CONDA_BASE="$(conda info --base)"
# shellcheck source=/dev/null
source "$CONDA_BASE/etc/profile.d/conda.sh"

# 2. Create env if missing.
if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
    echo -e "${GREEN}✓${NC} conda env '$ENV_NAME' already exists"
else
    echo -e "${YELLOW}→${NC} creating conda env '$ENV_NAME' (python=$PYTHON_VERSION)..."
    conda create -y -n "$ENV_NAME" "python=$PYTHON_VERSION"
fi

# 3. Activate.
conda activate "$ENV_NAME"

# Sanity-check we're in the right env.
if [ "${CONDA_DEFAULT_ENV:-}" != "$ENV_NAME" ]; then
    echo -e "${RED}Failed to activate '$ENV_NAME'.${NC}"
    exit 1
fi

# 4. Editable install with test deps.
echo -e "${YELLOW}→${NC} pip install -e .[test]"
pip install -e ".[test]"

# 5. Run the consumer-facing umbrella installer. Dogfood: forge is the
# source of install-forge-bootstrap, so running it here exercises the
# same path every consumer follows. --skip claude-md because forge IS
# the source of FOUNDATION.md (root file is a symlink to
# src/forge/data/FOUNDATION.md); install-forge-claude-md would wrap
# the source with managed markers and corrupt it.
echo -e "${YELLOW}→${NC} install-forge-bootstrap --skip claude-md"
install-forge-bootstrap --skip claude-md || {
    echo -e "${YELLOW}install-forge-bootstrap reported failures.${NC} Review output above."
}

echo ""
echo -e "${GREEN}Setup complete.${NC}"
echo "  Activate the env in new shells:  conda activate $ENV_NAME"
echo "  Run tests:                       pytest tests/"
echo "  Pre-commit hook fires on commit (validate manually: .githooks/pre-commit)"
