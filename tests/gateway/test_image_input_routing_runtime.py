import base64

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")}
    )
    runner.adapters = {}
    runner._pending_native_image_paths_by_session = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="273403055",
        chat_type="dm",
        user_id="42",
        user_name="Maxim",
    )


def _discord_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="123456789",
        chat_type="dm",
        user_id="42",
        user_name="Samyak",
    )


def _image_event(text: str = "look") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO,
        source=_source(),
        media_urls=["/tmp/cashback.png"],
        media_types=["image/png"],
    )


def _auto_config() -> dict:
    return {
        "agent": {"image_input_mode": "auto"},
        "auxiliary": {"vision": {"provider": "auto", "model": "", "base_url": ""}},
        "model": {"provider": "xiaomi", "default": "mimo-v2.5-pro"},
    }


@pytest.mark.asyncio
async def test_prepare_image_routing_uses_session_vision_model_override(monkeypatch):
    """Telegram /model overrides must affect native-vs-text image routing.

    Regression: _prepare_inbound_message_text used config.yaml's default model
    before the per-session model override was installed on auxiliary_client's
    runtime globals. A Telegram session switched to a vision model still had
    screenshots pre-analyzed as text when config.default was text-only.
    """
    runner = _make_runner()
    source = _source()
    event = _image_event()
    cfg = _auto_config()

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("agent.auxiliary_client._read_main_provider", lambda: "xiaomi")
    monkeypatch.setattr("agent.auxiliary_client._read_main_model", lambda: "mimo-v2.5-pro")
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **_: ("gpt-5.5", {"provider": "openai-codex"}),
    )

    def fake_supports(provider, model, config):
        return provider == "openai-codex" and model == "gpt-5.5"

    monkeypatch.setattr("agent.image_routing._lookup_supports_vision", fake_supports)

    async def fail_enrich(*_args, **_kwargs):
        pytest.fail("vision-capable session override should use native image routing")

    monkeypatch.setattr(runner, "_enrich_message_with_vision", fail_enrich)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    session_key = runner._session_key_for_source(source)
    assert result == "look"
    assert runner._pending_native_image_paths_by_session[session_key] == [
        "/tmp/cashback.png"
    ]


@pytest.mark.asyncio
async def test_prepare_image_routing_falls_back_to_text_for_text_only_session_override(monkeypatch):
    """A text-only session override should get vision_analyze text fallback.

    Regression mirror case: if config.default is a vision model but the current
    Telegram session is switched to a text-only provider (for example Mimo),
    auto routing must not attach pixels natively to the text-only model.
    """
    runner = _make_runner()
    source = _source()
    event = _image_event()
    cfg = _auto_config()
    cfg["model"] = {"provider": "openai-codex", "default": "gpt-5.5"}

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("agent.auxiliary_client._read_main_provider", lambda: "openai-codex")
    monkeypatch.setattr("agent.auxiliary_client._read_main_model", lambda: "gpt-5.5")
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **_: ("mimo-v2.5-pro", {"provider": "xiaomi"}),
    )

    def fake_supports(provider, model, config):
        return provider == "openai-codex" and model == "gpt-5.5"

    monkeypatch.setattr("agent.image_routing._lookup_supports_vision", fake_supports)

    async def fake_enrich(user_text, image_paths):
        from agent import auxiliary_client as aux

        assert user_text == "look"
        assert image_paths == ["/tmp/cashback.png"]
        runtime = aux._normalize_main_runtime(None)
        assert runtime["provider"] == "xiaomi"
        assert runtime["model"] == "mimo-v2.5-pro"
        return "[vision summary]\n\nlook"

    monkeypatch.setattr(runner, "_enrich_message_with_vision", fake_enrich)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    session_key = runner._session_key_for_source(source)
    assert result == "[vision summary]\n\nlook"
    assert runner._pending_native_image_paths_by_session.get(session_key) is None


@pytest.mark.asyncio
async def test_prepare_image_routing_runs_off_the_event_loop(monkeypatch):
    """The image-routing decision does blocking network I/O — a models.dev fetch
    on cache miss, and the Ollama ``/api/show`` capability probe for local
    servers — so it must run on a worker thread. Run inline on the gateway
    event loop it would freeze *every* session for up to the request timeout
    while a single image is routed.
    """
    import threading

    runner = _make_runner()
    source = _source()
    event = _image_event()
    cfg = _auto_config()

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("agent.auxiliary_client._read_main_provider", lambda: "xiaomi")
    monkeypatch.setattr("agent.auxiliary_client._read_main_model", lambda: "mimo-v2.5-pro")
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **_: ("gpt-5.5", {"provider": "openai-codex"}),
    )

    main_thread = threading.current_thread()
    seen: dict = {}

    def recording_supports(provider, model, config):
        # Stands in for the real, blocking capability lookup and records the
        # thread it executes on.
        seen["thread"] = threading.current_thread()
        return True  # vision-capable → native routing (skips _enrich_message_with_vision)

    monkeypatch.setattr("agent.image_routing._lookup_supports_vision", recording_supports)

    await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert seen.get("thread") is not None, "capability lookup was never reached"
    assert seen["thread"] is not main_thread, (
        "the blocking image-routing decision must be offloaded off the gateway "
        "event loop, not run inline on it"
    )


@pytest.mark.asyncio
async def test_discord_image_reaches_app_server_as_pixels(tmp_path, monkeypatch):
    """Exercise the native handoff across the gateway and app-server seams.

    The historical failure preserved only text plus an attachment marker at
    the final seam.  This test starts with a Discord photo event and proves
    the resulting turn contains a real data-URL image item.
    """
    from agent.image_routing import build_native_content_parts
    from agent.transports.codex_app_server_session import _coerce_turn_input_items

    png = tmp_path / "deterministic-red-pixel.png"
    png.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGP4DwQACfsD/QG2J3sAAAAASUVORK5CYII="
        )
    )
    source = _discord_source()
    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")}
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=[str(png)],
        media_types=["image/png"],
    )
    cfg = _auto_config()
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **_: ("claude-fable-5", {"provider": "anthropic"}),
    )
    monkeypatch.setattr(
        "agent.image_routing._lookup_supports_vision", lambda *_args: True
    )

    text = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[]
    )
    paths = runner._consume_pending_native_image_paths(
        runner._session_key_for_source(source)
    )
    rich_parts, skipped = build_native_content_parts(text, paths)
    turn_items = _coerce_turn_input_items(rich_parts)

    assert skipped == []
    assert [item["type"] for item in turn_items] == ["text", "image"]
    assert turn_items[1]["url"].startswith("data:image/png;base64,")
    assert "[image attached]" not in turn_items[0]["text"].lower()
