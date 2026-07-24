"""Behavior contracts for network-filesystem SQLite policy and migration."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import hermes_state
from hermes_cli.sqlite_migrate import migrate_home
from hermes_state import (
    SQLiteJournalMigrationRequired,
    apply_sqlite_storage_policy,
)


def _create_wal_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE entries (value TEXT NOT NULL)")
        conn.execute("INSERT INTO entries VALUES (?)", (value,))
    finally:
        conn.close()


def test_truncate_policy_is_applied_to_every_connection(tmp_path):
    path = tmp_path / "state.db"
    first = sqlite3.connect(path, isolation_level=None)
    try:
        mode = apply_sqlite_storage_policy(
            first,
            db_label="state.db",
            journal_mode="truncate",
            synchronous="full",
        )
        assert mode == "truncate"
        assert first.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"
        assert first.execute("PRAGMA synchronous").fetchone()[0] == 2
        first.execute("CREATE TABLE entries (value TEXT)")
    finally:
        first.close()

    # TRUNCATE is connection-local. Reopen returns to DELETE until the shared
    # policy is applied again.
    second = sqlite3.connect(path, isolation_level=None)
    try:
        assert second.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert (
            apply_sqlite_storage_policy(
                second,
                db_label="state.db",
                journal_mode="truncate",
                synchronous="full",
            )
            == "truncate"
        )
    finally:
        second.close()


def test_rollback_policy_refuses_live_wal_downgrade(tmp_path):
    path = tmp_path / "state.db"
    _create_wal_database(path, "preserved")

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        with pytest.raises(SQLiteJournalMigrationRequired, match="offline"):
            apply_sqlite_storage_policy(
                conn,
                db_label="state.db",
                journal_mode="truncate",
                synchronous="full",
            )
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT value FROM entries").fetchone()[0] == "preserved"
    finally:
        conn.close()


def test_memory_database_keeps_memory_journal():
    conn = sqlite3.connect(":memory:")
    try:
        assert (
            apply_sqlite_storage_policy(
                conn,
                db_label="memory",
                journal_mode="truncate",
                synchronous="full",
            )
            == "memory"
        )
    finally:
        conn.close()


def test_configured_policy_is_resolved_from_config(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "storage:\n"
        "  sqlite:\n"
        "    journal_mode: truncate\n"
        "    synchronous: full\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import config as config_module

    config_module._LOAD_CONFIG_CACHE.clear()
    config_module._RAW_CONFIG_CACHE.clear()
    hermes_state._sqlite_storage_policy_cache.clear()
    try:
        assert hermes_state._sqlite_storage_policy() == ("truncate", "full")
    finally:
        config_module._LOAD_CONFIG_CACHE.clear()
        config_module._RAW_CONFIG_CACHE.clear()
        hermes_state._sqlite_storage_policy_cache.clear()


def test_offline_migration_checkpoints_wal_backs_up_and_preserves_rows(tmp_path):
    home = tmp_path / "home"
    first = home / "state.db"
    second = home / "cron" / "executions.db"
    _create_wal_database(first, "state")
    _create_wal_database(second, "cron")

    report = migrate_home(home, target_mode="truncate")

    assert report["database_count"] == 2
    assert {item["before"] for item in report["databases"]} == {"wal"}
    assert {item["after"] for item in report["databases"]} == {"truncate"}
    backup_root = Path(report["backup_root"])
    assert (backup_root / "state.db").exists()
    assert (backup_root / "cron" / "executions.db").exists()

    for path, expected in ((first, "state"), (second, "cron")):
        conn = sqlite3.connect(path)
        try:
            # Rollback modes reopen as DELETE; runtime applies TRUNCATE to each
            # writable connection.
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT value FROM entries").fetchone()[0] == expected
        finally:
            conn.close()
        assert not path.with_name(path.name + "-wal").exists()
        assert not path.with_name(path.name + "-shm").exists()

    # Migration backups are deliberately excluded from later discovery.
    dry_run = migrate_home(home, target_mode="truncate", dry_run=True)
    assert dry_run["database_count"] == 2


def test_truncate_policy_serializes_concurrent_writers(tmp_path):
    path = tmp_path / "state.db"
    seed = sqlite3.connect(path, isolation_level=None)
    apply_sqlite_storage_policy(
        seed,
        db_label="state.db",
        journal_mode="truncate",
        synchronous="full",
    )
    seed.execute("CREATE TABLE entries (writer INTEGER, sequence INTEGER)")
    seed.close()

    connections = []
    for index in range(4):
        conn = sqlite3.connect(
            path,
            isolation_level=None,
            timeout=5,
            check_same_thread=False,
        )
        conn.execute("PRAGMA busy_timeout=5000")
        apply_sqlite_storage_policy(
            conn,
            db_label=f"state.db writer {index}",
            journal_mode="truncate",
            synchronous="full",
        )
        connections.append(conn)

    errors = []

    def write_rows(writer: int, conn: sqlite3.Connection) -> None:
        try:
            for sequence in range(25):
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO entries VALUES (?, ?)", (writer, sequence))
                conn.execute("COMMIT")
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=write_rows, args=(index, conn))
        for index, conn in enumerate(connections)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 100
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_offline_migration_recovers_committed_wal_after_process_crash(tmp_path):
    home = tmp_path / "home"
    path = home / "state.db"
    home.mkdir()
    script = (
        "import os, sqlite3, sys\n"
        "c=sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "c.execute('PRAGMA journal_mode=WAL')\n"
        "c.execute('PRAGMA wal_autocheckpoint=0')\n"
        "c.execute('CREATE TABLE entries (value TEXT)')\n"
        "c.execute(\"INSERT INTO entries VALUES ('committed-before-crash')\")\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(path)], check=True)
    assert path.with_name(path.name + "-wal").exists()

    report = migrate_home(home, target_mode="truncate")

    assert report["database_count"] == 1
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT value FROM entries").fetchone()[0] == (
            "committed-before-crash"
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()
