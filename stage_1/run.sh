#!/bin/sh
# Run with a single command: ./run.sh https://t.me/<username> [-n 5]
cd "$(dirname "$0")"

if ! python3 -c "import httpx, bs4" 2>/dev/null; then
    echo "Installing dependencies..."
    python3 -m pip install --user -r requirements.txt
fi

exec python3 -m scrapper "$@"
