#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta
import os
from pathlib import Path
import pwd
import re
import subprocess
import time


KIT = Path(__file__).resolve().parents[1]
USER = "coldvault-acceptance"
LIMIT = 600


def main():
    parser = argparse.ArgumentParser(description="Run the README acceptance test in a fresh Ubuntu VM.")
    parser.add_argument("candidate", nargs="?", type=Path)
    args = parser.parse_args()
    if os.getuid() != 0:
        parser.error("run as root")
    if subprocess.run(["systemd-detect-virt", "--vm", "-q"]).returncode:
        parser.error("refusing to run outside a throwaway VM")
    try:
        pwd.getpwnam(USER)
    except KeyError:
        pass
    else:
        parser.error(f"user {USER} already exists; use a fresh VM")

    start = time.monotonic()
    readme = {line.strip() for line in (KIT / "README.md").read_text().splitlines()}

    def remaining():
        left = LIMIT - (time.monotonic() - start)
        if left <= 0:
            raise TimeoutError(f"README run exceeded {LIMIT}s")
        return left

    def run(*argv, answers=None):
        subprocess.run(argv, check=True, timeout=remaining(), input=answers, text=True)

    def step(*commands, answers=None):
        for command in commands:
            if command not in readme:
                raise ValueError(f"missing README command: {command}")
            print("+", command, flush=True)
        run("su", "-", USER, "-c", "\n".join(("set -e", *commands)), answers=answers)

    run("useradd", "-m", "-s", "/bin/bash", USER)
    home = Path(pwd.getpwnam(USER).pw_dir)
    projects = home / ".claude/projects"
    owner = ("-o", USER, "-g", USER)
    run("install", "-d", *owner, home / ".claude", projects)
    run("install", *owner, "-m", "600", KIT / "examples/transcripts/claude/demo-session.jsonl", projects)
    version = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"$', (KIT / "coldvault.py").read_text(), re.M).group(1)
    download = f"curl -fLO https://raw.githubusercontent.com/TayHCode/coldvault/v{version}/coldvault.py"
    if args.candidate is None:
        step("mkdir -p ~/coldvault && cd ~/coldvault", download)
    else:
        if download not in readme:
            raise ValueError(f"missing README command: {download}")
        print(f"+ installing candidate {args.candidate}", flush=True)
        run("install", "-d", *owner, home / "coldvault")
        run("install", *owner, "-m", "644", args.candidate.resolve(), home / "coldvault/coldvault.py")

    vault = home / "ColdvaultVault"
    step("cd ~/coldvault", "python3 coldvault.py setup", answers=str(vault) + "\n" * 8)
    step("python3 ~/coldvault/coldvault.py schedule install --vault ~/ColdvaultVault")
    history = vault / ".coldvault/runs.jsonl"
    receipt = vault / ".coldvault/last-run.json"
    before = len(history.read_bytes().splitlines())
    try:
        run("timedatectl", "set-ntp", "false")
        hour = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        run("date", "-s", (hour - timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S"))
        while True:
            lines = history.read_bytes().splitlines()
            if len(lines) > before and receipt.is_file() and lines[-1] == receipt.read_bytes().rstrip(b"\n"):
                break
            time.sleep(min(5, remaining()))
        step("python3 ~/coldvault/coldvault.py status --vault ~/ColdvaultVault")
        remaining()
    finally:
        subprocess.run(["timedatectl", "set-ntp", "true"], check=True, timeout=15)
    remaining()
    print(f"PASS: README run finished in {int(time.monotonic() - start)}s (limit {LIMIT}s)")


if __name__ == "__main__":
    main()
