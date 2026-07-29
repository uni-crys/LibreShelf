from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings
from app.database import init_db
from app.database_admin import create_backup, restore_backup, verify_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(description="Librovia SQLite administration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate", help="upgrade the database to migration head")

    backup_parser = subparsers.add_parser("backup", help="create an online backup")
    backup_parser.add_argument("--output-dir", type=Path, required=True)
    backup_parser.add_argument("--keep", type=int, default=14)

    verify_parser = subparsers.add_parser("verify", help="run SQLite integrity_check")
    verify_parser.add_argument("path", type=Path, nargs="?")

    restore_parser = subparsers.add_parser("restore", help="restore a verified backup")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument(
        "--confirm",
        action="store_true",
        help="required because restore replaces the active database",
    )

    args = parser.parse_args()
    database = Path(settings.LIBROVIA_DATABASE_PATH).expanduser().resolve()

    if args.command == "migrate":
        init_db()
        print(f"Migration complete: {database}")
    elif args.command == "backup":
        path, checksum = create_backup(
            database,
            args.output_dir.expanduser().resolve(),
            args.keep,
        )
        print(f"Backup created: {path}")
        print(f"SHA-256: {checksum}")
    elif args.command == "verify":
        target = (args.path or database).expanduser().resolve()
        verify_sqlite(target)
        print(f"Integrity check passed: {target}")
    elif args.command == "restore":
        if not args.confirm:
            parser.error("restore requires --confirm")
        safety_backup = restore_backup(
            args.backup.expanduser().resolve(),
            database,
        )
        print(f"Restore complete: {database}")
        if safety_backup:
            print(f"Previous database preserved: {safety_backup}")


if __name__ == "__main__":
    main()
