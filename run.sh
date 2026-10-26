#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PROJECT_ROOT="$( cd "$DIR/.." >/dev/null 2>&1 && pwd )"

# Prioritize project virtualenv python if available (bypasses conda base conflict)
if [ -f "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
elif [ -f "$PROJECT_ROOT/.venv/bin/python3" ]; then
    PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
else
    PYTHON_BIN="python"
fi

# Ensure httpx is installed in the target python environment
if ! "$PYTHON_BIN" -c "import httpx" 2>/dev/null; then
    echo "📦 Installing requirements using $PYTHON_BIN..."
    "$PYTHON_BIN" -m pip install -q -r "$DIR/requirements.txt"
fi

PYTHONPATH="$DIR" "$PYTHON_BIN" "$DIR/ao_mcp_client/cli.py" "$@"
