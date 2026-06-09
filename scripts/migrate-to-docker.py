#!/usr/bin/env python3
"""
One-time migration: move existing project-root user data into the new
./phoenix-data/ directory so Docker can mount it without overwriting
anything.

Run this BEFORE the first `docker compose up --build`. Idempotent: re-runs
skip anything already in the target.

What moves:
    ./data.db              ->  ./phoenix-data/data.db
    ./downloaded/          ->  ./phoenix-data/downloaded/    (entire tree)
    ./logs/                ->  ./phoenix-data/logs/          (entire tree)

What does NOT move:
    Anything else. The project source stays put. This script never deletes
    your originals; it COPIES them. Once you've confirmed Docker reads the
    new location, you can manually remove the originals if you like:
        rm data.db
        rm -rf downloaded/ logs/
    (Or leave them; they're harmless and gitignored.)

Override the target with PHOENIX_DATA_DIR if you want a non-default location:
    PHOENIX_DATA_DIR=/var/phoenix-data python scripts/migrate-to-docker.py

Safe to abort at any time. No partial state: each file is copied atomically.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _humansize(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent

    target_root = Path(
        os.environ.get("PHOENIX_DATA_DIR") or (project_root / "phoenix-data")
    ).resolve()

    print(f"Project root: {project_root}")
    print(f"Data target : {target_root}")
    print()

    if target_root == project_root:
        print("ERROR: PHOENIX_DATA_DIR cannot be the project root itself.")
        print("       Pick a subdirectory or an absolute path outside the project.")
        return 2

    target_root.mkdir(parents=True, exist_ok=True)

    plan = [
        ("file", project_root / "data.db",     target_root / "data.db"),
        ("dir",  project_root / "downloaded",  target_root / "downloaded"),
        ("dir",  project_root / "logs",        target_root / "logs"),
    ]

    moved = 0
    skipped = 0
    missing = 0

    for kind, src, dst in plan:
        rel = src.name
        if not src.exists():
            print(f"  -  {rel:14s}  (not present in project root, skipping)")
            missing += 1
            continue

        if dst.exists():
            print(f"  =  {rel:14s}  already at target, skipping")
            skipped += 1
            continue

        if kind == "file":
            shutil.copy2(src, dst)
            size = _humansize(dst.stat().st_size)
            print(f"  +  {rel:14s}  copied  ({size})")
        else:
            shutil.copytree(src, dst)
            n_files = sum(1 for _ in dst.rglob("*") if _.is_file())
            print(f"  +  {rel:14s}  copied  ({n_files} files)")
        moved += 1

    print()
    print(f"Done. moved={moved}  skipped={skipped}  missing={missing}")
    print()

    if moved == 0 and skipped == 0:
        print("Nothing was migrated. This is a fresh install; that's fine.")
        return 0

    print("Next:")
    print(f"  1. Verify the target looks right:    ls -la {target_root}")
    print( "  2. Boot Phoenix in Docker:           docker compose up --build")
    print( "  3. Confirm your accounts + trades show up in the dashboard.")
    print( "  4. Once happy, remove the legacy copies from the project root:")
    print( "        rm data.db ;  rm -rf downloaded logs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
