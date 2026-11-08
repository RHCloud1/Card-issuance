"""Create a consistent SQLite snapshot, including committed WAL transactions."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


def backup_database(source: Path, destination: Path, timeout: float = 60) -> None:
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if source == destination:
        raise ValueError("The backup destination must differ from the source database.")
    if destination.exists():
        raise FileExistsError(f"Backup destination already exists: {destination}")
    if timeout <= 0:
        raise ValueError("The backup timeout must be positive.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    deadline = time.monotonic() + timeout

    def check_deadline(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup did not finish before its deadline.")

    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(temporary_path)) as snapshot:
                original.backup(snapshot, pages=256, progress=check_deadline, sleep=0.05)
                result = snapshot.execute("PRAGMA quick_check").fetchone()
                if result != ("ok",):
                    raise RuntimeError("SQLite snapshot failed its integrity check.")
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=os.getenv("DATABASE_PATH"))
    parser.add_argument("--output", required=True, help="Snapshot path, or - for binary stdout")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if args.source is None:
        parser.error("--source or DATABASE_PATH is required")
    if args.output == "-":
        with tempfile.TemporaryDirectory(prefix="card-issuance-backup-") as directory:
            snapshot = Path(directory) / "snapshot.sqlite3"
            backup_database(args.source, snapshot, args.timeout)
            with snapshot.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    else:
        backup_database(args.source, Path(args.output), args.timeout)
        print("SQLite snapshot created.", file=sys.stderr)


if __name__ == "__main__":
    main()
