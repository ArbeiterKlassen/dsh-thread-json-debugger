#!/usr/bin/env python3
"""Diagnose a DSH session log: structure first, cost second.

Use this when a thread will not open, a request fails before the model is even
called, or input tokens/growth look wrong. Everything here is read-only.

Exit codes: 0 clean, 1 structural problems found, 2 no session found.

Usage:
    python scan_session.py
    python scan_session.py --session 94811e6d --json
    python scan_session.py --all --limit 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import dshsession as S


def human(size) -> str:
    value = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GiB"


def scan(path: str) -> dict:
    raw = S.read_bytes(path)
    lines, events, parse_errors = S.load_events(path)
    report = S.validate(events, parse_errors, lines)
    header = events[0] if events and events[0] else {}
    objects = [e for e in events if e and isinstance(e.get("seq"), int)]

    sizes = []
    for index, line in enumerate(lines):
        if index == 0:
            continue
        sizes.append({"line": index + 1, "bytes": len(line)})
    sizes.sort(key=lambda row: -row["bytes"])

    histogram = Counter(e.get("type") for e in objects)
    offloads = [e for e in objects if e.get("type") == "image/offload"]
    retained_images = 0
    nodes = S.surface_nodes(objects)
    offloaded = S.offloaded_indexes(objects)
    for event in objects:
        if event["seq"] not in nodes:
            continue
        retained_images += sum(1 for index, _ in S.image_parts(event) if index not in offloaded.get(event["seq"], set()))

    return {
        "session": path,
        "sessionId": S.session_id(path),
        "bytes": len(raw),
        "frames": S.frame_count(raw),
        "lines": len(lines),
        "header": {
            "id": header.get("id"),
            "version": header.get("version"),
            "cwd": header.get("cwd"),
            "createdAt": header.get("createdAt"),
            "parentSession": header.get("parentSession"),
            "isSeeded": header.get("isSeeded"),
            "delegationDepth": header.get("delegationDepth"),
            "agentPreset": header.get("agentPreset"),
        },
        "events": report["events"],
        "surfaceNodes": report["nodes"],
        "lastSeq": report["last_seq"],
        "problems": report["problems"],
        "warnings": report["warnings"],
        "parseErrors": parse_errors,
        "eventTypes": dict(histogram.most_common()),
        "imageOffloadEvents": len(offloads),
        "retainedImages": retained_images,
        "biggestEvents": sizes[:10],
        "tail": [f"{e.get('seq')}:{e.get('type')}" for e in objects[-5:]],
        "endsWithNewline": S.decompress(raw).endswith(b"\n"),
    }


def print_report(result: dict) -> None:
    header = result["header"]
    print(f"session     {result['sessionId']}")
    print(f"log         {result['session']}")
    print(f"            {human(result['bytes'])} compressed, {result['frames']} zstd frame(s), "
          f"{result['lines']} line(s), {result['events']} event(s), trailing newline: {result['endsWithNewline']}")
    print(f"header      v{header['version']} cwd={header['cwd']} preset={header['agentPreset']} "
          f"depth={header['delegationDepth']} seeded={header['isSeeded']}")
    print(f"surface     {result['surfaceNodes']} node(s), last seq {result['lastSeq']}, tail "
          f"{', '.join(result['tail'])}")
    print(f"images      {result['imageOffloadEvents']} image/offload event(s), "
          f"{result['retainedImages']} image occurrence(s) still replayed")
    print("types       " + ", ".join(f"{k}×{v}" for k, v in list(result["eventTypes"].items())[:12]))
    print("biggest     " + ", ".join(f"line {row['line']}={human(row['bytes'])}" for row in result["biggestEvents"][:5]))

    for warning in result["warnings"][:8]:
        print(f"warn        {warning}")
    if result["problems"]:
        print()
        print(f"FAILED      {len(result['problems'])} structural problem(s) — the harness cannot replay this log")
        for problem in result["problems"][:15]:
            print(f"            ! {problem}")
        if len(result["problems"]) > 15:
            print(f"            ... {len(result['problems']) - 15} more")
        print()
        print("next        python repair_session.py --session <id> --copy <new log>   (see --help for repairs)")
    else:
        print()
        print("PASS        structure valid; the harness will replay this log as-is")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose a DSH session log (read-only).")
    parser.add_argument("--session", help="session id prefix, id, directory, or log path (default: newest)")
    parser.add_argument("--all", action="store_true", help="scan every session, worst first")
    parser.add_argument("--limit", type=int, default=10, help="rows for --all (default 10)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.all:
        rows = []
        for path in S.list_sessions():
            result = scan(path)
            rows.append(result)
        rows.sort(key=lambda r: (-len(r["problems"]), -r["bytes"]))
        if args.json:
            print(json.dumps(rows[: args.limit], ensure_ascii=False, indent=2))
        else:
            print(f"{'session':<38} {'size':>9} {'events':>7} {'problems':>9}  log")
            for result in rows[: args.limit]:
                print(f"{result['sessionId']:<38} {human(result['bytes']):>9} {result['events']:>7} "
                      f"{len(result['problems']):>9}  {result['session']}")
        return 1 if any(r["problems"] for r in rows[: args.limit]) else 0

    try:
        path = S.resolve_session(args.session)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = scan(path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_report(result)
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
