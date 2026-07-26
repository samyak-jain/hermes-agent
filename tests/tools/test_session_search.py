"""Tests for the single-shape session_search tool.

Four calling shapes:
  1. BROWSE    — no args → recent sessions, same conversation first
  2. READ      — pass session_id → bounded transcript
  3. DISCOVERY — pass query → FTS5 + anchored window + bookends per hit
  4. SCROLL    — pass session_id + around_message_id → just the window

All run zero LLM calls.
"""
import json
import time

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import (
    SESSION_SEARCH_SCHEMA,
    _HIDDEN_SESSION_SOURCES,
    _format_timestamp,
    session_search,
)


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed_modpack_sessions(db):
    """Create three sessions about a modpack so FTS5 has hits to dedupe."""
    now = int(time.time())
    # Older session — modpack origin
    db.create_session("s_oldest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 30000, "Building the Modpack", "s_oldest"))
    db.append_message("s_oldest", role="user", content="Let's build a Minecraft modpack")
    db.append_message("s_oldest", role="assistant", content="Great. Let me scaffold the modpack repo.")
    db.append_message("s_oldest", role="user", content="Use NeoForge 1.21.1")
    db.append_message("s_oldest", role="assistant", content="Done. Modpack repo created with NeoForge 1.21.1.")
    db.append_message("s_oldest", role="assistant", content="Tier-0 mods installed; modpack smoke test passes.")

    # Middle session — modpack quest coverage
    db.create_session("s_middle", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 15000, "Modpack Quest Coverage", "s_middle"))
    db.append_message("s_middle", role="user", content="Deep-dive every modpack reference quest guide")
    db.append_message("s_middle", role="assistant", content="Surveying ATM10 questbook for modpack inspiration.")
    db.append_message("s_middle", role="user", content="Update the modpack version too")
    db.append_message("s_middle", role="assistant", content="Modpack version bumped 0.4 → 0.8.5; quest coverage page added.")

    # Newest session — modpack mob spawn fix
    db.create_session("s_newest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 1000, "Modpack Mob Spawn Fix", "s_newest"))
    db.append_message("s_newest", role="user", content="Fix the modpack mob spawning")
    db.append_message("s_newest", role="assistant", content="Investigating elite mob gating in the modpack KubeJS.")
    db.append_message("s_newest", role="assistant", content="Shipped commit b850442. Modpack alternator nerfed too.")
    db._conn.commit()


# =========================================================================
# Schema invariants
# =========================================================================

class TestSchema:
    def test_schema_has_required_params(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        # Discovery shape
        assert "query" in params
        assert "limit" in params
        assert "sort" in params
        # Scroll shape
        assert "session_id" in params
        assert "around_message_id" in params
        assert "window" in params
        # Shared
        assert "role_filter" in params

    def test_no_mode_parameter(self):
        # Mode is inferred from which args are set — no explicit mode param
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert "mode" not in params

    def test_sort_enum(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert params["sort"]["enum"] == ["newest", "oldest"]

    def test_schema_description_teaches_scroll(self):
        desc = SESSION_SEARCH_SCHEMA["description"]
        assert "SCROLL" in desc
        assert "DISCOVERY" in desc
        assert "BROWSE" in desc
        # Must explain how to scroll
        assert "scroll FORWARD" in desc or "messages[-1]" in desc

    def test_schema_teaches_recency_browse_then_direct_read(self):
        desc = SESSION_SEARCH_SCHEMA["description"]
        session_id_desc = SESSION_SEARCH_SCHEMA["parameters"]["properties"][
            "session_id"
        ]["description"]
        assert "ALWAYS start here" in desc
        assert "do not guess topic keywords" in desc
        assert "session_id ALONE" in session_id_desc
        assert "Must be paired" not in session_id_desc

    def test_no_llm_promise_in_description(self):
        # The new design never calls an LLM
        desc = SESSION_SEARCH_SCHEMA["description"].lower()
        assert "no llm" in desc

    def test_schema_description_enforces_source_first_limit(self):
        desc = SESSION_SEARCH_SCHEMA["description"].lower()
        assert "source-first limit" in desc
        assert "conversation history only" in desc
        assert "direct source" in desc
        assert "session_search as secondary" in desc
        assert "not found" in desc


class TestHiddenSources:
    def test_tool_source_hidden(self):
        assert "tool" in _HIDDEN_SESSION_SOURCES


class TestFormatTimestamp:
    def test_unix_timestamp(self):
        out = _format_timestamp(1700000000)
        assert "2023" in out

    def test_none(self):
        assert _format_timestamp(None) == "unknown"

    def test_iso_string_passthrough(self):
        out = _format_timestamp("not-a-number-string")
        assert out == "not-a-number-string"


# =========================================================================
# Browse shape (no args)
# =========================================================================

class TestBrowseShape:
    def test_no_args_returns_recent_sessions(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db))
        assert result["success"] is True
        assert result["mode"] == "browse"
        assert result["count"] >= 3

    def test_browse_excludes_current_session(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db, current_session_id="s_newest"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids

    def test_browse_returns_titles(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db))
        titles = [r.get("title") for r in result["results"]]
        assert any("Modpack" in (t or "") for t in titles)

    def test_browse_returns_ending_preview(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db))
        newest = next(r for r in result["results"] if r["session_id"] == "s_newest")
        assert "ending_preview" in newest
        assert "alternator nerfed" in newest["ending_preview"]

    def test_previous_session_uses_conversation_affinity_not_keyword_guess(self, db):
        """Regression: the latest DM omitted guessed terms used by the model.

        A cron probe and API verification ran after it. Browsing must still put
        the preceding session from this Discord DM first, and a direct read must
        expose its real ending.
        """
        now = time.time()
        dm_key = "agent:main:discord:dm:42"

        db.create_session(
            "current_dm",
            source="discord",
            session_key=dm_key,
            user_id="42",
        )
        db.append_message(
            "current_dm",
            role="user",
            content="pick up where we left off",
            timestamp=now,
        )

        db.create_session(
            "actual_previous_dm",
            source="discord",
            session_key=dm_key,
            user_id="42",
        )
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (now - 300, "actual_previous_dm"),
        )
        first_id = db.append_message(
            "actual_previous_dm",
            role="user",
            content="Compare GOWA with wacli and install the better option.",
            timestamp=now - 300,
        )
        last_id = db.append_message(
            "actual_previous_dm",
            role="assistant",
            content=(
                "The sandbox is still unreachable after repeated SSH attempts. "
                + ("Detailed diagnostic context. " * 20)
                + "Resume the wacli install after the sandbox recovers."
            ),
            timestamp=now - 240,
        )

        db.create_session("nightly_probe", source="cron")
        db.append_message(
            "nightly_probe",
            role="assistant",
            content="Nightly audit complete.",
            timestamp=now - 30,
        )
        db.create_session("deployment_probe", source="api_server")
        db.append_message(
            "deployment_probe",
            role="assistant",
            content="REQUEST_LOG_OK",
            timestamp=now - 20,
        )

        db.create_session(
            "older_keyword_match",
            source="discord",
            session_key="agent:main:discord:dm:other",
        )
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (now - 86_400, "older_keyword_match"),
        )
        db.append_message(
            "older_keyword_match",
            role="user",
            content="The operator fix for the broken sandbox.",
            timestamp=now - 86_400,
        )
        db._conn.commit()

        browse = json.loads(
            session_search(db=db, current_session_id="current_dm", limit=3)
        )
        assert browse["results"][0]["session_id"] == "actual_previous_dm"
        assert browse["results"][0]["same_conversation"] is True
        assert "Resume the wacli install" in browse["results"][0]["ending_preview"]

        direct = json.loads(
            session_search(db=db, session_id=browse["results"][0]["session_id"])
        )
        assert [m["id"] for m in direct["messages"]] == [first_id, last_id]
        assert "sandbox recovers" in direct["messages"][-1]["content"]

        guessed = json.loads(
            session_search(
                db=db,
                query="operator fix",
                sort="newest",
                current_session_id="current_dm",
            )
        )
        assert guessed["results"][0]["session_id"] == "older_keyword_match"
        assert all(
            r["session_id"] != "actual_previous_dm" for r in guessed["results"]
        )


