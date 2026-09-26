#!/bin/bash
# EvoTrader — Shell wrapper for db_query.py
# Resolves virtual env Python and runs the query script.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Execute the python script using the project's virtual environment python
exec "$PROJECT_ROOT/.venv/bin/python" "$SCRIPT_DIR/db_query.py" "$@"
