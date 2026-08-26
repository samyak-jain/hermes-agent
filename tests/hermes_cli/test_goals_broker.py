from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import goals_broker as goals_module
from hermes_cli.goals_broker import (
    GoalsConflict,
    GoalsError,
    history_goals,
    read_goals,
    rollback_goals,
    update_goals,
)


POLICY = {
    "goals_edit": {
        "enabled": True,
        "require_approval": False,
        "read_only_profiles": [],
    }
}


@pytest.fixture()
def goals_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "GOALS.md").write_text("# Original\n\nCalm and helpful.\n", encoding="utf-8")
    return home


def _update(
    home: Path, content: str, reason: str = "operator requested change"
) -> dict:
    current = read_goals(home=home, config=POLICY)
    return update_goals(
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
    (default / "GOALS.md").write_text("default goals", encoding="utf-8")
    (named / "GOALS.md").write_text("named goals", encoding="utf-8")

    assert read_goals(home=default, config=POLICY)["profile"] == "default"
    assert read_goals(home=named, config=POLICY)["profile"] == "vegapunk"
    assert read_goals(home=named, config=POLICY)["content"] == "named goals"
    assert (default / "GOALS.md").read_text(encoding="utf-8") == "default goals"


def test_update_requires_cas_and_history_is_metadata_only(goals_home: Path):
    before = read_goals(home=goals_home, config=POLICY)
    changed = _update(goals_home, "# Revised\n\nDirect and kind.\n")

    assert changed["version"] != before["version"]
    assert changed["apply"] == "immediate"
    with pytest.raises(GoalsConflict, match="changed after it was read"):
        update_goals(
            home=goals_home,
            config=POLICY,
            content="# Stale writer",
            expected_version=before["version"],
            reason="stale attempt",
        )

    history = history_goals(home=goals_home)
    assert history["content_included"] is False
    assert history["retained_revisions"] == 1
    serialized = json.dumps(history)
    assert "# Original" not in serialized
    assert "# Revised" not in serialized
    audit_text = (goals_home / "state" / "goals" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert "# Original" not in audit_text
    assert "# Revised" not in audit_text
    revision_path = next((goals_home / "state" / "goals" / "revisions").iterdir())
    assert revision_path.stat().st_mode & 0o777 == 0o600


def test_rollback_can_restore_any_retained_revision(goals_home: Path):
    first = _update(goals_home, "# One\n\nFirst.\n")
    _update(goals_home, "# Two\n\nSecond.\n")
    current = read_goals(home=goals_home, config=POLICY)

    rolled_back = rollback_goals(
        home=goals_home,
        config=POLICY,
        revision=first["revision"],
        expected_version=current["version"],
        reason="return to original",
        actor="test",
    )

    assert rolled_back["rolled_back_revision"] == first["revision"]
    assert read_goals(home=goals_home, config=POLICY)["content"].startswith("# Original")


def test_creation_and_rollback_restore_absence(tmp_path: Path):
    home = tmp_path / "empty-profile"
    home.mkdir()
    before = read_goals(home=home, config=POLICY)
    assert before["version"] == "missing"
    assert before["content"] == "# Goals\n\n"

    changed = update_goals(
        home=home,
        config=POLICY,
        content="# New profile goals",
        expected_version="missing",
        reason="create identity",
    )
    rolled_back = rollback_goals(
        home=home,
        config=POLICY,
        revision=changed["revision"],
        expected_version=changed["version"],
        reason="restore absence",
    )

    assert rolled_back["exists"] is False
    assert not (home / "GOALS.md").exists()


def test_retains_only_five_snapshots(goals_home: Path):
    for index in range(7):
        _update(goals_home, f"# Revision {index}\n\nSafe content.\n")

    revisions = list((goals_home / "state" / "goals" / "revisions").glob("*.goals"))
    assert len(revisions) == 5
    assert history_goals(home=goals_home)["retained_revisions"] == 5


def test_prune_failure_does_not_undo_committed_goals(
    goals_home: Path, monkeypatch: pytest.MonkeyPatch
):
    for index in range(5):
        _update(goals_home, f"# Revision {index}\n")
    original_unlink = Path.unlink

    def fail_old_revision(path: Path, *args, **kwargs):
        if path.suffix == ".goals":
            raise PermissionError("simulated prune failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_revision)
    result = _update(goals_home, "# Revision committed despite prune failure\n")

    assert result["prune_warning"]
    assert "committed" in result["prune_warning"]
    assert (
        read_goals(home=goals_home, config=POLICY)["content"]
        == "# Revision committed despite prune failure\n"
    )


def test_rejects_symlink_and_non_regular_targets(tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("must stay unchanged", encoding="utf-8")
    home = tmp_path / "profile"
    home.mkdir()
    (home / "GOALS.md").symlink_to(outside)

    with pytest.raises(GoalsError, match="symlink or non-regular"):
        read_goals(home=home, config=POLICY)
    assert outside.read_text(encoding="utf-8") == "must stay unchanged"

    (home / "GOALS.md").unlink()
    (home / "GOALS.md").mkdir()
    with pytest.raises(GoalsError, match="symlink or non-regular"):
        read_goals(home=home, config=POLICY)


def test_rejects_symlinked_state_path(goals_home: Path, tmp_path: Path):
    outside = tmp_path / "outside-state"
    outside.mkdir()
    (goals_home / "state").symlink_to(outside)

    with pytest.raises(GoalsError, match="symlinked profile state path"):
        _update(goals_home, "# Must not write")
    assert list(outside.iterdir()) == []


def test_rollback_rejects_symlinked_revision_without_changing_goals(
    goals_home: Path, tmp_path: Path
):
    changed = _update(goals_home, "# Current\n")
    before = (goals_home / "GOALS.md").read_bytes()
    revision_path = (
        goals_home / "state" / "goals" / "revisions" / f"{changed['revision']}.goals"
    )
    revision_path.unlink()
    outside = tmp_path / "outside-revision"
    outside.write_text("# Outside", encoding="utf-8")
    revision_path.symlink_to(outside)

    with pytest.raises(GoalsError, match="revision file"):
        rollback_goals(
            home=goals_home,
            config=POLICY,
            revision=changed["revision"],
            expected_version=changed["version"],
            reason="must reject symlink",
        )
    assert (goals_home / "GOALS.md").read_bytes() == before


def test_rejects_invalid_utf8_existing_file(goals_home: Path):
    (goals_home / "GOALS.md").write_bytes(b"\xff\xfe")
    with pytest.raises(GoalsError, match="not valid UTF-8"):
        read_goals(home=goals_home, config=POLICY)
    with pytest.raises(GoalsError, match="not valid UTF-8"):
        update_goals(
            home=goals_home,
            config=POLICY,
            content="# Replacement",
            expected_version="sha256:unused",
            reason="attempt replacement",
        )


def test_rejects_oversize_existing_file(goals_home: Path):
    (goals_home / "GOALS.md").write_bytes(b"x" * 8_193)
    with pytest.raises(GoalsError, match="configured limit"):
        read_goals(home=goals_home, config=POLICY)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("x" * 8_193, "configured limit"),
        ("\ud800", "not valid UTF-8"),
        ("", "cannot be empty"),
        ("safe\x00unsafe", "NUL"),
    ],
)
def test_rejects_invalid_or_oversize_content(goals_home: Path, content: str, match: str):
    current = read_goals(home=goals_home, config=POLICY)
    with pytest.raises(GoalsError, match=match):
        update_goals(
            home=goals_home,
            config=POLICY,
            content=content,
            expected_version=current["version"],
            reason="invalid test",
        )


def test_read_only_managed_policy_blocks_mutation(goals_home: Path):
    policy = {
        "goals_edit": {
            "enabled": True,
            "require_approval": False,
            "read_only_profiles": ["default"],
        }
    }
    current = read_goals(home=goals_home, config=policy)
    assert current["editable"] is False
    with pytest.raises(GoalsError, match="read-only"):
        update_goals(
            home=goals_home,
            config=policy,
            content="# Blocked",
            expected_version=current["version"],
            reason="must fail",
        )


def test_distribution_owned_goals_is_read_only(goals_home: Path):
    (goals_home / "distribution.yaml").write_text(
        "name: managed-persona\ndistribution_owned:\n  - GOALS.md\n",
        encoding="utf-8",
    )
    current = read_goals(home=goals_home, config=POLICY)
    assert current["ownership"] == "distribution"
    assert current["editable"] is False

    with pytest.raises(GoalsError, match="owned by this profile distribution"):
        update_goals(
            home=goals_home,
            config=POLICY,
            content="# Blocked",
            expected_version=current["version"],
            reason="must fail",
        )


def test_two_concurrent_writers_with_same_version_have_one_winner(goals_home: Path):
    version = read_goals(home=goals_home, config=POLICY)["version"]
    barrier = threading.Barrier(2)
    results: list[str] = []

    def writer(label: str) -> None:
        barrier.wait()
        try:
            update_goals(
                home=goals_home,
                config=POLICY,
                content=f"# {label}\n",
                expected_version=version,
                reason=f"writer {label}",
            )
            results.append("success")
        except GoalsConflict:
            results.append("conflict")

    threads = [threading.Thread(target=writer, args=(label,)) for label in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(results) == ["conflict", "success"]


def test_atomic_replace_failure_preserves_original_and_has_no_audit(
    goals_home: Path, monkeypatch: pytest.MonkeyPatch
):
    before = (goals_home / "GOALS.md").read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(GoalsError, match="Atomic GOALS.md replacement failed"):
        _update(goals_home, "# Never committed")

    assert (goals_home / "GOALS.md").read_bytes() == before
    assert history_goals(home=goals_home)["count"] == 0
    revisions = goals_home / "state" / "goals" / "revisions"
    assert list(revisions.glob("*.goals")) == []


def test_audit_failure_restores_exact_original(
    goals_home: Path, monkeypatch: pytest.MonkeyPatch
):
    before = (goals_home / "GOALS.md").read_bytes()
    monkeypatch.setattr(
        goals_module,
        "_append_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("audit failed")),
    )

    with pytest.raises(GoalsError, match="automatically restored"):
        _update(goals_home, "# Must roll back")

    assert (goals_home / "GOALS.md").read_bytes() == before
    assert list((goals_home / "state" / "goals" / "revisions").glob("*.goals")) == []


def test_rollback_audit_failure_restores_pre_rollback_bytes(
    goals_home: Path, monkeypatch: pytest.MonkeyPatch
):
    changed = _update(goals_home, "# Current before failed rollback\n")
    before = (goals_home / "GOALS.md").read_bytes()
    monkeypatch.setattr(
        goals_module,
        "_append_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("audit failed")),
    )

    with pytest.raises(GoalsError, match="rollback was undone"):
        rollback_goals(
            home=goals_home,
            config=POLICY,
            revision=changed["revision"],
            expected_version=changed["version"],
            reason="simulate rollback audit failure",
        )

    assert (goals_home / "GOALS.md").read_bytes() == before
    history = history_goals(home=goals_home)
    assert [item["operation"] for item in history["changes"]] == ["update"]
