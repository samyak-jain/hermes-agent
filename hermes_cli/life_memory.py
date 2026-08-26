"""CLI entry points for deterministic life-memory ingestion and index repair."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from life_memory import LifeMemoryStore


def _read_body(args) -> str:
    if args.body is not None:
        return args.body
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("provide --body, --body-file, or pipe Markdown on stdin")


def _cmd_ingest(args) -> int:
    aliases = []
    for value in args.person or []:
        platform, sep, alias = value.partition(":")
        aliases.append(
            {"platform": platform, "alias": alias}
            if sep
            else {"platform": "name", "alias": value}
        )
    meta = json.loads(args.meta_json) if args.meta_json else {}
    with LifeMemoryStore() as store:
        result = store.ingest(
            source=args.source,
            source_kind=args.source_kind,
            source_display_name=args.source_display_name,
            origin=args.origin,
            ts=args.ts,
            title=args.title,
            body=_read_body(args),
            people_aliases=aliases,
            auto_create_people=args.auto_create_people,
            meta=meta,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_rebuild_people(args) -> int:
    with LifeMemoryStore() as store:
        result = store.rebuild_people_index()
    print(json.dumps(result, indent=2))
    return 0


def register_cli(subparsers) -> None:
    parser = subparsers.add_parser(
        "life-memory", help="Ingest and maintain durable episodic life memory"
    )
    commands = parser.add_subparsers(dest="life_memory_command", required=True)
    ingest = commands.add_parser("ingest", help="Write one immutable Markdown chunk")
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--source-kind")
    ingest.add_argument("--source-display-name")
    ingest.add_argument(
        "--origin", choices=("internal", "external_sync"), required=True
    )
    ingest.add_argument("--ts", required=True, help="ISO-8601 event timestamp")
    ingest.add_argument("--title", required=True)
    body = ingest.add_mutually_exclusive_group()
    body.add_argument("--body", help="Markdown body")
    body.add_argument("--body-file", help="Read Markdown body from this file")
    ingest.add_argument(
        "--person", action="append", help="Alias as PLATFORM:VALUE (repeatable)"
    )
    ingest.add_argument("--auto-create-people", action="store_true")
    ingest.add_argument("--meta-json", help="Additional JSON object metadata")
    ingest.set_defaults(func=_cmd_ingest)
    rebuild = commands.add_parser(
        "rebuild-people-index", help="Rebuild DB aliases from person frontmatter"
    )
    rebuild.set_defaults(func=_cmd_rebuild_people)
