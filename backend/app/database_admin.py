from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def verify_sqlite(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {result}")


def _copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with (
            sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db,
            sqlite3.connect(temporary) as destination_db,
        ):
            source_db.backup(destination_db)
        verify_sqlite(temporary)
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def create_backup(
    database: Path,
    backup_directory: Path,
    keep: int = 14,
) -> tuple[Path, str]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    verify_sqlite(database)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_directory / f"librovia-{timestamp}.sqlite3"
    _copy_database(database, destination)
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()

    backups = sorted(
        backup_directory.glob("librovia-*.sqlite3"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for expired in backups[keep:]:
        expired.unlink()
    return destination, checksum


def restore_backup(backup: Path, database: Path) -> Path | None:
    verify_sqlite(backup)
    safety_backup = None
    if database.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_backup = database.with_name(
            f"{database.stem}.pre-restore-{timestamp}{database.suffix}"
        )
        _copy_database(database, safety_backup)
    _copy_database(backup, database)
    return safety_backup
