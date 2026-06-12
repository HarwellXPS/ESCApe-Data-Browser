#!/usr/bin/env python3
"""
launch.py — one-step launcher for ESCApe Explorer.

Run this with any Python 3.8+ interpreter:

    python launch.py            (Windows)
    python3 launch.py           (macOS / Linux)

It will, on first run:
  1. create an isolated virtual environment in ./.venv
  2. install the required packages (matplotlib, Pillow) into it
  3. launch escape_explorer.py using that environment

On later runs it skips straight to step 3 (unless requirements change),
so startup is fast. Nothing is installed into your system Python.

Useful flags:
    python launch.py --setup-only   set up the environment but don't launch
    python launch.py --reinstall    rebuild the environment from scratch
    python launch.py --check        report environment status and exit
"""

from __future__ import annotations

import os
import sys
import shutil
import hashlib
import subprocess
import venv

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(HERE, ".venv")
APP = os.path.join(HERE, "escape_explorer.py")
REQS = os.path.join(HERE, "requirements.txt")
STAMP = os.path.join(VENV_DIR, ".requirements.sha1")

REQUIRED = ["matplotlib>=3.5", "pillow>=9.0", "reportlab>=3.6"]


def log(msg):
    print(f"[launch] {msg}", flush=True)


def venv_python() -> str:
    """Path to the Python interpreter inside the venv."""
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def reqs_hash() -> str:
    if os.path.exists(REQS):
        data = open(REQS, "rb").read()
    else:
        data = "\n".join(REQUIRED).encode()
    return hashlib.sha1(data).hexdigest()


def check_python_version():
    if sys.version_info < (3, 8):
        log(f"Python 3.8+ required; you have {sys.version.split()[0]}.")
        sys.exit(1)


def check_tkinter():
    """tkinter ships with Python but needs an OS package on some Linux."""
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        log("tkinter is not available in this Python installation.")
        if sys.platform.startswith("linux"):
            log("On Debian/Ubuntu:   sudo apt-get install python3-tk")
            log("On Fedora:          sudo dnf install python3-tkinter")
            log("On Arch:            sudo pacman -S tk")
        elif sys.platform == "darwin":
            log("On macOS, install Python from python.org (its build "
                "includes tkinter), or:  brew install python-tk")
        else:
            log("Reinstall Python from python.org and tick the 'tcl/tk' "
                "option in the installer.")
        return False


def create_venv():
    if os.path.exists(VENV_DIR):
        shutil.rmtree(VENV_DIR)
    log("Creating virtual environment in ./.venv ...")
    # with_pip=True bootstraps pip inside the new environment
    venv.create(VENV_DIR, with_pip=True, clear=True)


def pip_install():
    py = venv_python()
    log("Upgrading pip ...")
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"],
                   check=False)
    log("Installing dependencies (matplotlib, Pillow) ...")
    if os.path.exists(REQS):
        cmd = [py, "-m", "pip", "install", "-r", REQS]
    else:
        cmd = [py, "-m", "pip", "install", *REQUIRED]
    res = subprocess.run(cmd)
    if res.returncode != 0:
        log("Dependency install failed. Check your internet connection or "
            "proxy settings and try:  python launch.py --reinstall")
        sys.exit(res.returncode)
    with open(STAMP, "w") as fh:
        fh.write(reqs_hash())


def environment_ready() -> bool:
    if not os.path.exists(venv_python()):
        return False
    if not os.path.exists(STAMP):
        return False
    return open(STAMP).read().strip() == reqs_hash()


def ensure_environment(force=False):
    if force or not os.path.exists(venv_python()):
        create_venv()
        pip_install()
    elif not environment_ready():
        log("Requirements changed — updating dependencies ...")
        pip_install()
    else:
        log("Environment is ready.")


def launch_app():
    if not os.path.exists(APP):
        log(f"Cannot find escape_explorer.py next to launch.py ({APP}).")
        log("Keep launch.py, escape_explorer.py and requirements.txt in the "
            "same folder.")
        sys.exit(1)
    py = venv_python()
    log("Launching ESCApe Explorer ...")
    # Replace this process with the app where possible (clean exit codes).
    if os.name == "nt":
        sys.exit(subprocess.call([py, APP]))
    else:
        os.execv(py, [py, APP])


def main():
    args = set(sys.argv[1:])
    check_python_version()

    if "--check" in args:
        ready = environment_ready()
        log(f"venv present: {os.path.exists(venv_python())}")
        log(f"dependencies up to date: {ready}")
        check_tkinter()
        log(f"app file present: {os.path.exists(APP)}")
        return

    ensure_environment(force="--reinstall" in args)

    # tkinter lives in the base Python, not the venv; warn if missing.
    if not check_tkinter():
        log("Set up tkinter (see above) then run launch.py again.")
        if "--setup-only" not in args:
            sys.exit(1)

    if "--setup-only" in args:
        log("Setup complete. Run 'python launch.py' to start the app.")
        return

    launch_app()


if __name__ == "__main__":
    main()
