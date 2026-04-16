#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UV_BIN="${UV_BIN:-/Users/ritam/.local/bin/uv}"

cd "$ROOT_DIR"
mkdir -p "$ROOT_DIR/runtime"

exec "$UV_BIN" run python main.py
