#!/bin/sh
# Start the bot on a host that just runs one command (Wispbyte, Pterodactyl,
# Railway, a VPS, ...). Installs the two dependencies if they are missing and
# then hands the process over to the bot.
#
# Startup command to put in the panel:   bash start.sh
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

# A zip download (rather than a clone) drops the executable bit, and then
# every job fails with "permission denied" on the Luau runtime.
chmod +x deobf/bin/luau deobf/bin/luau-ast 2>/dev/null || true

if ! "$PY" -c "import discord" >/dev/null 2>&1; then
    echo "[*] installing dependencies..."
    "$PY" -m pip install --user --no-input --disable-pip-version-check -r requirements.txt \
        || "$PY" -m pip install --no-input --disable-pip-version-check -r requirements.txt
fi

exec "$PY" bot.py
