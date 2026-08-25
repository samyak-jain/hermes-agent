from __future__ import annotations

import argparse

from hermes_cli.subcommands.composio import build_composio_parser


def parser():
    root = argparse.ArgumentParser()
    subs = root.add_subparsers(dest="command")
    build_composio_parser(subs, cmd_composio=lambda args: 0)
    return root


def test_execute_parsing():
    args = parser().parse_args([
        "composio", "execute", "gmail", "GMAIL_GET_PROFILE",
        "--params", '{"limit": 2}', "--connection-id", "ca_1",
    ])
    assert args.composio_action == "execute"
    assert args.app == "gmail"
    assert args.action_name == "GMAIL_GET_PROFILE"
    assert args.params == '{"limit": 2}'
    assert args.connection_id == "ca_1"


def test_connection_aliases_parse():
    assert parser().parse_args(["composio", "apps"]).composio_action == "apps"
    assert parser().parse_args(["composio", "list"]).composio_action == "list"
