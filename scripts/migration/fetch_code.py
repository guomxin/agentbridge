"""Fetch the reviewed migration commit from GitHub without overwriting local work."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

REMOTE = "https://github.com/guomxin/cli-helper.git"
ALLOWED_REMOTES = {REMOTE, "git@github.com:guomxin/cli-helper.git"}


def run(destination, *args):
    return subprocess.check_output(["git", "-C", str(destination), *args], text=True, encoding="utf-8").strip()


def fetch_code(destination: Path, commit: str):
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ValueError("Use the full 40-character commit from the source handoff")
    destination = destination.expanduser().absolute()
    if destination.exists():
        if destination.is_symlink() or not (destination / ".git").is_dir():
            raise ValueError("Existing destination must be a standalone Git checkout")
        if Path(run(destination, "rev-parse", "--show-toplevel")).resolve() != destination.resolve():
            raise ValueError("Destination is not the Git root")
        if run(destination, "remote", "get-url", "origin") not in ALLOWED_REMOTES:
            raise ValueError("Unexpected origin URL")
        if run(destination, "status", "--porcelain"):
            raise ValueError("Local modifications exist; preserve them before fetching the migration baseline")
        ahead = run(destination, "rev-list", "--count", "HEAD", "--not", "--remotes")
        if ahead != "0":
            raise ValueError("Local-only commits exist; preserve the branch before switching")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", REMOTE, str(destination)], check=True)
    run(destination, "fetch", "origin", commit)
    if run(destination, "rev-parse", "FETCH_HEAD") != commit.lower():
        raise ValueError("Fetched commit mismatch")
    run(destination, "checkout", "--detach", commit)
    if run(destination, "rev-parse", "HEAD") != commit.lower():
        raise ValueError("Checkout mismatch")
    print(f"Verified migration code at {commit.lower()}; detached baseline, no environment activated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    fetch_code(args.destination, args.commit)


if __name__ == "__main__":
    main()
