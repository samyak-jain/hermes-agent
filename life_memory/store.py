from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from hermes_constants import get_hermes_home
_ORIGINS = {"internal", "external_sync"}
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: str | datetime) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _component(value: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("-", value.strip()).strip(".-")
    if not cleaned or cleaned in {"..", "."}:
        raise ValueError("source must contain a safe path component")
    return cleaned[:100]


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace ``path`` using a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class LifeMemoryStore:
    """SQLite index plus immutable Markdown chunk and person files."""

    def __init__(self, home: Optional[str | Path] = None):
        self.home = Path(home).expanduser() if home else self.configured_home()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "chunks").mkdir(exist_ok=True)
        (self.home / "people").mkdir(exist_ok=True)
        self.db_path = self.home / "ingest.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    @staticmethod
    def configured_home() -> Path:
        try:
            from hermes_cli.config import load_config_readonly

            value = (load_config_readonly().get("life_memory") or {}).get("home")
            if value:
                return Path(value).expanduser()
        except Exception:
            pass
        return get_hermes_home() / "life_memory"

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, display_name TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS people (
                    person_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    notes_path TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS person_aliases (
                    alias TEXT NOT NULL COLLATE NOCASE, platform TEXT NOT NULL COLLATE NOCASE,
                    person_id TEXT NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
                    UNIQUE(alias, platform)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                    origin TEXT NOT NULL CHECK(origin IN ('internal','external_sync')),
                    ts TEXT NOT NULL, content_path TEXT NOT NULL UNIQUE,
                    content_sha256 TEXT NOT NULL, title TEXT NOT NULL,
                    person_ids_json TEXT NOT NULL DEFAULT '[]', meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, UNIQUE(source_id, content_sha256)
                );
                CREATE INDEX IF NOT EXISTS chunks_source_ts ON chunks(source_id, ts DESC);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(title, body, tokenize='unicode61');
                CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
                    DELETE FROM chunk_fts WHERE rowid = OLD.rowid;
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_fts_title AFTER UPDATE OF title ON chunks BEGIN
                    UPDATE chunk_fts SET title = NEW.title WHERE rowid = NEW.rowid;
                END;
            """)

    def ensure_source(
        self,
        source_id: str,
        *,
        kind: Optional[str] = None,
        display_name: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> dict:
        source_id = _component(source_id)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO sources VALUES (?, ?, ?, ?, ?)",
                (
                    source_id,
                    kind or source_id,
                    display_name or source_id,
                    json.dumps(config or {}, ensure_ascii=False),
                    _now(),
                ),
            )
            return dict(
                self._conn.execute(
                    "SELECT * FROM sources WHERE id=?", (source_id,)
                ).fetchone()
            )

    def create_person(
        self,
        display_name: str,
        aliases: Iterable[dict] = (),
        person_id: Optional[str] = None,
    ) -> dict:
        person_id = _component(person_id or str(uuid.uuid4()))
        normalized = self._normalize_aliases(aliases)
        path = self.home / "people" / f"{person_id}.md"
        if path.exists():
            raise FileExistsError(f"person profile already exists: {path}")
        frontmatter = {"display_name": display_name, "aliases": normalized}
        _atomic_write_text(
            path,
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
            + "---\n",
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO people VALUES (?, ?, ?, ?)",
                (person_id, display_name, str(path.relative_to(self.home)), _now()),
            )
            for item in normalized:
                self._conn.execute(
                    "INSERT INTO person_aliases VALUES (?, ?, ?)",
                    (item["alias"], item["platform"], person_id),
                )
        return {
            "person_id": person_id,
            "display_name": display_name,
            "aliases": normalized,
            "notes_path": str(path),
        }

    @staticmethod
    def _normalize_aliases(aliases: Iterable[Any]) -> list[dict]:
        out = []
        for item in aliases or ():
            if isinstance(item, str):
                platform, sep, alias = item.partition(":")
                if not sep:
                    platform, alias = "name", item
            elif isinstance(item, dict):
                platform, alias = (
                    str(item.get("platform", "name")),
                    str(item.get("alias", "")),
                )
            else:
                raise ValueError("aliases must be strings or {platform, alias} objects")
            if alias.strip():
                out.append({
                    "platform": platform.strip().lower(),
                    "alias": alias.strip(),
                })
        return out

    def resolve_alias(
        self, alias: str, platform: Optional[str] = None
    ) -> Optional[dict]:
        params: list[Any] = [alias]
        sql = "SELECT p.* FROM person_aliases a JOIN people p USING(person_id) WHERE a.alias=?"
        if platform:
            sql += " AND a.platform=?"
            params.append(platform)
        sql += " ORDER BY p.created_at LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def rebuild_people_index(self) -> dict:
        parsed = []
        for path in sorted((self.home / "people").glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
                raise ValueError(f"invalid YAML frontmatter: {path}")
            data = yaml.safe_load(raw.split("\n---\n", 1)[0][4:]) or {}
            display_name = str(data.get("display_name") or path.stem)
            parsed.append((
                path.stem,
                display_name,
                path,
                self._normalize_aliases(data.get("aliases") or []),
            ))
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM person_aliases")
            self._conn.execute("DELETE FROM people")
            for person_id, name, path, aliases in parsed:
                self._conn.execute(
                    "INSERT INTO people VALUES (?, ?, ?, ?)",
                    (person_id, name, str(path.relative_to(self.home)), _now()),
                )
                for item in aliases:
                    self._conn.execute(
                        "INSERT INTO person_aliases VALUES (?, ?, ?)",
                        (item["alias"], item["platform"], person_id),
                    )
        return {"people": len(parsed), "aliases": sum(len(x[3]) for x in parsed)}

    def ingest(
        self,
        *,
        source: str,
        origin: str,
        ts: str | datetime,
        title: str,
        body: str,
        people_aliases: Iterable[Any] = (),
        auto_create_people: bool = False,
        meta: Optional[dict] = None,
        source_kind: Optional[str] = None,
        source_display_name: Optional[str] = None,
    ) -> dict:
        if origin not in _ORIGINS:
            raise ValueError(f"origin must be one of {sorted(_ORIGINS)}")
        source_row = self.ensure_source(
            source, kind=source_kind, display_name=source_display_name
        )
        source_id, timestamp = source_row["id"], _iso(ts)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM chunks WHERE source_id=? AND content_sha256=?",
                (source_id, digest),
            ).fetchone()
        if existing:
            return {**dict(existing), "deduplicated": True}

        resolved, unresolved = [], []
        for item in self._normalize_aliases(people_aliases):
            person = self.resolve_alias(item["alias"], item["platform"])
            if not person and auto_create_people:
                person = self.create_person(item["alias"], [item])
            if person:
                if person["person_id"] not in resolved:
                    resolved.append(person["person_id"])
            else:
                unresolved.append(item)
        metadata = dict(meta or {})
        if unresolved:
            metadata["unresolved_people_aliases"] = unresolved

        chunk_id = str(uuid.uuid4())
        month = timestamp[:7]
        path = self.home / "chunks" / source_id / month / f"{chunk_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        created = _now()
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM chunks WHERE source_id=? AND content_sha256=?",
                    (source_id, digest),
                ).fetchone()
                if existing:
                    self._conn.rollback()
                    return {**dict(existing), "deduplicated": True}
                _atomic_write_text(path, body)
                cur = self._conn.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        source_id,
                        origin,
                        timestamp,
                        str(path.relative_to(self.home)),
                        digest,
                        title,
                        json.dumps(resolved),
                        json.dumps(metadata, ensure_ascii=False),
                        created,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO chunk_fts(rowid, title, body) VALUES (?, ?, ?)",
                    (cur.lastrowid, title, body),
                )
                self._conn.commit()
        except Exception:
            with self._lock:
                self._conn.rollback()
            path.unlink(missing_ok=True)
            raise
        return {
            "id": chunk_id,
            "source_id": source_id,
            "origin": origin,
            "ts": timestamp,
            "content_path": str(path.relative_to(self.home)),
            "content_sha256": digest,
            "title": title,
            "person_ids_json": json.dumps(resolved),
            "meta_json": json.dumps(metadata, ensure_ascii=False),
            "created_at": created,
            "deduplicated": False,
        }

    def _hydrate(
        self, rows: Iterable[sqlite3.Row], max_body_chars: int = 4000
    ) -> list[dict]:
        hits = []
        for row in rows:
            item = dict(row)
            body = (self.home / item["content_path"]).read_text(encoding="utf-8")
            item["body"] = body[:max_body_chars]
            item["body_truncated"] = len(body) > max_body_chars
            hits.append(item)
        return hits

    def search(
        self,
        query: str,
        *,
        source: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        clauses, params = ["chunk_fts MATCH ?"], [query]
        if source:
            clauses.append("c.source_id=?")
            params.append(source)
        if start:
            clauses.append("c.ts>=?")
            params.append(_iso(start))
        if end:
            clauses.append("c.ts<=?")
            params.append(_iso(end))
        params.append(max(1, min(int(limit), 50)))
        sql = (
            "SELECT c.*, bm25(chunk_fts) AS rank FROM chunk_fts "
            "JOIN chunks c ON c.rowid=chunk_fts.rowid WHERE "
            + " AND ".join(clauses)
            + " ORDER BY rank, c.ts DESC LIMIT ?"
        )
        with self._lock:
            return self._hydrate(self._conn.execute(sql, params).fetchall())

    def recent(
        self,
        *,
        source: Optional[str] = None,
        since: Optional[str] = None,
        person_id: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        clauses, params = ["1=1"], []
        if source:
            clauses.append("source_id=?")
            params.append(source)
        if since:
            clauses.append("ts>=?")
            params.append(_iso(since))
        if person_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(chunks.person_ids_json) WHERE value=?)"
            )
            params.append(person_id)
        params.append(max(1, min(int(limit), 50)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chunks WHERE "
                + " AND ".join(clauses)
                + " ORDER BY ts DESC LIMIT ?",
                params,
            ).fetchall()
            return self._hydrate(rows)

    def person(
        self, value: str, platform: Optional[str] = None, limit: int = 10
    ) -> Optional[dict]:
        person = self.resolve_alias(value, platform)
        with self._lock:
            if not person:
                row = self._conn.execute(
                    "SELECT * FROM people WHERE person_id=? OR display_name=? COLLATE NOCASE LIMIT 1",
                    (value, value),
                ).fetchone()
                person = dict(row) if row else None
            if not person:
                return None
            aliases = [
                dict(x)
                for x in self._conn.execute(
                    "SELECT alias, platform FROM person_aliases WHERE person_id=? ORDER BY platform, alias",
                    (person["person_id"],),
                ).fetchall()
            ]
        profile = (self.home / person["notes_path"]).read_text(encoding="utf-8")
        return {
            **person,
            "aliases": aliases,
            "profile_markdown": profile,
            "chunks": self.recent(person_id=person["person_id"], limit=limit),
        }

    def sources(self) -> list[dict]:
        with self._lock:
            return [
                dict(x)
                for x in self._conn.execute(
                    "SELECT s.*, COUNT(c.id) chunk_count, MAX(c.ts) latest_ts FROM sources s "
                    "LEFT JOIN chunks c ON c.source_id=s.id GROUP BY s.id ORDER BY s.display_name"
                ).fetchall()
            ]
