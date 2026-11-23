#!/usr/bin/env bash
# Source this before any uv/pip/playwright command so all caches and
# downloads stay on the linux-dev partition (see PRD §0.5), not on /.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RA_HOME="$REPO_ROOT/.cache-ra"
export XDG_CACHE_HOME="$RA_HOME/xdg-cache"
export UV_CACHE_DIR="$RA_HOME/uv-cache"
export UV_PYTHON_INSTALL_DIR="$RA_HOME/uv-python"
export UV_LINK_MODE=copy
export PLAYWRIGHT_BROWSERS_PATH="$RA_HOME/playwright-browsers"
export PIP_CACHE_DIR="$RA_HOME/pip-cache"
export TMPDIR="$RA_HOME/tmp"
mkdir -p "$XDG_CACHE_HOME" "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$PLAYWRIGHT_BROWSERS_PATH" "$PIP_CACHE_DIR" "$TMPDIR"