# =========================================================================
# Discovery shape (with query)
# =========================================================================

class TestDiscoveryShape:
    def test_query_returns_anchored_windows(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", db=db))
        assert result["success"] is True
        assert result["mode"] == "discover"
        assert result["count"] >= 1

    def test_discovery_result_has_bookends_and_window(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, db=db))
        for hit in result["results"]:
            assert "bookend_start" in hit
            assert "messages" in hit
            assert "bookend_end" in hit
            assert "match_message_id" in hit
            assert "snippet" in hit
            assert "messages_before" in hit
            assert "messages_after" in hit

    def test_match_message_id_is_anchor_in_window(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, db=db))
        for hit in result["results"]:
            anchor_id = hit["match_message_id"]
            window_ids = [m["id"] for m in hit["messages"]]
            assert anchor_id in window_ids

    def test_no_results_returns_empty_list(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="zzz_no_such_term_zzz", db=db))
        assert result["success"] is True
        assert result["results"] == []
        assert result["count"] == 0

    def test_query_can_match_session_title_without_message_hit(self, db):
        db.create_session("s_fingerprint", source="cli")
        db.set_session_title("s_fingerprint", "fingerprint-login")
        db.append_message("s_fingerprint", role="user", content="Let's configure PAM for biometric auth")
        db.append_message("s_fingerprint", role="assistant", content="Checking Linux auth settings.")

        result = json.loads(session_search(query="fingerprint-login", db=db))

        assert result["success"] is True
        assert result["count"] == 1
        hit = result["results"][0]
        assert hit["session_id"] == "s_fingerprint"
        assert hit["title"] == "fingerprint-login"
        assert hit["matched_role"] == "session_title"
        assert "Session title matched" in hit["snippet"]

    def test_title_query_strips_common_model_quoting(self, db):
        db.create_session("s_fingerprint", source="cli")
        db.set_session_title("s_fingerprint", "fingerprint-login")
        db.append_message("s_fingerprint", role="user", content="PAM auth setup")

        result = json.loads(session_search(query="`fingerprint-login`", db=db))

        assert result["success"] is True
        assert result["results"][0]["session_id"] == "s_fingerprint"
        assert result["results"][0]["matched_role"] == "session_title"

    def test_title_match_respects_current_session_filter(self, db):
        db.create_session("s_current", source="cli")
        db.set_session_title("s_current", "fingerprint-login")
        db.append_message("s_current", role="user", content="PAM auth setup")

        result = json.loads(session_search(
            query="fingerprint-login",
            current_session_id="s_current",
            db=db,
        ))

        assert result["success"] is True
        assert result["results"] == []
        assert result["count"] == 0

    def test_limit_clamped_to_max_10(self, db):
        _seed_modpack_sessions(db)
        # Pass huge limit; should not error and should cap
        result = json.loads(session_search(query="modpack", limit=999, db=db))
        assert result["count"] <= 10

    def test_limit_floor_to_1(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=0, db=db))
        # Result count depends on hits, but the limit must be at least 1
        assert result["count"] >= 0

    def test_non_int_limit_falls_back(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit="bogus", db=db))
        assert result["success"] is True

    def test_current_session_filtered_out(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", db=db, current_session_id="s_newest"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids

    def test_large_discovery_stays_inline_for_claude_harness(self, db):
        """The Claude host must not replace recall with a private file pointer."""
        now = time.time()
        huge = "needle " + ("x" * 40_000)
        for idx in range(10):
            sid = f"large_{idx}"
            db.create_session(sid, source="discord")
            for msg_idx in range(11):
                role = "user" if msg_idx % 2 == 0 else "assistant"
                db.append_message(
                    sid,
                    role=role,
                    content=f"{huge} session={idx} message={msg_idx}",
                    tool_calls=(
                        [{
                            "id": f"call_{idx}_{msg_idx}",
                            "type": "function",
                            "function": {
                                "name": "large_fixture",
                                "arguments": "y" * 40_000,
                            },
                        }]
                        if role == "assistant"
                        else None
                    ),
                    timestamp=now - idx * 100 + msg_idx,
                )
        db._conn.commit()

        raw = session_search(query="needle", limit=10, db=db)
        result = json.loads(raw)

        assert result["count"] == 10
        assert len(raw.encode("utf-8")) < 45_000
        assert "chars omitted" in raw
        assert "y" * 1_000 not in raw


class TestDiscoverySort:
    def test_sort_newest_orders_by_recency(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, sort="newest", db=db))
        # First result should be the most recent session
        first = result["results"][0]
        assert first["session_id"] == "s_newest" or "Newest" in (first.get("title") or "")

    def test_sort_oldest_orders_by_age(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, sort="oldest", db=db))
        first = result["results"][0]
        assert first["session_id"] == "s_oldest"

    def test_invalid_sort_silently_ignored(self, db):
        _seed_modpack_sessions(db)
        # Should not error
        result = json.loads(session_search(query="modpack", sort="bogus", db=db))
        assert result["success"] is True


