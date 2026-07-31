"""Contracts for required Docker integration coverage."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_PATH = Path(__file__).resolve().parents[1] / "docker" / "conftest.py"
_SPEC = importlib.util.spec_from_file_location("docker_test_conftest", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Failed to load tests/docker/conftest.py")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_prebuilt_image_fails_instead_of_skipping_without_docker(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_IMAGE", "hermes-agent:test")
    monkeypatch.setattr(_MOD, "_docker_available", lambda: False)
    monkeypatch.setattr(_MOD.shutil, "which", lambda name: f"/usr/bin/{name}")

    with pytest.raises(pytest.UsageError, match="requires an available Docker"):
        _MOD.pytest_collection_modifyitems(None, [])


def test_prebuilt_image_fails_instead_of_skipping_without_script(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_IMAGE", "hermes-agent:test")
    monkeypatch.setattr(_MOD, "_docker_available", lambda: True)
    monkeypatch.setattr(
        _MOD.shutil,
        "which",
        lambda name: None if name == "script" else f"/usr/bin/{name}",
    )

    with pytest.raises(pytest.UsageError, match="requires the `script` command"):
        _MOD.pytest_collection_modifyitems(None, [])
