#!/usr/bin/env python3
"""Apply the smallest safe repair that makes a DSH session log replay again.

Repairs work on a copy by default. In-place repair needs --in-place --force and
a closed DSH app, because the app owns the file and its sequence counter.

Repairs (pick any combination):
  --truncate-tail          drop trailing unparsable/partial lines (crash damage)
  --stub-missing-results   append a tool/result stub for calls that lost theirs
  --drop-seq 12,13         drop events by seq, then renumber
  --renumber               compact sequence numbers to 0..N-1 and remap references

Everything is validated before it is written; a repair that does not validate is
never saved.

Usage:
    python repair_session.py --copy C:\\Users\\deepd\\tmp\\fixed.zstd --truncate-tail
    python repair_session.py --copy fixed.zstd --stub-missing-results --drop-seq 120
    python repair_session.py --in-place --force --renumber
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid

import dshsession as S


def missing_result_calls(events: list[dict]) -> tuple[list[str], set[str]]:
    calls = {}
    results = set()
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "tool/call":
            calls[data.get("callId")] = event
        elif event.get("type") == "tool/result":
            message = data.get("message") or {}
            if message.get("toolCallId"):
                results.add(message["toolCallId"])
    last_turn_end = max((e["seq"] for e in events if e.get("type") == "turn/end"), default=-1)
    abandoned = [cid for cid, event in calls.items() if cid not in results and event["seq"] < last_turn_end]
    return abandoned, results


def stub_result(call: dict, seq: int, reason: str) -> dict:
    data = call.get("data") or {}
    call_id = data.get("callId")
    return {
        "type": "tool/result",
        "seq": seq,
        "time": int(time.time() * 1000),
        "data": {
            "turn": data.get("turn"),
            "step": data.get("step"),
            "message": {
                "role": "tool",
                "source": {"kind": "tool", "callId": call_id},
                "toolCallId": call_id,
                "content": [{"type": "text", "text": reason}],
                "isError": True,
                "id": str(uuid.uuid4()),
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair a DSH session log so it replays.")
    parser.add_argument("--session", help="session id prefix, id, directory, or log path (default: newest)")
    parser.add_argument("--copy", help="write the repaired log here (recommended)")
    parser.add_argument("--in-place", action="store_true", help="repair the session log itself (needs --force)")
    parser.add_argument("--force", action="store_true", help="allow in-place repair while the app is closed")
    parser.add_argument("--truncate-tail", action="store_true", help="drop trailing unparsable lines")
    parser.add_argument("--stub-missing-results", action="store_true", help="append stubs for abandoned tool calls")
    parser.add_argument("--drop-seq", help="comma separated seqs to drop, then renumber")
    parser.add_argument("--drop-duplicate-results", action="store_true",
                        help="drop repeated tool/result events for the same call, then renumber")
    parser.add_argument("--renumber", action="store_true", help="compact sequence numbers and remap references")
    parser.add_argument("--dry-run", action="store_true", help="report the plan without writing")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.copy and not args.in_place:
        parser.error("choose an output: --copy <log path> (recommended) or --in-place --force")
    if args.in_place:
        if not args.force:
            parser.error("--in-place needs --force; a copy is safer")
        if S.is_app_running():
            raise SystemExit("error: the DSH app is running; close it before repairing in place")

    source = S.resolve_session(args.session)
    lines, events, parse_errors = S.load_events(source)
    header = events[0] if events and events[0] and events[0].get("type") == "session" else None
    objects = [e for e in events if e and e.get("type") != "session"]

    actions = []
    dropped_parse = []

    if parse_errors and not args.truncate_tail:
        raise SystemExit("error: the log has unparsable lines; add --truncate-tail (or scan it first):\n  "
                         + "\n  ".join(parse_errors[:5]))
    if args.truncate_tail and parse_errors:
        dropped_parse = list(parse_errors)
        actions.append(f"dropped {len(parse_errors)} unparsable trailing line(s)")

    if args.drop_seq:
        wanted = {int(piece) for chunk in args.drop_seq.split(",") for piece in [chunk.strip()] if piece}
        before = len(objects)
        objects = [e for e in objects if e.get("seq") not in wanted]
        actions.append(f"dropped {before - len(objects)} event(s) by seq: {sorted(wanted)}")

    if args.drop_duplicate_results:
        seen = set()
        kept = []
        duplicates = 0
        for event in objects:
            if event.get("type") == "tool/result":
                call_id = ((event.get("data") or {}).get("message") or {}).get("toolCallId")
                if call_id and call_id in seen:
                    duplicates += 1
                    continue
                if call_id:
                    seen.add(call_id)
            kept.append(event)
        objects = kept
        if duplicates:
            actions.append(f"dropped {duplicates} duplicate tool/result event(s), keeping the first per call")
        else:
            actions.append("no duplicate tool/result events were found")

    if args.stub_missing_results:
        abandoned, _ = missing_result_calls(objects)
        next_seq = (max(e["seq"] for e in objects) + 1) if objects else 0
        for offset, call_id in enumerate(abandoned):
            call = next(e for e in objects if (e.get("data") or {}).get("callId") == call_id)
            objects.append(stub_result(
                call,
                next_seq + offset,
                "[dsh-thread-json-debugger] this tool result was missing from the log; "
                "a stub was appended to keep the history replayable",
            ))
        if abandoned:
            actions.append(f"appended {len(abandoned)} tool/result stub(s)")

    if args.renumber or args.drop_seq or args.drop_duplicate_results or args.stub_missing_results:
        objects, mapping = S.renumber(objects)
        if mapping:
            actions.append(f"renumbered {len(mapping)} event(s) to 0..{len(objects) - 1}")

    if not actions:
        raise SystemExit("nothing to do: pass a repair flag (see --help)")

    out_lines = []
    if header:
        out_lines.append(S.dumps(header))
    out_lines.extend(S.dumps(event) for event in objects)

    parsed = [json.loads(line) for line in out_lines]
    verdict = S.validate(parsed, [], out_lines)
    report = {
        "source": source,
        "target": os.path.abspath(args.copy) if args.copy else source,
        "inPlace": bool(args.in_place),
        "dryRun": bool(args.dry_run),
        "actions": actions,
        "droppedParseErrors": dropped_parse,
        "eventsAfter": len(objects),
        "problemsAfter": verdict["problems"],
        "warningsAfter": verdict["warnings"],
        "surfaceNodesAfter": verdict["nodes"],
        "written": False,
    }

    if verdict["problems"] and not args.dry_run:
        report["problemsAfter"] = verdict["problems"]
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("refusing to write: the repaired log still has problems")
            for problem in verdict["problems"][:10]:
                print(f"  ! {problem}")
        return 1

    if not args.dry_run:
        target = args.copy if args.copy else source
        if args.copy:
            os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
            if os.path.exists(target):
                shutil.copy2(target, f"{target}.bak")
        S.write_lines(target, out_lines, backup=not args.copy)
        report["written"] = True
        report["targetBytes"] = os.path.getsize(target)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"source    {source}")
    print(f"target    {report['target']}{' (in place)' if args.in_place else ''}")
    for action in actions:
        print(f"action    {action}")
    print(f"result    {len(objects)} events, {verdict['nodes']} surface nodes, "
          f"{len(verdict['problems'])} problem(s), {len(verdict['warnings'])} warning(s)")
    for warning in verdict["warnings"][:5]:
        print(f"          ~ {warning}")
    if args.dry_run:
        print("dry run   nothing written")
    else:
        print(f"written   {report['target']} ({report['targetBytes']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