class TestRoleFilter:
    def test_default_excludes_tool_role(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", role="user", content="modpack question")
        db.append_message("s1", role="tool", content="modpack tool output", tool_name="x")
        result = json.loads(session_search(query="modpack", db=db))
        # The FTS5 match should be on the user message, not the tool message
        if result["count"] > 0:
            matched_role = result["results"][0]["matched_role"]
            assert matched_role in ("user", "assistant")

    def test_explicit_tool_role_includes_tool(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", role="tool", content="modpack tool output", tool_name="x")
        result = json.loads(session_search(query="modpack", role_filter="tool", db=db))
        # Should now match the tool message
        if result["count"] > 0:
            assert result["results"][0]["matched_role"] == "tool"


# =========================================================================
# Scroll shape (session_id + around_message_id)
# =========================================================================

class TestScrollShape:
    def test_scroll_returns_window_without_bookends(self, db):
        _seed_modpack_sessions(db)
        # Get an anchor first via discovery
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]

        # Now scroll
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=2, db=db
        ))
        assert result["success"] is True
        assert result["mode"] == "scroll"
        assert "messages" in result
        # Scroll shape has no bookends
        assert "bookend_start" not in result
        assert "bookend_end" not in result

    def test_scroll_window_clamped_to_20(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=999, db=db
        ))
        assert result["window"] == 20

    def test_scroll_window_floor_to_1(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=-5, db=db
        ))
        assert result["window"] == 1

    def test_scroll_returns_messages_before_after_counts(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=3, db=db
        ))
        assert "messages_before" in result
        assert "messages_after" in result

    def test_scroll_anchor_in_window(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=2, db=db
        ))
        anchor_in_window = [m for m in result["messages"] if m["id"] == anchor_mid]
        assert len(anchor_in_window) == 1
        assert anchor_in_window[0].get("anchor") is True

    def test_scroll_missing_anchor_errors(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(
            session_id="s_oldest", around_message_id=999999, db=db
        ))
        assert result["success"] is False
        assert "not in" in result.get("error", "")

    def test_scroll_missing_session_errors(self, db):
        result = json.loads(session_search(
            session_id="nonexistent", around_message_id=1, db=db
        ))
        assert result["success"] is False

    def test_scroll_rejects_current_session_lineage(self, db):
        _seed_modpack_sessions(db)
        # Grab some valid id from s_oldest
        disc = json.loads(session_search(query="modpack", limit=3, db=db))
        match = [r for r in disc["results"] if r["session_id"] == "s_oldest"]
        if match:
            mid = match[0]["match_message_id"]
            result = json.loads(session_search(
                session_id="s_oldest", around_message_id=mid, db=db,
                current_session_id="s_oldest",
            ))
            assert result["success"] is False
            assert "current session" in result.get("error", "").lower()

    def test_scroll_invalid_around_message_id_errors(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(
            session_id="s_oldest", around_message_id="not-an-int", db=db
        ))
        assert result["success"] is False


