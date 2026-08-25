import json
import sqlite3

from life_memory import LifeMemoryStore


def test_schema_uses_wal_and_expected_constraints(tmp_path):
    with LifeMemoryStore(tmp_path) as store:
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert {"sources", "chunks", "chunk_fts", "people", "person_aliases"} <= tables
        store.ensure_source("notes")
        with store._conn:
            try:
                store._conn.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("bad", "notes", "unknown", "2026-08-26T00:00:00+00:00",
                     "chunks/bad.md", "sha", "bad", "[]", "{}", "now"),
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("chunks.origin CHECK constraint was not enforced")


def test_ingest_dedupe_and_search(tmp_path):
    with LifeMemoryStore(tmp_path) as store:
        first = store.ingest(
            source="gmail",
            origin="external_sync",
            ts="2026-08-25T12:00:00Z",
            title="Project Kumo",
            body="Sam discussed the quarterly Kumo roadmap.",
        )
        duplicate = store.ingest(
            source="gmail",
            origin="external_sync",
            ts="2026-08-26T12:00:00Z",
            title="Duplicate",
            body="Sam discussed the quarterly Kumo roadmap.",
        )
        assert first["deduplicated"] is False
        assert duplicate["deduplicated"] is True
        assert duplicate["id"] == first["id"]
        assert (
            tmp_path / first["content_path"]
        ).read_text() == "Sam discussed the quarterly Kumo roadmap."
        hits = store.search("Kumo", source="gmail")
        assert [(h["id"], h["source_id"], h["origin"]) for h in hits] == [
            (first["id"], "gmail", "external_sync")
        ]


def test_alias_resolution_autocreate_and_unresolved_metadata(tmp_path):
    with LifeMemoryStore(tmp_path) as store:
        person = store.create_person(
            "Alex", [{"platform": "email", "alias": "alex@example.com"}], "alex"
        )
        hit = store.ingest(
            source="mail",
            origin="internal",
            ts="2026-08-25T12:00:00Z",
            title="Known and unknown",
            body="body",
            people_aliases=["email:alex@example.com", "slack:U404"],
        )
        assert json.loads(hit["person_ids_json"]) == [person["person_id"]]
        assert json.loads(hit["meta_json"])["unresolved_people_aliases"] == [
            {"platform": "slack", "alias": "U404"}
        ]
        created = store.ingest(
            source="mail",
            origin="internal",
            ts="2026-08-25T13:00:00Z",
            title="Autocreate",
            body="different",
            people_aliases=["slack:U123"],
            auto_create_people=True,
        )
        assert len(json.loads(created["person_ids_json"])) == 1


def test_rebuild_alias_index_from_human_edited_frontmatter(tmp_path):
    with LifeMemoryStore(tmp_path) as store:
        person = store.create_person("Alex", ["email:old@example.com"], "alex")
        path = tmp_path / "people" / "alex.md"
        path.write_text(
            "---\ndisplay_name: Alex Smith\naliases:\n  - platform: email\n    alias: new@example.com\n---\nNotes.\n"
        )
        assert store.rebuild_people_index() == {"people": 1, "aliases": 1}
        assert store.resolve_alias("old@example.com", "email") is None
        assert (
            store.resolve_alias("new@example.com", "email")["display_name"]
            == "Alex Smith"
        )
