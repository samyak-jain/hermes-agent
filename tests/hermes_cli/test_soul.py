from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from agent.prompt_builder import load_soul_md
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import soul as soul_module
from hermes_cli.soul import (
    SoulConflict,
    SoulError,
    history_soul,
    read_soul,
    rollback_soul,
    update_soul,
)


POLICY = {
    "soul_edit": {
        "enabled": True,
        "require_approval": False,
        "max_bytes": 65_536,
        "read_only_profiles": [],
    }
}


@pytest.fixture()
def soul_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "SOUL.md").write_text("# Original\n\nCalm and helpful.\n", encoding="utf-8")
    return home


def _update(
    home: Path, content: str, reason: str = "operator requested change"
) -> dict:
    current = read_soul(home=home, config=POLICY)
    return update_soul(
        home=home,
        config=POLICY,
        content=content,
        expected_version=current["version"],
        reason=reason,
        actor="test",
    )


def test_default_and_named_profile_are_derived_from_fixed_home(tmp_path: Path):
    default = tmp_path / "hermes"
    named = default / "profiles" / "vegapunk"
    named.mkdir(parents=True)
    (default / "SOUL.md").write_text("default soul", encoding="utf-8")
    (named / "SOUL.md").write_text("named soul", encoding="utf-8")

    assert read_soul(home=default, config=POLICY)["profile"] == "default"
    assert read_soul(home=named, config=POLICY)["profile"] == "vegapunk"
    assert read_soul(home=named, config=POLICY)["content"] == "named soul"
    assert (default / "SOUL.md").read_text(encoding="utf-8") == "default soul"