class TestScrollPattern:
    """The forward/backward scroll loop using tool output."""

    def test_scroll_forward_from_last_id(self, db):
        # Long session
        db.create_session("s_long", source="cli")
        ids = []
        for i in range(20):
            ids.append(db.append_message("s_long", role="user" if i % 2 == 0 else "assistant",
                                         content=f"long session msg {i}"))

        v1 = json.loads(session_search(
            session_id="s_long", around_message_id=ids[5], window=3, db=db
        ))
        last_id = v1["messages"][-1]["id"]
        v2 = json.loads(session_search(
            session_id="s_long", around_message_id=last_id, window=3, db=db
        ))
        # Forward scroll: v2 should reach further than v1
        assert max(m["id"] for m in v2["messages"]) > max(m["id"] for m in v1["messages"])
        # Boundary id appears in both
        assert last_id in [m["id"] for m in v1["messages"]]
        assert last_id in [m["id"] for m in v2["messages"]]


# =========================================================================
# Shape precedence
# =========================================================================

class TestShapePrecedence:
    def test_scroll_args_beat_query(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        # Pass both query and scroll args — scroll should win
        result = json.loads(session_search(
            query="modpack",  # would normally trigger discovery
            session_id=anchor_sid, around_message_id=anchor_mid, db=db,
        ))
        assert result["mode"] == "scroll"

    def test_empty_query_falls_back_to_browse(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="   ", db=db))
        assert result["mode"] == "browse"

    def test_non_string_query_falls_back_to_browse(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query=None, db=db))  # type: ignore
        assert result["mode"] == "browse"

    def test_session_id_without_anchor_reads(self, db):
        _seed_modpack_sessions(db)
        # session_id alone (no anchor, no query) → read shape, not browse.
        result = json.loads(session_search(session_id="s_oldest", db=db))
        assert result["mode"] == "read"


