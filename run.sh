#!/usr/bin/env bash
# ESCApe Explorer launcher for macOS and Linux.
# Make it executable once:  chmod +x run.sh
# Then run:                  ./run.sh

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    exec python3 launch.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python launch.py "$@"
else
    echo
    echo "Python 3 was not found."
    echo "  macOS:  install from https://www.python.org/downloads/ (includes tcl/tk)"
    echo "          or:  brew install python python-tk"
    echo "  Linux:  sudo apt-get install python3 python3-venv python3-tk"
    echo
    exit 1
fi
