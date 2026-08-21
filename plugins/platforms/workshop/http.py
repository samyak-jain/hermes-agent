"""aiohttp route provider for the workshop turn protocol."""

from __future__ import annotations

import json
from typing import Any, Callable

from .auth import WorkshopAuthenticator
from .protocol import (
    WorkshopControlRequest,
    WorkshopDeltaRequest,
    WorkshopProtocolError,
    WorkshopToolResultRequest,
    WorkshopTurnRequest,
    validate_identifier,
)
from .storage import WorkshopLedger


def _web():
    from aiohttp import web

    return web


class WorkshopHTTPController:
    """Authenticated HTTP facade; live execution remains adapter-owned."""

    def __init__(
        self,
        api_adapter: Any,
        *,
        authenticator: WorkshopAuthenticator | None = None,
        ledger: WorkshopLedger | None = None,
    ):
        self.api_adapter = api_adapter
        self.authenticator = authenticator or WorkshopAuthenticator.from_environment()
        self.ledger = ledger or WorkshopLedger()

    def routes(self) -> list[tuple]:
        return [
            ("POST", "/api/workshop/v1/turns", self.start_turn),
            ("GET", "/api/workshop/v1/turns/{turn_id}/events", self.turn_events),
            (
                "POST",
                "/api/workshop/v1/turns/{turn_id}/tool-results/{call_id}",
                self.post_tool_result,
            ),
            ("POST", "/api/workshop/v1/turns/{turn_id}/control", self.control_turn),
            (
                "POST",
                "/api/workshop/v1/sessions/{workspace_id}/{chat_id}/deltas",
                self.post_delta,
            ),
        ]

    def _error(self, code: str, message: str, *, status: int):
        return _web().json_response(
            {"error": {"code": code, "message": message}}, status=status
        )

    def _authorize(self, request):
        if self.authenticator.authorized(request.headers.get("Authorization")):
            return None
        return self._error("unauthorized", "Invalid workshop bearer token", status=401)

    async def _json_body(self, request) -> Any:
        try:
            return await request.json()
        except Exception as exc:
            raise WorkshopProtocolError("invalid_json", "Request body must be valid JSON") from exc

    def _adapter(self, request):
        runner = request.app.get("gateway_runner")
        for platform, adapter in (getattr(runner, "adapters", {}) or {}).items():
            if str(getattr(platform, "value", platform)) == "workshop":
                return adapter
        return None

    async def _parse_and_delegate(
        self,
        request,
        parser: Callable[[Any], Any],
        method_name: str,
        *args,
    ):
        auth_error = self._authorize(request)
        if auth_error is not None:
            return auth_error
        try:
            parsed = parser(await self._json_body(request))
        except WorkshopProtocolError as exc:
            return self._error(exc.code, str(exc), status=exc.status)
        adapter = self._adapter(request)
        handler = getattr(adapter, method_name, None) if adapter is not None else None
        if not callable(handler):
            return self._error(
                "workshop_unavailable",
                "Workshop platform is not connected",
                status=503,
            )
        return await handler(*args, parsed, request=request, controller=self)

    async def start_turn(self, request):
        return await self._parse_and_delegate(
            request, WorkshopTurnRequest.from_dict, "start_workshop_turn"
        )

    async def turn_events(self, request):
        auth_error = self._authorize(request)
        if auth_error is not None:
            return auth_error
        try:
            turn_id = validate_identifier(request.match_info.get("turn_id"), "turn_id")
            raw_after = request.query.get("after_seq", "0")
            after_seq = int(raw_after)
            if after_seq < 0:
                raise ValueError
        except (WorkshopProtocolError, TypeError, ValueError) as exc:
            message = str(exc) if isinstance(exc, WorkshopProtocolError) else "after_seq must be non-negative"
            code = exc.code if isinstance(exc, WorkshopProtocolError) else "invalid_event_sequence"
            return self._error(code, message, status=400)

        adapter = self._adapter(request)
        stream = getattr(adapter, "stream_workshop_events", None) if adapter is not None else None
        if callable(stream):
            return await stream(turn_id, after_seq=after_seq, request=request, controller=self)

        # Completed turns remain replayable even while the live workshop
        # adapter is unavailable.  Active turns need the adapter's subscriber
        # tail and therefore return 503 instead of falsely closing the stream.
        turn = self.ledger.get_turn(turn_id)
        if turn is None:
            return self._error("turn_not_found", "Workshop turn not found", status=404)
        if turn.state in {"queued", "running", "ending"}:
            return self._error(
                "workshop_unavailable",
                "Workshop platform is not connected for this active turn",
                status=503,
            )
        events = self.ledger.list_events(turn_id, after_seq=after_seq)
        web = _web()
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        for event in events:
            payload = json.dumps(event.to_wire(), ensure_ascii=False, separators=(",", ":"))
            await response.write(f"event: {event.event}\ndata: {payload}\n\n".encode("utf-8"))
        await response.write_eof()
        return response

    async def post_tool_result(self, request):
        auth_error = self._authorize(request)
        if auth_error is not None:
            return auth_error
        try:
            turn_id = validate_identifier(request.match_info.get("turn_id"), "turn_id")
            call_id = validate_identifier(request.match_info.get("call_id"), "call_id")
        except WorkshopProtocolError as exc:
            return self._error(exc.code, str(exc), status=exc.status)
        return await self._parse_and_delegate(
            request,
            WorkshopToolResultRequest.from_dict,
            "resolve_workshop_tool_call",
            turn_id,
            call_id,
        )

    async def control_turn(self, request):
        auth_error = self._authorize(request)
        if auth_error is not None:
            return auth_error
        try:
            turn_id = validate_identifier(request.match_info.get("turn_id"), "turn_id")
        except WorkshopProtocolError as exc:
            return self._error(exc.code, str(exc), status=exc.status)
        return await self._parse_and_delegate(
            request,
            WorkshopControlRequest.from_dict,
            "control_workshop_turn",
            turn_id,
        )

    async def post_delta(self, request):
        auth_error = self._authorize(request)
        if auth_error is not None:
            return auth_error
        try:
            workspace_id = validate_identifier(
                request.match_info.get("workspace_id"), "workspace_id"
            )
            chat_id = validate_identifier(request.match_info.get("chat_id"), "chat_id")
        except WorkshopProtocolError as exc:
            return self._error(exc.code, str(exc), status=exc.status)
        try:
            parsed = WorkshopDeltaRequest.from_dict(await self._json_body(request))
        except WorkshopProtocolError as exc:
            return self._error(exc.code, str(exc), status=exc.status)
        if parsed.workspace_id != workspace_id or parsed.chat_id != chat_id:
            return self._error(
                "route_identity_mismatch",
                "Path workspace/chat identifiers must match the request body",
                status=409,
            )
        adapter = self._adapter(request)
        handler = getattr(adapter, "ingest_workshop_delta", None) if adapter is not None else None
        if not callable(handler):
            return self._error(
                "workshop_unavailable", "Workshop platform is not connected", status=503
            )
        return await handler(parsed, request=request, controller=self)


def create_api_routes(api_adapter: Any) -> list[tuple]:
    """PlatformEntry route factory evaluated during API-server startup."""
    return WorkshopHTTPController(api_adapter).routes()
