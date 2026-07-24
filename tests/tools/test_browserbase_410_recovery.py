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
