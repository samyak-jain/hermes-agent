import pytest

from tools.browser_supervisor import is_http_410_gone
from tools.browser_tool import _is_expired_cloud_session_error


def test_http_410_is_terminal_for_browserbase_session() -> None:
    session = {"bb_session_id": "session-1", "context_id": "context-1"}
    assert _is_expired_cloud_session_error(
        session, "websocket handshake failed: unexpected server response: 410"
    )


def test_410_does_not_recreate_local_session() -> None:
    assert not _is_expired_cloud_session_error(
        {"bb_session_id": None}, "HTTP 410 Gone"
    )


def test_other_cloud_errors_do_not_recreate_session() -> None:
    assert not _is_expired_cloud_session_error(
        {"bb_session_id": "session-1"}, "HTTP 503 Service Unavailable"
    )


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 410",
        "status code 410",
        "unexpected server response: 410",
        "410 Gone",
    ],
)
def test_shared_http_410_fingerprints(message: str) -> None:
    assert is_http_410_gone(message)
    assert _is_expired_cloud_session_error({"bb_session_id": "session-1"}, message)
