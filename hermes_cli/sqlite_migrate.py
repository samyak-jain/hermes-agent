"""Offline migration for Hermes SQLite databases.

Rollback-journal deployment on a network filesystem needs two coordinated
steps:

1. Stop every process that can hold a SQLite connection.
2. Checkpoint each persistent WAL database and move it back to rollback mode.

Runtime connections then apply DELETE or TRUNCATE through
``hermes_state.apply_sqlite_storage_policy``. This module deliberately refuses
to coordinate live processes; deployment tooling owns the drain/stop boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home

_SQLITE_HEADER = b"SQLite format 3\x00"
_DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})


def _is_sqlite_database(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def discover_sqlite_databases(
    root: Path,
    *,
    excluded_roots: Iterable[Path] = (),
) -> List[Path]:
    """Return real SQLite files below ``root`` in deterministic order."""
    root = root.resolve()
    excluded = {path.resolve() for path in excluded_roots}
    found: set[Path] = set()
    for path in root.rglob("*"):
        if path.suffix.lower() not in _DATABASE_SUFFIXES:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if any(resolved == item or item in resolved.parents for item in excluded):
            continue
        if _is_sqlite_database(path):
            found.add(path)
    return sorted(found, key=lambda item: str(item))


def _integrity_check(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    problems = [str(row[0]) for row in rows if row and str(row[0]).lower() != "ok"]
    if problems:
        raise sqlite3.DatabaseError("; ".join(problems[:10]))


def _backup_database(
    source: sqlite3.Connection,
    source_path: Path,
    *,
    home: Path,
    backup_root: Path,
) -> Path:
    try:
        relative = source_path.resolve().relative_to(home.resolve())
    except ValueError:
        relative = Path(source_path.name)
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite migration backup: {destination}")
    backup = sqlite3.connect(str(destination))
    try:
        source.backup(backup)
        _integrity_check(backup)
    finally:
        backup.close()
    try:
        os.chmod(destination, source_path.stat().st_mode & 0o777)
    except OSError:
        pass
    return destination


def migrate_database(
    path: Path,
    *,
    home: Path,
    backup_root: Path,
    target_mode: str = "truncate",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Validate, back up, and move one database out of persistent WAL mode."""
    target = str(target_mode).strip().lower()
    if target not in {"delete", "truncate"}:
        raise ValueError("target_mode must be delete or truncate")
    path = path.resolve()
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        before = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        _integrity_check(conn)
        report: Dict[str, Any] = {
            "path": str(path),
            "before": before,
            "target": target,
            "dry_run": bool(dry_run),
        }
        if dry_run:
            report["after"] = before
            report["backup"] = None
            return report

        backup = _backup_database(
            conn,
            path,
            home=home,
            backup_root=backup_root,
        )
        report["backup"] = str(backup)

        if before == "wal":
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and int(checkpoint[0]) != 0:
                raise sqlite3.OperationalError(
                    f"WAL checkpoint remained busy for {path}: {tuple(checkpoint)}"
                )
            changed = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            actual = str(changed[0]).lower() if changed else ""
            if actual != "delete":
                raise sqlite3.OperationalError(
                    f"could not leave persistent WAL mode for {path}: got {actual}"
                )

        changed = conn.execute(
            f"PRAGMA journal_mode={target.upper()}"
        ).fetchone()
        actual = str(changed[0]).lower() if changed else ""
        if actual != target:
            raise sqlite3.OperationalError(
                f"could not set journal_mode={target} for {path}: got {actual}"
            )
        conn.execute("PRAGMA synchronous=FULL")
        _integrity_check(conn)
        report["after"] = actual
        return report
    finally:
        conn.close()


def migrate_home(
    home: Path,
    *,
    target_mode: str = "truncate",
    backup_root: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Migrate every SQLite database currently present under HERMES_HOME."""
    home = home.resolve()
    migration_backup_base = home / "backups" / "sqlite-journal-migration"
    if backup_root is None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        backup_root = migration_backup_base / stamp
    backup_root = backup_root.resolve()
    databases = discover_sqlite_databases(
        home,
        excluded_roots=(migration_backup_base, backup_root),
    )
    results = [
        migrate_database(
            path,
            home=home,
            backup_root=backup_root,
            target_mode=target_mode,
            dry_run=dry_run,
        )
        for path in databases
    ]
    return {
        "home": str(home),
        "target": str(target_mode).lower(),
        "dry_run": bool(dry_run),
        "backup_root": None if dry_run else str(backup_root),
        "database_count": len(results),
        "databases": results,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline WAL-to-rollback migration for Hermes SQLite databases"
    )
    parser.add_argument("--home", type=Path, default=get_hermes_home())
    parser.add_argument(
        "--journal-mode",
        choices=("delete", "truncate"),
        default="truncate",
    )
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    report = migrate_home(
        args.home,
        target_mode=args.journal_mode,
        backup_root=args.backup_root,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
