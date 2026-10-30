#!/usr/bin/env python3
"""Audit a DSH session's context cost: cache hit rate, replay weight, churn.

Read-only. Answers the questions that matter when a thread feels expensive:
who is filling the window, which requests broke the prefix cache, and which
tool outputs are still being replayed every single turn.

Usage:
    python audit_context.py
    python audit_context.py --session 94811e6d --top 20 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict

import dshsession as S


def human(size) -> str:
    value = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GiB"


def label_of(event: dict) -> str:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event.get("type") == "tool/result":
        call = (data.get("message") or {}).get("toolCallId")
        return f"result of {call}" if call else "result"
    if event.get("type") == "user/message":
        return "user turn"
    if event.get("type") == "developer/message":
        parts = S.content_parts(event)
        kinds = Counter(p.get("type") for p in parts if isinstance(p, dict))
        return "registry: " + ",".join(f"{k}×{v}" for k, v in kinds.items())
    return event.get("type") or "?"


def audit(path: str, top: int = 15) -> dict:
    raw = S.read_bytes(path)
    lines, events, parse_errors = S.load_events(path)
    objects = [e for e in events if e and isinstance(e.get("seq"), int)]
    by_seq = {e["seq"]: e for e in objects}

    # Raw line sizes line up with seq order for a valid log.
    size_of = {}
    for index, line in enumerate(lines[1:]):
        size_of[index] = len(line)
    total_log = sum(size_of.values())

    requests = []
    for event in objects:
        if event.get("type") != "assistant/message":
            continue
        usage = event["data"].get("usage") or {}
        stream = event["data"].get("stream")
        requests.append({
            "seq": event["seq"],
            "turn": event["data"].get("turn"),
            "step": event["data"].get("step"),
            "input": usage.get("inputTokens") or 0,
            "cacheRead": usage.get("cacheReadTokens") or 0,
            "output": usage.get("outputTokens") or 0,
            "reasoning": usage.get("reasoningTokens") or 0,
            "streamChunks": len(stream) if isinstance(stream, list) else 0,
            "streamBytes": len(json.dumps(stream, ensure_ascii=False)) if isinstance(stream, list) else 0,
        })

    for row in requests:
        billed = row["input"] + row["cacheRead"]
        row["hitRate"] = (row["cacheRead"] / billed) if billed else 0.0

    totals = {
        "requests": len(requests),
        "input": sum(r["input"] for r in requests),
        "cacheRead": sum(r["cacheRead"] for r in requests),
        "output": sum(r["output"] for r in requests),
        "reasoning": sum(r["reasoning"] for r in requests),
        "streamBytes": sum(r["streamBytes"] for r in requests),
    }
    billed = totals["input"] + totals["cacheRead"]
    totals["hitRate"] = (totals["cacheRead"] / billed) if billed else 0.0

    reasons = Counter()
    models = Counter()
    for event in objects:
        if event.get("type") == "request/header":
            data = event.get("data") or {}
            reasons[data.get("reason") or "?"] += 1
            header = data.get("header")
            if isinstance(header, dict):
                config = header.get("config") or {}
                models[f"{config.get('provider')}/{config.get('model')} effort={config.get('reasoningEffort')}"] += 1
        elif event.get("type") == "request/context":
            data = event.get("data") or {}
            models[f"{data.get('provider')}/{data.get('model')} window={data.get('contextWindow')}"] += 1

    type_bytes = defaultdict(int)
    type_counts = Counter()
    for seq, size in size_of.items():
        event = by_seq.get(seq)
        kind = event.get("type") if event else "?"
        type_bytes[kind] += size
        type_counts[kind] += 1

    biggest = sorted(
        ({"seq": seq, "bytes": size, "type": (by_seq.get(seq) or {}).get("type"),
          "label": label_of(by_seq.get(seq) or {})} for seq, size in size_of.items()),
        key=lambda row: -row["bytes"],
    )[:top]

    calls = {}
    for event in objects:
        if event.get("type") == "tool/call":
            data = event.get("data") or {}
            calls[data.get("callId")] = {"name": data.get("name"), "arguments": data.get("arguments") or ""}

    repeated = Counter()
    for event in objects:
        if event.get("type") != "tool/result":
            continue
        data = event.get("data") or {}
        message = data.get("message") or {}
        call = calls.get(message.get("toolCallId"))
        if not call:
            continue
        key = (call["name"], call["arguments"][:400])
        repeated[key] += 1
    duplicates = [
        {"name": name, "count": count, "arguments": arguments[:160]}
        for (name, arguments), count in repeated.most_common()
        if count > 1
    ][:top]

    registry = [e for e in objects if e.get("type") == "developer/message"]
    added_tools = set()
    for event in registry:
        for part in S.content_parts(event):
            if isinstance(part, dict) and part.get("type") == "tool-addition":
                added_tools.add(part.get("toolName"))

    bulky_inputs = [
        {"seq": seq, "bytes": size, "type": (by_seq.get(seq) or {}).get("type")}
        for seq, size in size_of.items()
        if size > 50 * 1024 and (by_seq.get(seq) or {}).get("type") in ("user/message", "tool/result")
    ]
    bulky_inputs.sort(key=lambda row: -row["bytes"])

    nodes = S.surface_nodes(objects)
    offloaded = S.offloaded_indexes(objects)
    retained_images, image_bytes = 0, 0
    for event in objects:
        if event["seq"] not in nodes:
            continue
        for index, block in S.image_parts(event):
            if index in offloaded.get(event["seq"], set()):
                continue
            attachment = block.get("attachment") if isinstance(block, dict) else {}
            retained_images += 1
            image_bytes += (attachment or {}).get("bytes") or 0

    return {
        "session": path,
        "sessionId": S.session_id(path),
        "logBytes": len(raw),
        "events": len(objects),
        "payloadBytes": total_log,
        "parseErrors": parse_errors,
        "totals": totals,
        "requests": requests,
        "requestReasons": dict(reasons),
        "models": dict(models),
        "typeBytes": {k: {"bytes": v, "count": type_counts[k]} for k, v in sorted(type_bytes.items(), key=lambda kv: -kv[1])},
        "biggestEvents": biggest,
        "duplicateToolCalls": duplicates,
        "registryChurn": {"events": len(registry), "toolsAdded": len(added_tools)},
        "bulkyInputs": bulky_inputs[:top],
        "retainedImages": retained_images,
        "imageBytes": image_bytes,
    }


def verdicts(result: dict) -> list[str]:
    lines = []
    totals = result["totals"]
    hit = totals["hitRate"]
    if totals["requests"] >= 3:
        if hit >= 0.8:
            lines.append(f"cache      {hit:.0%} of billed input was a cache read — prefix reuse is healthy")
        elif hit >= 0.5:
            lines.append(f"cache      {hit:.0%} hit rate — expect {human(totals['input'])} of full-price input")
        else:
            lines.append(f"cache      only {hit:.0%} hit rate — {human(totals['input'])} was billed as fresh input")
    worst = sorted(result["requests"], key=lambda r: r["hitRate"])[:3]
    for row in worst:
        if row["input"] > 20000:
            lines.append(f"cache miss turn {row['turn']} step {row['step']}: {row['input']} input tokens, "
                         f"hit {row['hitRate']:.0%}")
    if result["registryChurn"]["events"]:
        lines.append(f"registry   {result['registryChurn']['events']} tool-registry change(s) "
                     f"({result['registryChurn']['toolsAdded']} tools) — each one invalidates the prompt prefix")
    if result["retainedImages"]:
        lines.append(f"images     {result['retainedImages']} image occurrence(s) cost "
                     f"{human(result['imageBytes'])} on every request; offload_images.py can stop that")
    if result["totals"]["streamBytes"]:
        lines.append(f"log        assistant stream chunks add {human(result['totals']['streamBytes'])} "
                     f"of log weight (not replayed to the model)")
    bulky = result["bulkyInputs"]
    if bulky:
        lines.append(f"replay     {len(bulky)} input event(s) over 50KiB, largest {human(bulky[0]['bytes'])} "
                     f"at seq {bulky[0]['seq']}")
    dups = result["duplicateToolCalls"]
    if dups:
        lines.append(f"repeats    {len(dups)} tool call(s) issued with identical arguments "
                     f"(worst ×{dups[0]['count']} {dups[0]['name']})")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit DSH session context cost (read-only).")
    parser.add_argument("--session", help="session id prefix, id, directory, or log path (default: newest)")
    parser.add_argument("--top", type=int, default=15, help="rows per section (default 15)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--quiet", action="store_true", help="verdicts only")
    args = parser.parse_args(argv)

    try:
        path = S.resolve_session(args.session)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = audit(path, args.top)
    result["verdicts"] = verdicts(result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    totals = result["totals"]
    print(f"session     {result['sessionId']}")
    print(f"log         {human(result['logBytes'])} compressed, {result['events']} events, "
          f"{human(result['payloadBytes'])} of event payload")
    print(f"model       {', '.join(result['models']) or 'unknown'}")
    print(f"requests    {totals['requests']}, reasons {result['requestReasons']}")
    print(f"tokens      input {totals['input']:,} | cacheRead {totals['cacheRead']:,} | "
          f"output {totals['output']:,} | reasoning {totals['reasoning']:,} | hit {totals['hitRate']:.1%}")
    if not args.quiet:
        print()
        print(f"{'turn':>4} {'step':>4} {'input':>9} {'cacheRead':>10} {'hit':>6} {'out':>6} {'stream':>8}")
        print("-" * 56)
        for row in result["requests"][-args.top:]:
            print(f"{row['turn']:>4} {row['step']:>4} {row['input']:>9,} {row['cacheRead']:>10,} "
                  f"{row['hitRate']:>5.0%} {row['output']:>6,} {human(row['streamBytes']):>8}")
        print()
        print(f"{'seq':>6} {'bytes':>9}  type / label")
        print("-" * 62)
        for row in result["biggestEvents"][: min(args.top, 10)]:
            print(f"{row['seq']:>6} {human(row['bytes']):>9}  {row['type']}: {row['label']}")
        if result["duplicateToolCalls"]:
            print()
            print("repeated tool calls with identical arguments")
            for row in result["duplicateToolCalls"][:8]:
                print(f"  ×{row['count']} {row['name']}: {row['arguments'][:100]}")
    print()
    for line in result["verdicts"]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
