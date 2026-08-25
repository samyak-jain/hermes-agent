"""``hermes composio`` parser."""

from __future__ import annotations

from typing import Callable


def build_composio_parser(subparsers, *, cmd_composio: Callable) -> None:
    parser = subparsers.add_parser("composio", help="Manage Composio connections and execute actions")
    commands = parser.add_subparsers(dest="composio_action")
    commands.add_parser("toolkits", aliases=["apps"], help="List allowed available toolkits")
    commands.add_parser("connections", aliases=["list"], help="List connections and statuses")
    connect = commands.add_parser("connect", help="Start OAuth connection for an allowed app")
    connect.add_argument("app")
    connect.add_argument("--callback-url", default=None)
    delete = commands.add_parser("delete", aliases=["remove", "rm"], help="Revoke and delete a connection")
    delete.add_argument("connection_id")
    execute = commands.add_parser("execute", help="Execute an action for an allowed app")
    execute.add_argument("app")
    execute.add_argument("action_name")
    execute.add_argument("--params", default="{}", help="JSON object of action arguments")
    execute.add_argument("--connection-id", default=None)
    parser.set_defaults(func=cmd_composio)