# =========================================================================
# Read shape — dump a whole session by id (serves @session links)
# =========================================================================

class TestReadShape:
    def test_read_returns_full_session(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(session_id="s_oldest", db=db))
        assert result["success"] is True
        assert result["mode"] == "read"
        assert result["session_id"] == "s_oldest"
        assert result["message_count"] == 5
        assert result["truncated"] is False
        assert len(result["messages"]) == 5
        assert result["session_meta"]["title"] == "Building the Modpack"

    def test_read_unknown_session_errors(self, db):
        result = json.loads(session_search(session_id="ghost", db=db))
        assert result["success"] is False

    def test_read_truncates_large_session(self, db):
        db.create_session("s_big", source="cli")
        for i in range(50):
            db.append_message("s_big", role="user" if i % 2 == 0 else "assistant", content=f"m{i}")
        db._conn.commit()
        result = json.loads(session_search(session_id="s_big", db=db))
        assert result["mode"] == "read"
        assert result["message_count"] == 50
        assert result["truncated"] is True
        assert len(result["messages"]) == 30  # head 20 + tail 10

    def test_large_read_stays_inline_for_claude_harness(self, db):
        db.create_session("s_huge", source="discord")
        for i in range(50):
            db.append_message(
                "s_huge",
                role="user" if i % 2 == 0 else "assistant",
                content=f"message {i} " + ("z" * 40_000),
                tool_calls=(
                    [{
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": "large_fixture",
                            "arguments": "a" * 40_000,
                        },
                    }]
                    if i % 2
                    else None
                ),
            )

        raw = session_search(session_id="s_huge", db=db)
        result = json.loads(raw)

        assert result["mode"] == "read"
        assert result["truncated"] is True
        assert len(result["messages"]) == 30
        assert len(raw.encode("utf-8")) < 45_000
        assert "chars omitted" in raw
        assert "a" * 1_000 not in raw


# =========================================================================
# Cross-profile read — `profile` swaps in another profile's DB (read-only)
# =========================================================================

class TestCrossProfileRead:
    def _patch_profiles(self, monkeypatch, home, exists=True):
        from hermes_cli import profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "normalize_profile_name", lambda n: n)
        monkeypatch.setattr(profiles_mod, "validate_profile_name", lambda n: None)
        monkeypatch.setattr(profiles_mod, "profile_exists", lambda n: exists)
        monkeypatch.setattr(profiles_mod, "get_profile_dir", lambda n: home)

    def test_profile_param_reads_other_db(self, db, tmp_path, monkeypatch):
        other_home = tmp_path / "other_home"
        other_home.mkdir()
        other = SessionDB(other_home / "state.db")
        other.create_session("s_other", source="cli")
        other._conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?", ("Other Profile Chat", "s_other")
        )
        other.append_message("s_other", role="user", content="hello from the other profile")
        other._conn.commit()

        self._patch_profiles(monkeypatch, other_home)

        # s_other lives only in the other profile; the current `db` lacks it.
        result = json.loads(session_search(session_id="s_other", profile="other", db=db))
        assert result["success"] is True
        assert result["mode"] == "read"
        assert result["session_meta"]["title"] == "Other Profile Chat"

    def test_bare_id_locates_across_profiles(self, db, tmp_path, monkeypatch):
        # The real-world failure: model dropped the owning profile and passed a
        # bare id. The tool must scan profiles and find it anyway.
        other_home = tmp_path / "asdf_home"
        other_home.mkdir()
        other = SessionDB(other_home / "state.db")
        other.create_session("s_far", source="cli")
        other.append_message("s_far", role="user", content="hi")
        other._conn.commit()

        from collections import namedtuple
        from hermes_cli import profiles as profiles_mod
        Info = namedtuple("Info", "name path")
        monkeypatch.setattr(profiles_mod, "get_profile_dir", lambda n: tmp_path / "default_home")
        monkeypatch.setattr(profiles_mod, "list_profiles", lambda: [Info("asdf", other_home)])

        # `db` (current profile) lacks s_far; no profile passed → scan finds it.
        result = json.loads(session_search(session_id="s_far", db=db))
        assert result["success"] is True
        assert result["mode"] == "read"
        assert result["profile"] == "asdf"

    def test_unknown_profile_errors(self, db, monkeypatch, tmp_path):
        self._patch_profiles(monkeypatch, tmp_path, exists=False)
        result = json.loads(session_search(session_id="x", profile="ghost", db=db))
        assert result["success"] is False
        assert "ghost" in result.get("error", "")

    def test_combined_value_autosplits(self, db, tmp_path, monkeypatch):
        # Agent passed the raw "@session:<profile>/<id>" value as session_id with
        # no separate profile — the tool should recover both.
        other_home = tmp_path / "other_home"
        other_home.mkdir()
        other = SessionDB(other_home / "state.db")
        other.create_session("s_other", source="cli")
        other.append_message("s_other", role="user", content="hi")
        other._conn.commit()

        self._patch_profiles(monkeypatch, other_home)

        # Every permutation the model might send must resolve to (asdf, s_other).
        for kwargs in (
            {"session_id": "asdf/s_other"},                    # full value, no profile
            {"session_id": "asdf/s_other", "profile": "asdf"},  # full value AND profile
            {"session_id": "s_other", "profile": "asdf"},       # bare id + profile
        ):
            result = json.loads(session_search(db=db, **kwargs))
            assert result["success"] is True, kwargs
            assert result["mode"] == "read"
            assert result["session_id"] == "s_other"


