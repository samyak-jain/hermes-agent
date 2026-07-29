"""Static contracts for CI gating, architecture, and publication safety."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> dict:
    with (ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_required_aggregate_waits_for_docker_and_fails_closed():
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = _workflow("ci.yml")
    gate = workflow["jobs"]["all-checks-pass"]

    assert "docker" in gate["needs"]

    command = gate["steps"][0]["run"]
    assert "accepted = {'success', 'skipped'}" in command
    assert "not in accepted" in command


def test_docker_prs_use_native_runners_without_publication_permissions():
    workflow = _workflow("docker.yml")
    build = workflow["jobs"]["build"]
    include = build["strategy"]["matrix"]["include"]
    by_arch = {entry["arch"]: entry for entry in include}

    assert by_arch["amd64"]["runner"] == "ubuntu-latest"
    assert by_arch["arm64"]["runner"] == "ubuntu-24.04-arm"
    assert all(
        "setup-qemu-action" not in str(step.get("uses", ""))
        for step in build["steps"]
    )
    assert workflow["permissions"] == {"contents": "read"}

    push_steps = [
        step
        for step in build["steps"]
        if step.get("name", "").startswith("Push ")
    ]
    assert push_steps
    assert all(
        "pull_request" not in step["if"]
        and "github.repository == 'NousResearch/hermes-agent'" not in step["if"]
        for step in push_steps
    )

    docker_test = next(
        step
        for step in build["steps"]
        if step.get("name") == "Run docker integration tests"
    )
    assert docker_test["env"]["HERMES_TEST_IMAGE"] == "${{ env.IMAGE_NAME }}:test"
    assert "scripts/run_tests.sh tests/docker/" in docker_test["run"]
