"""Opt-in smoke test: COMPOSIO_API_KEY=... pytest tests/live/test_composio_live.py."""

from __future__ import annotations

import os

import pytest


@pytest.mark.skipif(not os.environ.get("COMPOSIO_API_KEY"), reason="COMPOSIO_API_KEY is not set")
def test_composio_authenticates_and_lists_toolkits():
    pytest.importorskip("composio")
    from composio import Composio

    response = Composio(api_key=os.environ["COMPOSIO_API_KEY"]).toolkits.list(
        limit=1000, managed_by="all", sort_by="alphabetically"
    )
    assert isinstance(list(response.items or []), list)