def test_update_requires_cas_and_history_is_metadata_only(soul_home: Path):
    before = read_soul(home=soul_home, config=POLICY)
    changed = _update(soul_home, "# Revised\n\nDirect and kind.\n")

    assert changed["version"] != before["version"]
    assert changed["apply"] == "next_new_session"
    assert "No gateway restart is required" in changed["message"]
    with pytest.raises(SoulConflict, match="changed after it was read"):
        update_soul(
            home=soul_home,
            config=POLICY,
            content="# Stale writer",
            expected_version=before["version"],
            reason="stale attempt",
        )

    history = history_soul(home=soul_home)
    assert history["content_included"] is False
    assert history["retained_revisions"] == 1
    serialized = json.dumps(history)
    assert "# Original" not in serialized
    assert "# Revised" not in serialized
    audit_text = (soul_home / "state" / "soul" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert "# Original" not in audit_text
    assert "# Revised" not in audit_text
    revision_path = next((soul_home / "state" / "soul" / "revisions").iterdir())
    assert revision_path.stat().st_mode & 0o777 == 0o600


def test_update_preserves_current_prompt_snapshot_and_new_load_sees_change(
    soul_home: Path,
):
    token = set_hermes_home_override(soul_home)
    try:
        current_session_prompt = load_soul_md()
        current = read_soul(home=soul_home, config=POLICY)
        update_soul(
            home=soul_home,
            config=POLICY,
            content="# New-session identity\n\nStill safe.\n",
            expected_version=current["version"],
            reason="exercise session boundary",
        )

        assert current_session_prompt.startswith("# Original")
        assert load_soul_md().startswith("# New-session identity")
    finally:
        reset_hermes_home_override(token)


def test_rollback_can_restore_any_retained_revision(soul_home: Path):
    first = _update(soul_home, "# One\n\nFirst.\n")
    _update(soul_home, "# Two\n\nSecond.\n")
    current = read_soul(home=soul_home, config=POLICY)

    rolled_back = rollback_soul(
        home=soul_home,
        config=POLICY,
        revision=first["revision"],
        expected_version=current["version"],
        reason="return to original",
        actor="test",
    )

    assert rolled_back["rolled_back_revision"] == first["revision"]
    assert read_soul(home=soul_home, config=POLICY)["content"].startswith("# Original")


def test_creation_and_rollback_restore_absence(tmp_path: Path):
    home = tmp_path / "empty-profile"
    home.mkdir()
    before = read_soul(home=home, config=POLICY)
    assert before["version"] == "missing"

    changed = update_soul(
        home=home,
        config=POLICY,
        content="# New profile soul",
        expected_version="missing",
        reason="create identity",
    )
    rolled_back = rollback_soul(
        home=home,
        config=POLICY,
        revision=changed["revision"],
        expected_version=changed["version"],
        reason="restore absence",
    )

    assert rolled_back["exists"] is False
    assert not (home / "SOUL.md").exists()


def test_retains_only_five_snapshots(soul_home: Path):
    for index in range(7):
        _update(soul_home, f"# Revision {index}\n\nSafe content.\n")

    revisions = list((soul_home / "state" / "soul" / "revisions").glob("*.soul"))
    assert len(revisions) == 5
    assert history_soul(home=soul_home)["retained_revisions"] == 5


def test_prune_failure_does_not_undo_committed_soul(
    soul_home: Path, monkeypatch: pytest.MonkeyPatch
):
    for index in range(5):
        _update(soul_home, f"# Revision {index}\n")
    original_unlink = Path.unlink

    def fail_old_revision(path: Path, *args, **kwargs):
        if path.suffix == ".soul":
            raise PermissionError("simulated prune failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_revision)
    result = _update(soul_home, "# Revision committed despite prune failure\n")

    assert result["prune_warning"]
    assert "committed" in result["prune_warning"]
    assert (
        read_soul(home=soul_home, config=POLICY)["content"]
        == "# Revision committed despite prune failure\n"
    )


def test_rejects_symlink_and_non_regular_targets(tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("must stay unchanged", encoding="utf-8")
    home = tmp_path / "profile"
    home.mkdir()
    (home / "SOUL.md").symlink_to(outside)

    with pytest.raises(SoulError, match="symlink or non-regular"):
        read_soul(home=home, config=POLICY)
    assert outside.read_text(encoding="utf-8") == "must stay unchanged"

    (home / "SOUL.md").unlink()
    (home / "SOUL.md").mkdir()
    with pytest.raises(SoulError, match="symlink or non-regular"):
        read_soul(home=home, config=POLICY)


def test_rejects_symlinked_state_path(soul_home: Path, tmp_path: Path):
    outside = tmp_path / "outside-state"
    outside.mkdir()
    (soul_home / "state").symlink_to(outside)

    with pytest.raises(SoulError, match="symlinked profile state path"):
        _update(soul_home, "# Must not write")
    assert list(outside.iterdir()) == []


def test_rollback_rejects_symlinked_revision_without_changing_soul(
    soul_home: Path, tmp_path: Path
):
    changed = _update(soul_home, "# Current\n")
    before = (soul_home / "SOUL.md").read_bytes()
    revision_path = (
        soul_home / "state" / "soul" / "revisions" / f"{changed['revision']}.soul"
    )
    revision_path.unlink()
    outside = tmp_path / "outside-revision"
    outside.write_text("# Outside", encoding="utf-8")
    revision_path.symlink_to(outside)

    with pytest.raises(SoulError, match="revision file"):
        rollback_soul(
            home=soul_home,
            config=POLICY,
            revision=changed["revision"],
            expected_version=changed["version"],
            reason="must reject symlink",
        )
    assert (soul_home / "SOUL.md").read_bytes() == before


def test_rejects_invalid_utf8_existing_file(soul_home: Path):
    (soul_home / "SOUL.md").write_bytes(b"\xff\xfe")
    with pytest.raises(SoulError, match="not valid UTF-8"):
        read_soul(home=soul_home, config=POLICY)
    with pytest.raises(SoulError, match="not valid UTF-8"):
        update_soul(
            home=soul_home,
            config=POLICY,
            content="# Replacement",
            expected_version="sha256:unused",
            reason="attempt replacement",
        )


def test_rejects_oversize_existing_file(soul_home: Path):
    (soul_home / "SOUL.md").write_bytes(b"x" * 65_537)
    with pytest.raises(SoulError, match="configured limit"):
        read_soul(home=soul_home, config=POLICY)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("x" * 65_537, "configured limit"),
        ("\ud800", "not valid UTF-8"),
        ("", "cannot be empty"),
        ("safe\x00unsafe", "NUL"),
    ],
)
def test_rejects_invalid_or_oversize_content(soul_home: Path, content: str, match: str):
    current = read_soul(home=soul_home, config=POLICY)
    with pytest.raises(SoulError, match=match):
        update_soul(
            home=soul_home,
            config=POLICY,
            content=content,
            expected_version=current["version"],
            reason="invalid test",
        )


def test_read_only_managed_policy_blocks_mutation(soul_home: Path):
    policy = {
        "soul_edit": {
            "enabled": True,
            "require_approval": False,
            "max_bytes": 65_536,
            "read_only_profiles": ["default"],
        }
    }
    current = read_soul(home=soul_home, config=policy)
    assert current["editable"] is False
    with pytest.raises(SoulError, match="read-only"):
        update_soul(
            home=soul_home,
            config=policy,
            content="# Blocked",
            expected_version=current["version"],
            reason="must fail",
        )


def test_distribution_owned_soul_is_read_only(soul_home: Path):
    (soul_home / "distribution.yaml").write_text(
        "name: managed-persona\ndistribution_owned:\n  - SOUL.md\n",
        encoding="utf-8",
    )
    current = read_soul(home=soul_home, config=POLICY)
    assert current["ownership"] == "distribution"
    assert current["editable"] is False

    with pytest.raises(SoulError, match="owned by this profile distribution"):
        update_soul(
            home=soul_home,
            config=POLICY,
            content="# Blocked",
            expected_version=current["version"],
            reason="must fail",
        )


def test_two_concurrent_writers_with_same_version_have_one_winner(soul_home: Path):
    version = read_soul(home=soul_home, config=POLICY)["version"]
    barrier = threading.Barrier(2)
    results: list[str] = []

    def writer(label: str) -> None:
        barrier.wait()
        try:
            update_soul(
                home=soul_home,
                config=POLICY,
                content=f"# {label}\n",
                expected_version=version,
                reason=f"writer {label}",
            )
            results.append("success")
        except SoulConflict:
            results.append("conflict")

    threads = [threading.Thread(target=writer, args=(label,)) for label in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(results) == ["conflict", "success"]


def test_atomic_replace_failure_preserves_original_and_has_no_audit(
    soul_home: Path, monkeypatch: pytest.MonkeyPatch
):
    before = (soul_home / "SOUL.md").read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(SoulError, match="Atomic SOUL.md replacement failed"):
        _update(soul_home, "# Never committed")

    assert (soul_home / "SOUL.md").read_bytes() == before
    assert history_soul(home=soul_home)["count"] == 0
    revisions = soul_home / "state" / "soul" / "revisions"
    assert list(revisions.glob("*.soul")) == []


def test_audit_failure_restores_exact_original(
    soul_home: Path, monkeypatch: pytest.MonkeyPatch
):
    before = (soul_home / "SOUL.md").read_bytes()
    monkeypatch.setattr(
        soul_module,
        "_append_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("audit failed")),
    )

    with pytest.raises(SoulError, match="automatically restored"):
        _update(soul_home, "# Must roll back")

    assert (soul_home / "SOUL.md").read_bytes() == before
    assert list((soul_home / "state" / "soul" / "revisions").glob("*.soul")) == []


def test_rollback_audit_failure_restores_pre_rollback_bytes(
    soul_home: Path, monkeypatch: pytest.MonkeyPatch
):
    changed = _update(soul_home, "# Current before failed rollback\n")
    before = (soul_home / "SOUL.md").read_bytes()
    monkeypatch.setattr(
        soul_module,
        "_append_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("audit failed")),
    )

    with pytest.raises(SoulError, match="rollback was undone"):
        rollback_soul(
            home=soul_home,
            config=POLICY,
            revision=changed["revision"],
            expected_version=changed["version"],
            reason="simulate rollback audit failure",
        )

    assert (soul_home / "SOUL.md").read_bytes() == before
    history = history_soul(home=soul_home)
    assert [item["operation"] for item in history["changes"]] == ["update"]
