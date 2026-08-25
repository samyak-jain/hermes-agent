"""Implementation of ``hermes composio``."""

from __future__ import annotations

import json
import sys

from tools.composio_client import ComposioClient, ComposioError, json_result


def composio_command(args) -> int:
    action = getattr(args, "composio_action", None)
    action = {"apps": "toolkits", "list": "connections", "remove": "delete", "rm": "delete"}.get(action, action)
    if not action:
        print("usage: hermes composio <toolkits|connect|connections|delete|execute>", file=sys.stderr)
        return 1
    try:
        client = ComposioClient()
        if action == "toolkits":
            result = client.list_toolkits()
        elif action == "connect":
            result = client.initiate_connection(args.app, callback_url=args.callback_url)
        elif action == "connections":
            result = client.list_connections()
        elif action == "delete":
            result = client.delete_connection(args.connection_id)
        elif action == "execute":
            try:
                params = json.loads(args.params)
            except json.JSONDecodeError as exc:
                raise ComposioError(f"--params must be valid JSON: {exc.msg}") from exc
            if not isinstance(params, dict):
                raise ComposioError("--params must decode to a JSON object.")
            result = client.execute(args.app, args.action_name, params, connected_account_id=args.connection_id)
        else:
            raise ComposioError(f"Unknown Composio command: {action}")
    except ComposioError as exc:
        print(f"composio: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"composio request failed: {exc}", file=sys.stderr)
        return 1
    print(json_result(result))
    return 0