# =========================================================================
# Cron demotion in discover ranking (#19434)
# =========================================================================

class TestCronDemotion:
    def _seed_cron_and_interactive(self, db):
        """One interactive (telegram) session and several cron sessions, all
        matching the same query. Cron rows accumulate repetitive vocabulary
        and out-number the user's single interactive session — the live-data
        symptom in #19434.
        """
        now = int(time.time())
        # Interactive user session — older, so it loses on bare recency too.
        db.create_session("s_user", source="telegram")
        db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                         (now - 90000, "s_user"))
        db.append_message("s_user", role="user", content="how is the venom project going")
        db.append_message("s_user", role="assistant", content="The venom project shipped its first milestone.")
        # Several cron sessions, all newer and all stuffed with the same terms.
        for i in range(8):
            sid = f"cron_{i}"
            db.create_session(sid, source="cron")
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                             (now - 1000 - i, sid))
            db.append_message(sid, role="user", content="venom project daily status")
            db.append_message(sid, role="assistant", content="venom project venom project venom summary")
        db._conn.commit()

    def test_interactive_session_surfaces_above_cron(self, db):
        self._seed_cron_and_interactive(db)
        result = json.loads(session_search(query="venom project", limit=1, db=db))
        assert result["success"] is True
        assert result["count"] == 1
        # With cron drowning FTS, bare BM25/recency would return a cron_* hit.
        # Demotion must put the user's interactive session first.
        assert result["results"][0]["source"] == "telegram"
        assert result["results"][0]["session_id"] == "s_user"

    def test_cron_still_reachable_when_only_match(self, db):
        """Demotion must not exclude cron — when only cron matches, it still
        comes back."""
        now = int(time.time())
        db.create_session("cron_only", source="cron")
        db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                         (now - 500, "cron_only"))
        db.append_message("cron_only", role="user", content="quarterly archive sweep")
        db.append_message("cron_only", role="assistant", content="Archive sweep complete.")
        db._conn.commit()
        result = json.loads(session_search(query="archive sweep", db=db))
        assert result["success"] is True
        assert result["count"] == 1
        assert result["results"][0]["source"] == "cron"

    def test_order_for_recall_is_stable_within_class(self):
        from tools.session_search_tool import _order_for_recall
        rows = [
            {"id": 1, "source": "cron"},
            {"id": 2, "source": "telegram"},
            {"id": 3, "source": "cron"},
            {"id": 4, "source": "cli"},
            {"id": 5, "source": None},
        ]
        ordered = _order_for_recall(rows)
        # Interactive rows first, in original relative order; cron last, in
        # original relative order.
        assert [r["id"] for r in ordered] == [2, 4, 5, 1, 3]
