"""Versioned wire protocol and strict validation for the workshop platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import json
import re
import time
from typing import Any, Mapping, Sequence


PROTOCOL_VERSION = 1
MAX_ACTIVE_TURNS = 4
MAX_PENDING_REMOTE_CALLS = 8
MAX_CLIENT_TOOLS = 32
MAX_SCHEMA_BYTES = 256 * 1024
MAX_TURN_SECONDS = 15 * 60
MAX_EVENT_BACKLOG_BYTES = 8 * 1024 * 1024
COMPLETED_EVENT_RETENTION_SECONDS = 24 * 60 * 60
RETRY_AFTER_SECONDS = 5

MAX_ID_LENGTH = 128
MAX_TOOL_NAME_LENGTH = 128
MAX_TOOL_DESCRIPTION_LENGTH = 8 * 1024
MAX_TURN_TEXT_BYTES = 1024 * 1024
MAX_DISPLAY_METADATA_BYTES = 16 * 1024
MAX_DISPLAY_METADATA_DEPTH = 4
MAX_DISPLAY_METADATA_NODES = 128
MAX_DISPLAY_METADATA_COLLECTION_ITEMS = 64
MAX_DISPLAY_METADATA_STRING_BYTES = 4 * 1024
MAX_DISPLAY_METADATA_KEY_BYTES = 128
MAX_DELTA_BYTES = 256 * 1024
MAX_DELTA_NODES = 4096
MAX_DELTA_DEPTH = 16
MAX_DELTA_COLLECTION_ITEMS = 256
MAX_DELTA_STRING_BYTES = 64 * 1024
MAX_SCHEMA_DEPTH = 16
MAX_SCHEMA_NODES = 4096

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")

# These names belong to the approved Hermes-local workshop policy.  Runtime
# assembly performs a second collision check against the complete local schema
# surface; ingress rejects the known boundary early with a useful 400.
HERMES_WORKSHOP_LOCAL_TOOL_NAMES = frozenset(
    {
        "clarify",
        "spawn_agent",
        "memory",
        "skills_list",
        "skill_view",
        "skill_manage",
        "session_search",
        "config",
        "soul",
    }
)

_TOOL_FIELDS = frozenset({"name", "description", "parameters"})
_TURN_FIELDS = frozenset(
    {
        "protocol_version",
        "client_turn_id",
        "workspace_id",
        "chat_id",
        "input",
        "tools",
        "metadata",
    }
)
_INPUT_FIELDS = frozenset({"type", "text"})
_CONTROL_FIELDS = frozenset({"protocol_version", "signal", "mode", "reason"})
_TOOL_RESULT_FIELDS = frozenset({"protocol_version", "result", "is_error"})
_DELTA_FIELDS = frozenset(
    {"protocol_version", "delta_id", "workspace_id", "chat_id", "payload"}
)
_DELTA_PAYLOAD_FIELDS = frozenset({"type", "version", "timestamp", "data"})

# Deliberately matches only the semantics implemented by the current
# jsonSchemaToZod converter.  Representative CF OS fixtures may justify adding
# keywords, but unknown constraints are rejected rather than silently ignored.
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "enum",
        "anyOf",
        "oneOf",
        "properties",
        "required",
        "additionalProperties",
        "items",
    }
)
SUPPORTED_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


class WorkshopProtocolError(ValueError):
    """A caller-visible protocol validation error."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class WorkshopEventType(str, Enum):
    TURN_STARTED = "turn.started"
    MESSAGE_START = "message.start"
    TEXT_DELTA = "text.delta"
    THINKING_DELTA = "thinking.delta"
    TOOL_CALL_START = "tool_call.start"
    TOOL_CALL_ARGUMENTS_DELTA = "tool_call.arguments.delta"
    TOOL_CALL_END = "tool_call.end"
    TOOL_ACTIVITY = "tool_activity"
    USAGE = "usage"
    TURN_END = "turn.end"
    ERROR = "error"


LIVE_ONLY_EVENT_TYPES = frozenset(
    {
        WorkshopEventType.THINKING_DELTA.value,
        WorkshopEventType.TOOL_CALL_ARGUMENTS_DELTA.value,
    }
)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise WorkshopProtocolError(
            "invalid_json_value", "Payload contains a non-JSON value"
        ) from exc


