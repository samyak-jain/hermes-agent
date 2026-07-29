"""Hermetic test-runner environment contracts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_prebuilt_docker_image_crosses_hermetic_env_boundary(tmp_path: Path):
    """Keep the image tag, while still stripping credentials at ``env -i``."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    venv_bin = repo / ".venv" / "bin"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    fake_bin.mkdir()

    shutil.copy2(ROOT / "scripts" / "run_tests.sh", scripts / "run_tests.sh")
    (repo / ".venv" / "bin" / "activate").touch()

    fake_python = venv_bin / "python"
    fake_python.write_text(
        """#!/bin/sh
case "$1 $2" in
  "-c import pytest") exit 0 ;;
  "-m compileall") exit 0 ;;
esac
printf 'image=%s\\n' "${HERMES_TEST_IMAGE-unset}"
printf 'secret=%s\\n' "${OPENAI_API_KEY-unset}"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_git.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "HERMES_TEST_IMAGE": "hermes-agent:test",
            "OPENAI_API_KEY": "must-not-cross-env-i",
        }
    )
    result = subprocess.run(
        [str(scripts / "run_tests.sh"), "tests/docker/", "--jobs", "1"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "image=hermes-agent:test" in result.stdout
    assert "secret=unset" in result.stdout
