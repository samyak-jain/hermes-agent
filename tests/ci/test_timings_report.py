"""Behavior checks for merge-blocking CI timing metrics."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "timings_report.py"
_SPEC = importlib.util.spec_from_file_location("timings_report", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Failed to load timings_report.py")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_required_critical_path_includes_queue_and_stops_at_gate():
    timings = {
        "created_at": "2026-07-29T00:00:00Z",
        "run_started_at": "2026-07-29T00:00:02Z",
        "jobs": [
            {
                "name": "Python tests / Run tests slice 1/8",
                "conclusion": "success",
                "completed_at": "2026-07-29T00:03:40Z",
            },
            {
                "name": "All required checks pass",
                "conclusion": "success",
                "completed_at": "2026-07-29T00:04:02Z",
            },
            {
                "name": "CI timing report",
                "conclusion": "success",
                "completed_at": "2026-07-29T00:05:30Z",
            },
        ],
    }

    assert _MOD.required_critical_path_s(timings) == 240.0


def test_required_critical_path_is_unavailable_without_completed_gate():
    timings = {
        "run_started_at": "2026-07-29T00:00:00Z",
        "jobs": [
            {
                "name": "All required checks pass",
                "conclusion": "skipped",
                "completed_at": "2026-07-29T00:00:01Z",
            }
        ],
    }

    assert _MOD.required_critical_path_s(timings) is None


def test_stats_compare_required_critical_path_not_total_compute():
    def sample(gate_minute: int, worker_seconds: float) -> dict:
        return {
            "run_started_at": "2026-07-29T00:00:00Z",
            "jobs": [
                {
                    "name": "worker",
                    "conclusion": "success",
                    "started_at": "2026-07-29T00:00:10Z",
                    "completed_at": "2026-07-29T00:03:10Z",
                    "duration_s": worker_seconds,
                    "wait_s": 0,
                },
                {
                    "name": "All required checks pass",
                    "conclusion": "success",
                    "started_at": f"2026-07-29T00:0{gate_minute - 1}:55Z",
                    "completed_at": f"2026-07-29T00:0{gate_minute}:00Z",
                    "duration_s": 5,
                    "wait_s": 0,
                },
            ],
        }

    stats = _MOD.compute_stats(sample(4, 1_000), sample(5, 100))

    assert stats["critical_path"] == 240.0
    assert stats["bl_critical_path"] == 300.0
    assert stats["compute"] > stats["bl_compute"]