def _byte_length(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkshopProtocolError("invalid_request", f"{field} must be an object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise WorkshopProtocolError(
            "unknown_field", f"{field} contains unsupported field(s): {', '.join(unknown)}"
        )


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise WorkshopProtocolError(
            "invalid_identifier",
            f"{field} must be 1-{MAX_ID_LENGTH} ASCII letters, digits, '.', '_' or '-'",
        )
    return value


def validate_identifier(value: Any, field: str) -> str:
    """Validate an externally supplied routing/resource identifier."""
    return _identifier(value, field)


def _version(value: Any) -> int:
    if value != PROTOCOL_VERSION:
        raise WorkshopProtocolError(
            "unsupported_protocol_version",
            f"protocol_version must be {PROTOCOL_VERSION}",
        )
    return PROTOCOL_VERSION


def _validate_display_metadata_value(
    value: Any,
    *,
    path: str = "metadata",
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    """Bound caller display data without interpreting it as agent context."""

    if counter is None:
        counter = [0]
    counter[0] += 1
    if (
        depth > MAX_DISPLAY_METADATA_DEPTH
        or counter[0] > MAX_DISPLAY_METADATA_NODES
    ):
        raise WorkshopProtocolError(
            "metadata_too_complex", f"{path} exceeds the metadata complexity limit"
        )
    if value is None or isinstance(value, (bool, int, float)):
        canonical_json(value)
        return
    if isinstance(value, str):
        try:
            encoded_length = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise WorkshopProtocolError(
                "invalid_metadata", f"{path} contains invalid Unicode"
            ) from exc
        if encoded_length > MAX_DISPLAY_METADATA_STRING_BYTES:
            raise WorkshopProtocolError(
                "metadata_value_too_large", f"{path} string is too large"
            )
        return
    if isinstance(value, list):
        if len(value) > MAX_DISPLAY_METADATA_COLLECTION_ITEMS:
            raise WorkshopProtocolError(
                "metadata_too_complex",
                f"{path} may contain at most {MAX_DISPLAY_METADATA_COLLECTION_ITEMS} items",
            )
        for index, item in enumerate(value):
            _validate_display_metadata_value(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                counter=counter,
            )
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_DISPLAY_METADATA_COLLECTION_ITEMS:
            raise WorkshopProtocolError(
                "metadata_too_complex",
                f"{path} may contain at most {MAX_DISPLAY_METADATA_COLLECTION_ITEMS} fields",
            )
        for key, item in value.items():
            try:
                key_length = len(key.encode("utf-8")) if isinstance(key, str) else 0
            except UnicodeEncodeError as exc:
                raise WorkshopProtocolError(
                    "invalid_metadata", f"{path} contains an invalid field name"
                ) from exc
            if (
                not isinstance(key, str)
                or not key
                or key_length > MAX_DISPLAY_METADATA_KEY_BYTES
            ):
                raise WorkshopProtocolError(
                    "invalid_metadata", f"{path} contains an invalid field name"
                )
            _validate_display_metadata_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                counter=counter,
            )
        return
    raise WorkshopProtocolError("invalid_metadata", f"{path} contains a non-JSON value")


def _display_metadata(value: Any) -> dict[str, Any]:
    metadata = _mapping(value, "metadata")
    _validate_display_metadata_value(metadata)
    encoded = canonical_json(metadata).encode("utf-8")
    if len(encoded) > MAX_DISPLAY_METADATA_BYTES:
        raise WorkshopProtocolError(
            "metadata_too_large",
            f"metadata exceeds {MAX_DISPLAY_METADATA_BYTES} bytes",
            status=413,
        )
    # Normalize to an owned JSON value. This remains opaque display data and
    # is deliberately never inserted into MessageEvent or model context.
    return json.loads(encoded)


def _validate_delta_data(
    value: Any,
    *,
    path: str = "payload.data",
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    """Bound opaque workspace data without interpreting it as control."""

    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > MAX_DELTA_DEPTH or counter[0] > MAX_DELTA_NODES:
        raise WorkshopProtocolError(
            "delta_too_complex", f"{path} exceeds the delta complexity limit"
        )
    if value is None or isinstance(value, (bool, int, float)):
        canonical_json(value)
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_DELTA_STRING_BYTES:
            raise WorkshopProtocolError(
                "delta_value_too_large", f"{path} string is too large"
            )
        return
    if isinstance(value, list):
        if len(value) > MAX_DELTA_COLLECTION_ITEMS:
            raise WorkshopProtocolError(
                "delta_too_complex",
                f"{path} may contain at most {MAX_DELTA_COLLECTION_ITEMS} items",
            )
        for index, item in enumerate(value):
            _validate_delta_data(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                counter=counter,
            )
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_DELTA_COLLECTION_ITEMS:
            raise WorkshopProtocolError(
                "delta_too_complex",
                f"{path} may contain at most {MAX_DELTA_COLLECTION_ITEMS} fields",
            )
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 128:
                raise WorkshopProtocolError(
                    "invalid_delta", f"{path} contains an invalid field name"
                )
            _validate_delta_data(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                counter=counter,
            )
        return
    raise WorkshopProtocolError(
        "invalid_delta", f"{path} contains a non-JSON value"
    )


def _validate_schema(
    schema: Any,
    *,
    path: str,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > MAX_SCHEMA_DEPTH or counter[0] > MAX_SCHEMA_NODES:
        raise WorkshopProtocolError(
            "schema_too_complex", f"{path} exceeds the schema complexity limit"
        )
    if not isinstance(schema, Mapping):
        raise WorkshopProtocolError("invalid_tool_schema", f"{path} must be an object")
    unknown = sorted(str(key) for key in set(schema) - SUPPORTED_SCHEMA_KEYWORDS)
    if unknown:
        raise WorkshopProtocolError(
            "unsupported_schema_keyword",
            f"{path} uses unsupported JSON Schema keyword(s): {', '.join(unknown)}",
        )

    schema_type = schema.get("type")
    if schema_type is not None and schema_type not in SUPPORTED_SCHEMA_TYPES:
        raise WorkshopProtocolError(
            "unsupported_schema_type", f"{path}.type is not supported: {schema_type!r}"
        )
    description = schema.get("description")
    if description is not None and (
        not isinstance(description, str)
        or len(description.encode("utf-8")) > MAX_TOOL_DESCRIPTION_LENGTH
    ):
        raise WorkshopProtocolError(
            "invalid_tool_schema", f"{path}.description is invalid or too long"
        )

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise WorkshopProtocolError("invalid_tool_schema", f"{path}.enum must be non-empty")
        if any(not isinstance(item, (str, int, float, bool, type(None))) for item in enum):
            raise WorkshopProtocolError(
                "invalid_tool_schema", f"{path}.enum supports JSON scalar values only"
            )

    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if variants is None:
            continue
        if not isinstance(variants, list) or not variants:
            raise WorkshopProtocolError(
                "invalid_tool_schema", f"{path}.{union_key} must be a non-empty array"
            )
        for index, variant in enumerate(variants):
            _validate_schema(
                variant,
                path=f"{path}.{union_key}[{index}]",
                depth=depth + 1,
                counter=counter,
            )

    properties = schema.get("properties")
    if properties is not None:
        if schema_type not in (None, "object") or not isinstance(properties, Mapping):
            raise WorkshopProtocolError(
                "invalid_tool_schema", f"{path}.properties requires an object schema"
            )
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise WorkshopProtocolError(
                    "invalid_tool_schema", f"{path}.properties contains an invalid name"
                )
            _validate_schema(
                child,
                path=f"{path}.properties.{name}",
                depth=depth + 1,
                counter=counter,
            )

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or any(
            not isinstance(item, str) or not item for item in required
        ):
            raise WorkshopProtocolError(
                "invalid_tool_schema", f"{path}.required must be an array of names"
            )
        if len(set(required)) != len(required):
            raise WorkshopProtocolError(
                "invalid_tool_schema", f"{path}.required contains duplicates"
            )
        property_names = set(properties or {})
        missing = sorted(set(required) - property_names)
        if missing:
            raise WorkshopProtocolError(
                "invalid_tool_schema",
                f"{path}.required references unknown properties: {', '.join(missing)}",
            )

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        _validate_schema(
            additional,
            path=f"{path}.additionalProperties",
            depth=depth + 1,
            counter=counter,
        )

    items = schema.get("items")
    if items is not None:
        if schema_type not in (None, "array"):
            raise WorkshopProtocolError(
                "invalid_tool_schema", f"{path}.items requires an array schema"
            )
        _validate_schema(
            items,
            path=f"{path}.items",
            depth=depth + 1,
            counter=counter,
        )


@dataclass(frozen=True)
class WorkshopToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "WorkshopToolDefinition":
        value = _mapping(raw, f"tools[{index}]")
        _reject_unknown(value, _TOOL_FIELDS, f"tools[{index}]")
        name = value.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise WorkshopProtocolError(
                "invalid_tool_name",
                f"tools[{index}].name must match {_TOOL_NAME_RE.pattern}",
            )
        if name.startswith("mcp__") or name in HERMES_WORKSHOP_LOCAL_TOOL_NAMES:
            raise WorkshopProtocolError(
                "reserved_tool_name", f"Workshop tool name is reserved: {name}"
            )
        description = value.get("description", "")
        if not isinstance(description, str) or len(description.encode("utf-8")) > MAX_TOOL_DESCRIPTION_LENGTH:
            raise WorkshopProtocolError(
                "invalid_tool_description",
                f"tools[{index}].description must be at most {MAX_TOOL_DESCRIPTION_LENGTH} bytes",
            )
        input_schema = value.get("parameters")
        _validate_schema(input_schema, path=f"tools[{index}].parameters")
        if input_schema.get("type") != "object":
            raise WorkshopProtocolError(
                "invalid_tool_schema", f"tools[{index}].parameters.type must be 'object'"
            )
        return cls(name=name, description=description, input_schema=dict(input_schema))

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }

    def to_bridge_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "origin": "workshop",
        }


def parse_tool_catalog(raw: Any) -> tuple[tuple[WorkshopToolDefinition, ...], str]:
    if not isinstance(raw, list):
        raise WorkshopProtocolError("invalid_tools", "tools must be an array")
    if len(raw) > MAX_CLIENT_TOOLS:
        raise WorkshopProtocolError(
            "too_many_tools", f"tools may contain at most {MAX_CLIENT_TOOLS} entries"
        )
    tools = tuple(
        sorted(
            (WorkshopToolDefinition.from_dict(item, index=index) for index, item in enumerate(raw)),
            key=lambda tool: tool.name,
        )
    )
    names = [tool.name for tool in tools]
    if len(set(names)) != len(names):
        raise WorkshopProtocolError("duplicate_tool_name", "tools contains duplicate names")
    canonical = [tool.to_wire() for tool in tools]
    encoded = canonical_json(canonical).encode("utf-8")
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise WorkshopProtocolError(
            "tool_catalog_too_large",
            f"canonical tool catalog exceeds {MAX_SCHEMA_BYTES} bytes",
            status=413,
        )
    return tools, hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class WorkshopTurnRequest:
    client_turn_id: str
    workspace_id: str
    chat_id: str
    text: str
    tools: tuple[WorkshopToolDefinition, ...]
    catalog_version: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkshopTurnRequest":
        value = _mapping(raw, "request")
        _reject_unknown(value, _TURN_FIELDS, "request")
        _version(value.get("protocol_version"))
        turn_input = _mapping(value.get("input"), "input")
        _reject_unknown(turn_input, _INPUT_FIELDS, "input")
        if turn_input.get("type") != "user":
            raise WorkshopProtocolError("invalid_input_type", "input.type must be 'user'")
        text = turn_input.get("text")
        if not isinstance(text, str) or not text.strip():
            raise WorkshopProtocolError("invalid_input", "input.text must be a non-empty string")
        if len(text.encode("utf-8")) > MAX_TURN_TEXT_BYTES:
            raise WorkshopProtocolError("input_too_large", "input.text is too large", status=413)
        tools, digest = parse_tool_catalog(value.get("tools"))
        metadata = _display_metadata(
            value["metadata"] if "metadata" in value else {}
        )
        return cls(
            client_turn_id=_identifier(value.get("client_turn_id"), "client_turn_id"),
            workspace_id=_identifier(value.get("workspace_id"), "workspace_id"),
            chat_id=_identifier(value.get("chat_id"), "chat_id"),
            text=text,
            tools=tools,
            catalog_version=digest,
            metadata=metadata,
        )

    @property
    def request_digest(self) -> str:
        body = {
            "client_turn_id": self.client_turn_id,
            "workspace_id": self.workspace_id,
            "chat_id": self.chat_id,
            "input": {"type": "user", "text": self.text},
            "tools": [tool.to_wire() for tool in self.tools],
            "metadata": self.metadata,
        }
        return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkshopToolResultRequest:
    result: Any
    is_error: bool

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkshopToolResultRequest":
        value = _mapping(raw, "request")
        _reject_unknown(value, _TOOL_RESULT_FIELDS, "request")
        _version(value.get("protocol_version"))
        if "result" not in value:
            raise WorkshopProtocolError("invalid_tool_result", "result is required")
        is_error = value.get("is_error", False)
        if not isinstance(is_error, bool):
            raise WorkshopProtocolError("invalid_tool_result", "is_error must be a boolean")
        canonical_json(value["result"])
        return cls(result=value["result"], is_error=is_error)


@dataclass(frozen=True)
class WorkshopControlRequest:
    signal: str
    mode: str
    reason: str

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkshopControlRequest":
        value = _mapping(raw, "request")
        _reject_unknown(value, _CONTROL_FIELDS, "request")
        _version(value.get("protocol_version"))
        signal = value.get("signal")
        if signal not in {"abort", "end_turn"}:
            raise WorkshopProtocolError("invalid_control", "signal must be 'abort' or 'end_turn'")
        mode = value.get(
            "mode", "immediate" if signal == "abort" else "after_current_call"
        )
        if mode not in {"after_current_call", "immediate"}:
            raise WorkshopProtocolError(
                "invalid_control", "mode must be 'after_current_call' or 'immediate'"
            )
        if signal == "abort" and mode != "immediate":
            raise WorkshopProtocolError("invalid_control", "abort requires mode 'immediate'")
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 1024:
            raise WorkshopProtocolError(
                "invalid_control", "reason must be a non-empty string of at most 1024 bytes"
            )
        return cls(signal=signal, mode=mode, reason=reason)


@dataclass(frozen=True)
class WorkshopDeltaRequest:
    delta_id: str
    workspace_id: str
    chat_id: str
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkshopDeltaRequest":
        value = _mapping(raw, "request")
        _reject_unknown(value, _DELTA_FIELDS, "request")
        _version(value.get("protocol_version"))
        payload_raw = _mapping(value.get("payload"), "payload")
        _reject_unknown(payload_raw, _DELTA_PAYLOAD_FIELDS, "payload")
        delta_type = _identifier(payload_raw.get("type"), "payload.type")
        delta_version = payload_raw.get("version")
        if isinstance(delta_version, bool) or delta_version != 1:
            raise WorkshopProtocolError(
                "unsupported_delta_version", "payload.version must be 1"
            )
        timestamp = payload_raw.get("timestamp")
        if not isinstance(timestamp, str) or len(timestamp.encode("utf-8")) > 64:
            raise WorkshopProtocolError(
                "invalid_delta_timestamp",
                "payload.timestamp must be a timezone-aware ISO 8601 string",
            )
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorkshopProtocolError(
                "invalid_delta_timestamp",
                "payload.timestamp must be a timezone-aware ISO 8601 string",
            ) from exc
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise WorkshopProtocolError(
                "invalid_delta_timestamp",
                "payload.timestamp must include a timezone offset",
            )
        if "data" not in payload_raw:
            raise WorkshopProtocolError("invalid_delta", "payload.data is required")
        data = payload_raw["data"]
        _validate_delta_data(data)
        payload = {
            "type": delta_type,
            "version": 1,
            "timestamp": timestamp,
            "data": data,
        }
        if _byte_length(payload) > MAX_DELTA_BYTES:
            raise WorkshopProtocolError("delta_too_large", "payload is too large", status=413)
        return cls(
            delta_id=_identifier(value.get("delta_id"), "delta_id"),
            workspace_id=_identifier(value.get("workspace_id"), "workspace_id"),
            chat_id=_identifier(value.get("chat_id"), "chat_id"),
            payload=payload,
        )

    @property
    def canonical_payload(self) -> str:
        return canonical_json(self.payload)

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(self.canonical_payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkshopEvent:
    turn_id: str
    session_id: str
    seq: int
    event: str
    payload: dict[str, Any]
    timestamp: float

    @classmethod
    def create(
        cls,
        *,
        turn_id: str,
        session_id: str,
        seq: int,
        event: str | WorkshopEventType,
        payload: Mapping[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> "WorkshopEvent":
        event_name = event.value if isinstance(event, WorkshopEventType) else str(event)
        if event_name not in {member.value for member in WorkshopEventType}:
            raise WorkshopProtocolError("invalid_event_type", f"Unknown event type: {event_name}")
        if not isinstance(seq, int) or seq < 1:
            raise WorkshopProtocolError("invalid_event_sequence", "event seq must be positive")
        event_payload = dict(payload or {})
        canonical_json(event_payload)
        return cls(
            turn_id=turn_id,
            session_id=session_id,
            seq=seq,
            event=event_name,
            payload=event_payload,
            timestamp=time.time() if timestamp is None else timestamp,
        )

    @property
    def persistent(self) -> bool:
        return self.event not in LIVE_ONLY_EVENT_TYPES

    def to_wire(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "event": self.event,
            "timestamp": self.timestamp,
            **self.payload,
        }


def tool_catalog_wire(tools: Sequence[WorkshopToolDefinition]) -> list[dict[str, Any]]:
    return [tool.to_wire() for tool in tools]
