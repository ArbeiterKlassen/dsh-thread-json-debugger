"""DSH session log access: zstd framing, session IO, and V4 structural validation.

Shared by the dsh-thread-* skills. Pure standard library.

Layout of a DSH session log (format version 4):
    <DSH_HOME>/sessions/<workspace-key>/<session-id>/session.v4.jsonl.zstd
The file is a concatenation of independent Zstandard frames; each frame carries
one or more complete JSONL lines. Line 1 is the session header (no `seq`);
every later line is an event with `type`, `seq` (0-based, contiguous), `time`
and `data`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import shutil
import subprocess
import sys
import time
from glob import glob

SESSION_FILENAMES = (
    "session.v4.jsonl.zstd",
    "session.v4.jsonl",
    "session.jsonl.zstd",
    "session.jsonl",
)


# --------------------------------------------------------------------------
# zstd: ctypes libzstd first (no subprocess, handles multi-frame streams),
# external `zstd` binary as fallback.
# --------------------------------------------------------------------------


class _InBuffer(ctypes.Structure):
    _fields_ = [
        ("src", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
        ("pos", ctypes.c_size_t),
    ]


class _OutBuffer(ctypes.Structure):
    _fields_ = [
        ("dst", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
        ("pos", ctypes.c_size_t),
    ]


_LIB_CACHE: list = []


def _libzstd():
    if _LIB_CACHE:
        return _LIB_CACHE[0]

    candidates: list[str] = []
    env = os.environ.get("DSH_ZSTD_LIB")
    if env:
        candidates.append(env)
    found = ctypes.util.find_library("zstd")
    if found:
        candidates.append(found)
    here = os.path.dirname(os.path.abspath(__file__))
    names = ("libzstd.dll", "zstd.dll", "libzstd.so.1", "libzstd.dylib", "libzstd.so")
    for base in (here, os.path.join(here, ".."), os.path.join(here, "..", "lib")):
        for name in names:
            candidates.append(os.path.join(base, name))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            for name in names:
                candidates.append(os.path.join(entry, name))
    for root in (r"D:\Anaconda\Library\bin", r"C:\ProgramData\anaconda3\Library\bin"):
        candidates.extend(os.path.join(root, n) for n in names)

    lib = None
    for candidate in candidates:
        try:
            lib = ctypes.CDLL(candidate)
            break
        except OSError:
            continue
    if lib is not None:
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
        lib.ZSTD_getErrorName.restype = ctypes.c_char_p
        lib.ZSTD_decompressBound.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_decompressBound.restype = ctypes.c_ulonglong
    _LIB_CACHE.append(lib)
    return lib


def _zstd_error(lib, code: int) -> str:
    try:
        return lib.ZSTD_getErrorName(ctypes.c_size_t(code)).decode("utf-8", "replace")
    except Exception:  # pragma: no cover - diagnostics only
        return f"zstd error {code}"


def _decompress_ctypes(lib, data: bytes) -> bytes:
    lib.ZSTD_createDStream.restype = ctypes.c_void_p
    lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.restype = ctypes.c_size_t
    lib.ZSTD_DStreamOutSize.restype = ctypes.c_size_t
    lib.ZSTD_decompressStream.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_OutBuffer),
        ctypes.POINTER(_InBuffer),
    ]
    lib.ZSTD_decompressStream.restype = ctypes.c_size_t

    stream = lib.ZSTD_createDStream()
    if not stream:
        raise RuntimeError("ZSTD_createDStream failed")
    try:
        code = lib.ZSTD_initDStream(stream)
        if lib.ZSTD_isError(code):
            raise RuntimeError(_zstd_error(lib, code))

        src = (ctypes.c_char * len(data)).from_buffer_copy(data)
        inbuf = _InBuffer(ctypes.cast(src, ctypes.c_void_p), len(data), 0)
        out_size = max(int(lib.ZSTD_DStreamOutSize()), 1 << 17)
        out_mem = ctypes.create_string_buffer(out_size)
        outbuf = _OutBuffer(ctypes.cast(out_mem, ctypes.c_void_p), out_size, 0)
        chunks: list[bytes] = []
        while inbuf.pos < inbuf.size:
            outbuf.pos = 0
            code = lib.ZSTD_decompressStream(stream, ctypes.byref(outbuf), ctypes.byref(inbuf))
            if lib.ZSTD_isError(code):
                raise RuntimeError(_zstd_error(lib, code))
            if outbuf.pos:
                chunks.append(bytes(out_mem.raw[: outbuf.pos]))
            if code == 0 and inbuf.pos >= inbuf.size:
                break
            if outbuf.pos == 0 and inbuf.pos >= inbuf.size:
                break
        return b"".join(chunks)
    finally:
        lib.ZSTD_freeDStream(stream)


def _compress_ctypes(lib, data: bytes) -> bytes:
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_compress.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
    ]
    lib.ZSTD_compress.restype = ctypes.c_size_t

    bound = int(lib.ZSTD_compressBound(len(data))) or 1
    dst = ctypes.create_string_buffer(bound)
    src = ctypes.create_string_buffer(data, len(data)) if data else ctypes.create_string_buffer(1)
    code = lib.ZSTD_compress(dst, bound, src, len(data), 3)
    if lib.ZSTD_isError(code):
        raise RuntimeError(_zstd_error(lib, code))
    return dst.raw[:code]


def _zstd_binary() -> str | None:
    for name in ("zstd", "zstd.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


def decompress(data: bytes) -> bytes:
    """Decode a (possibly multi-frame) zstd blob."""
    if not data:
        return b""
    lib = _libzstd()
    if lib is not None:
        try:
            return _decompress_ctypes(lib, data)
        except Exception as exc:  # fall back to the binary
            lib_error = exc
    else:  # pragma: no cover
        lib_error = RuntimeError("libzstd not found")
    binary = _zstd_binary()
    if binary:
        proc = subprocess.run([binary, "-dc"], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode == 0:
            return proc.stdout
        raise RuntimeError(f"zstd -dc failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    raise RuntimeError(f"no zstd backend available ({lib_error}); set DSH_ZSTD_LIB to libzstd")


def compress_frame(line: bytes) -> bytes:
    """Encode one JSONL line as its own Zstandard frame."""
    lib = _libzstd()
    if lib is not None:
        return _compress_ctypes(lib, line)
    binary = _zstd_binary()
    if binary:
        proc = subprocess.run([binary, "-q", "-c", "-3"], input=line, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode == 0:
            return proc.stdout
        raise RuntimeError(f"zstd compress failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    raise RuntimeError("no zstd backend available for compression")


def frame_count(data: bytes) -> int:
    """Count zstd frames by walking the magic numbers at frame boundaries."""
    lib = _libzstd()
    if lib is None:
        return -1
    lib.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_findFrameCompressedSize.restype = ctypes.c_size_t
    src = (ctypes.c_char * max(len(data), 1)).from_buffer_copy(data or b"\0")
    offset = 0
    count = 0
    while offset < len(data):
        size = lib.ZSTD_findFrameCompressedSize(
            ctypes.cast(ctypes.byref(src, offset), ctypes.c_void_p), len(data) - offset
        )
        if lib.ZSTD_isError(size) or size == 0:
            return count if offset == len(data) else -1
        offset += int(size)
        count += 1
    return count


# --------------------------------------------------------------------------
# Session discovery and IO
# --------------------------------------------------------------------------


def dsh_home() -> str:
    return os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")


def list_sessions(home: str | None = None) -> list[str]:
    """All session log paths under DSH_HOME, newest first."""
    base = os.path.join(home or dsh_home(), "sessions")
    paths = [p for p in glob(os.path.join(base, "*", "*", "session*")) if os.path.isfile(p)]
    paths = [p for p in paths if os.path.basename(p) in SESSION_FILENAMES]
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths


def resolve_session(spec: str | None = None) -> str:
    """Resolve a session argument to a log path.

    Accepts an absolute/relative path, a full session id, an id prefix, or None
    for the newest session under DSH_HOME.
    """
    if spec and os.path.isfile(spec):
        return os.path.abspath(spec)
    if spec and os.path.isdir(spec):
        for name in SESSION_FILENAMES:
            candidate = os.path.join(spec, name)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        raise SystemExit(f"no session log inside directory: {spec}")

    sessions = list_sessions()
    if not sessions:
        raise SystemExit(f"no sessions found under {dsh_home()}")
    if not spec:
        return sessions[0]
    matches = [p for p in sessions if spec.lower() in p.lower()]
    if not matches:
        raise SystemExit(f"no session matches {spec!r}")
    if len(matches) > 1:
        narrowed = [p for p in matches if os.path.basename(os.path.dirname(p)).startswith(spec)]
        if narrowed:
            matches = narrowed
    if len(matches) > 1:
        listing = "\n  ".join(matches[:10])
        raise SystemExit(f"ambiguous session {spec!r}, candidates:\n  {listing}")
    return matches[0]


def session_id(path: str) -> str:
    return os.path.basename(os.path.dirname(os.path.abspath(path)))


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def load_lines(path: str) -> list[bytes]:
    """Decompressed logical lines, without the trailing newline."""
    raw = read_bytes(path)
    text = decompress(raw) if path.endswith(".zstd") else raw
    parts = text.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def load_events(path: str) -> tuple[list[bytes], list[dict | None], list[str]]:
    """Return (raw_lines, parsed_objects_or_None, parse_errors)."""
    lines = load_lines(path)
    objects: list[dict | None] = []
    errors: list[str] = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            objects.append(None)
            errors.append(f"line {index}: blank line")
            continue
        try:
            value = json.loads(line)
        except Exception as exc:
            objects.append(None)
            errors.append(f"line {index}: JSON parse error: {exc}")
            continue
        if not isinstance(value, dict):
            objects.append(None)
            errors.append(f"line {index}: JSON value is not an object")
            continue
        objects.append(value)
    return lines, objects, errors


def _atomic_write(path: str, payload: bytes, backup: bool) -> str | None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = None
    if backup and os.path.isfile(path):
        backup_path = f"{path}.bak-{stamp}"
        shutil.copy2(path, backup_path)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(tmp, path)
    except OSError:
        # Windows: the running app may hold the file open. Write in place.
        with open(path, "r+b") as handle:
            handle.seek(0)
            handle.write(payload)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.remove(tmp)
        except OSError:
            pass
    return backup_path


def write_lines(path: str, lines: list[bytes], backup: bool = True) -> str | None:
    """Rewrite the whole log, one zstd frame per line (valid V2 framing)."""
    payload = b"".join(compress_frame(line + b"\n") for line in lines)
    return _atomic_write(path, payload, backup)


def append_lines(path: str, lines: list[bytes]) -> int:
    """Append new events as fresh frames; returns bytes written."""
    payload = b"".join(compress_frame(line + b"\n") for line in lines)
    with open(path, "ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return len(payload)


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


SEQ_KEYS = ("seq", "startSeq", "endSeq", "baseSeq", "anchorSeq", "headerSeq")
SEQ_LIST_KEYS = ("sourceEventSeqs", "eventSeqs", "seqs")


def _remap(value, mapping: dict[int, int]):
    """Rewrite sequence references inside a JSON payload using old -> new."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in SEQ_KEYS and isinstance(item, int) and not isinstance(item, bool):
                result[key] = mapping.get(item, item)
            elif key in SEQ_LIST_KEYS and isinstance(item, list):
                result[key] = [mapping.get(i, i) if isinstance(i, int) else i for i in item]
            else:
                result[key] = _remap(item, mapping)
        return result
    if isinstance(value, list):
        return [_remap(item, mapping) for item in value]
    return value


def renumber(events: list[dict], drop_types: tuple[str, ...] = ()) -> tuple[list[dict], dict[int, int]]:
    """Compact events to a contiguous 0..N-1 sequence, dropping selected types.

    Only meaningful for a fresh copy: the harness indexes events by
    `seq - baseSeq`, so gaps are not allowed but renumbering is fine as long as
    every reference (surfaceOp ranges, sourceEventSeqs) is remapped with it.
    """
    kept = [e for e in events if e.get("type") not in drop_types]
    mapping = {}
    for position, event in enumerate(kept):
        old = event.get("seq")
        if isinstance(old, int) and not isinstance(old, bool):
            mapping[old] = position
    rewritten = []
    for position, event in enumerate(kept):
        clone = dict(event)
        clone["seq"] = position
        if "surfaceOp" in clone:
            clone["surfaceOp"] = _remap(clone["surfaceOp"], mapping)
        if "sourceEventSeqs" in clone:
            clone["sourceEventSeqs"] = _remap(clone["sourceEventSeqs"], mapping)
        if isinstance(clone.get("data"), dict):
            clone["data"] = _remap(clone["data"], mapping)
        rewritten.append(clone)
    return rewritten, mapping


# --------------------------------------------------------------------------
# Event helpers
# --------------------------------------------------------------------------

MESSAGE_TYPES = ("user/message", "assistant/message", "system/message", "tool/result", "developer/message")


def message_of(event: dict) -> dict | None:
    """The message payload a given event carries, if any."""
    kind = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    if kind == "user/message":
        return data
    if kind in ("assistant/message", "tool/result", "system/message", "developer/message"):
        message = data.get("message")
        return message if isinstance(message, dict) else None
    return None


def content_parts(event: dict) -> list:
    message = message_of(event)
    if not message:
        return []
    parts = message.get("content")
    return parts if isinstance(parts, list) else []


def image_parts(event: dict) -> list[tuple[int, dict]]:
    """(depth-first image index, block) for one event.

    Matches the harness rule: indexes count every `image` block in
    `message.content` order, including blocks already marked offloaded.
    """
    found: list[tuple[int, dict]] = []
    index = 0
    for block in content_parts(event):
        if isinstance(block, dict) and block.get("type") == "image":
            found.append((index, block))
            index += 1
    return found


def surface_nodes(events: list[dict]) -> set[int]:
    """Replay `surfaceOp` to get the seqs still on the model surface."""
    nodes: set[int] = set()
    for event in events:
        seq = event.get("seq")
        if not isinstance(seq, int):
            continue
        op = event.get("surfaceOp")
        if op == "append":
            nodes.add(seq)
        elif isinstance(op, dict) and op.get("op") == "replace":
            start = op.get("startSeq")
            end = op.get("endSeq")
            if isinstance(start, int) and isinstance(end, int):
                for gone in range(start, end + 1):
                    nodes.discard(gone)
            nodes.add(seq)
    return nodes


def offloaded_indexes(events: list[dict]) -> dict[int, set[int]]:
    """Accumulated image/offload selections: event seq -> offloaded indexes."""
    result: dict[int, set[int]] = {}
    for event in events:
        if event.get("type") != "image/offload":
            continue
        data = event.get("data")
        targets = data.get("targets") if isinstance(data, dict) else None
        if not isinstance(targets, list):
            continue
        for target in targets:
            if not isinstance(target, dict):
                continue
            seq = target.get("seq")
            indexes = target.get("imageIndexes")
            if isinstance(seq, int) and isinstance(indexes, list):
                bucket = result.setdefault(seq, set())
                bucket.update(i for i in indexes if isinstance(i, int))
    return result


def validate_image_offload(event: dict, by_seq: dict[int, dict], nodes: set[int],
                           prior: dict[int, set[int]]) -> list[str]:
    """Mirror the harness `imageOffloadProjection` rules for one event."""
    problems: list[str] = []
    data = event.get("data")
    if not isinstance(data, dict) or set(data.keys()) != {"targets"}:
        return ["image/offload: data must contain exactly one key 'targets'"]
    targets = data["targets"]
    if not isinstance(targets, list) or not targets:
        return ["image/offload: data must contain a nonempty targets array"]
    seen: set[int] = set()
    for target in targets:
        if not isinstance(target, dict) or set(target.keys()) != {"seq", "imageIndexes"}:
            problems.append("image/offload: each target must contain exactly seq and imageIndexes")
            continue
        seq = target["seq"]
        indexes = target["imageIndexes"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            problems.append("image/offload: target seq must be a non-negative integer")
            continue
        if seq in seen:
            problems.append(f"image/offload: duplicate target seq {seq}")
            continue
        seen.add(seq)
        if seq not in nodes:
            problems.append(f"image/offload: target seq {seq} is not a current surface node")
            continue
        source = by_seq.get(seq)
        if source is None or source.get("type") not in ("user/message", "tool/result"):
            problems.append(f"image/offload: target seq {seq} must be user/message or tool/result")
            continue
        if not isinstance(indexes, list) or not indexes:
            problems.append("image/offload: imageIndexes must be a nonempty array")
            continue
        previous = -1
        for index in indexes:
            if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index <= previous:
                problems.append("image/offload: imageIndexes must be strictly increasing non-negative integers")
                break
            previous = index
        existing = {i for i, _ in image_parts(source)}
        already = prior.get(seq, set())
        for index in indexes:
            if not isinstance(index, int):
                continue
            if index not in existing:
                problems.append(f"image/offload: image index {index} does not exist in seq {seq}")
            elif index in already:
                problems.append(f"image/offload: image index {index} in seq {seq} is already offloaded")
    return problems


def validate(events: list[dict], parse_errors: list[str], lines: list[bytes]) -> dict:
    """Structural validation of a v4 session log; returns a report dict."""
    problems: list[str] = list(parse_errors)
    warnings: list[str] = []

    header = events[0] if events else None
    if not header or header.get("type") != "session":
        problems.append("header: line 1 is not a `session` header event")
    else:
        if header.get("version") != 4:
            problems.append(f"header: unexpected format version {header.get('version')!r} (expected 4)")
        if not header.get("id"):
            problems.append("header: missing session id")

    by_seq: dict[int, dict] = {}
    last_seq = -1
    for index, event in enumerate(events[1:], 2):
        if event is None:
            continue
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            problems.append(f"line {index}: event has no integer seq")
            continue
        if seq in by_seq:
            problems.append(f"line {index}: duplicate seq {seq}")
        if seq != last_seq + 1:
            problems.append(f"line {index}: seq gap/regression, expected {last_seq + 1} got {seq}")
        last_seq = max(last_seq, seq)
        by_seq[seq] = event
        kind = event.get("type")
        if not isinstance(kind, str) or not kind:
            problems.append(f"line {index}: event has no type")
        if not isinstance(event.get("time"), (int, float)):
            problems.append(f"line {index}: event has no numeric time")
        if not isinstance(event.get("data"), dict):
            problems.append(f"line {index}: event has no object data")

    # tool call / result pairing
    calls: dict[str, int] = {}
    results: dict[str, int] = {}
    for seq, event in sorted(by_seq.items()):
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "tool/call":
            call_id = data.get("callId")
            if not isinstance(call_id, str) or not call_id:
                problems.append(f"seq {seq}: tool/call without a non-empty callId")
            elif call_id in calls:
                problems.append(f"seq {seq}: duplicate tool/call for callId {call_id}")
            else:
                calls[call_id] = seq
        elif kind == "tool/result":
            message = message_of(event) or {}
            call_id = message.get("toolCallId") or data.get("toolCallId")
            source = message.get("source") or {}
            if not isinstance(call_id, str) or not call_id:
                problems.append(f"seq {seq}: tool/result without a non-empty callId")
            else:
                if call_id in results:
                    problems.append(f"seq {seq}: duplicate tool/result for callId {call_id}")
                results[call_id] = seq
            if not (isinstance(source, dict) and source.get("kind") == "tool" and source.get("callId") == call_id):
                problems.append(f"seq {seq}: tool/result source must be {{kind:'tool', callId:{call_id!r}}}")
            if message.get("role") != "tool":
                problems.append(f"seq {seq}: tool/result message role must be 'tool'")
            if not isinstance(message.get("content"), list):
                problems.append(f"seq {seq}: tool/result content must be an array")
            if "isError" in message and not isinstance(message.get("isError"), bool):
                problems.append(f"seq {seq}: tool/result isError must be a boolean")
    last_turn_end = max((s for s, e in by_seq.items() if e.get("type") == "turn/end"), default=-1)
    for call_id, seq in sorted(calls.items(), key=lambda kv: kv[1]):
        if call_id not in results:
            if seq > last_turn_end:
                warnings.append(f"seq {seq}: tool/call {call_id} is in flight (no result yet)")
            else:
                problems.append(f"seq {seq}: tool/call {call_id} has no tool/result")
    for call_id, seq in sorted(results.items(), key=lambda kv: kv[1]):
        if call_id not in calls:
            warnings.append(f"seq {seq}: tool/result {call_id} has no matching tool/call (fork/crash recovery)")

    # turn / step balance
    open_turn = None
    open_step = None
    for seq, event in sorted(by_seq.items()):
        kind = event.get("type")
        if kind == "turn/start":
            if open_turn is not None:
                problems.append(f"seq {seq}: turn/start while turn {open_turn} still open")
            open_turn = seq
        elif kind == "turn/end":
            if open_turn is None:
                problems.append(f"seq {seq}: turn/end without an open turn")
            open_turn = None
        elif kind == "step/start":
            if open_step is not None:
                warnings.append(f"seq {seq}: step/start while step {open_step} still open")
            open_step = seq
        elif kind == "step/end":
            open_step = None
    if open_turn is not None:
        warnings.append(f"turn opened at seq {open_turn} never ended (live or interrupted turn)")

    # message shape
    for seq, event in sorted(by_seq.items()):
        kind = event.get("type")
        if kind not in MESSAGE_TYPES:
            continue
        message = message_of(event)
        if message is None:
            problems.append(f"seq {seq}: {kind} carries no message object")
            continue
        parts = message.get("content")
        if parts is None:
            continue
        if not isinstance(parts, list):
            problems.append(f"seq {seq}: {kind} content must be an array")
            continue
        for index, block in enumerate(parts):
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                problems.append(f"seq {seq}: content[{index}] is not a typed block")

    # image/offload projection rules
    nodes = surface_nodes(list(by_seq.values()))
    prior: dict[int, set[int]] = {}
    for seq, event in sorted(by_seq.items()):
        if event.get("type") != "image/offload":
            continue
        problems.extend(f"seq {seq}: {p}" for p in validate_image_offload(event, by_seq, nodes, prior))
        accumulated = offloaded_indexes([event])
        for target_seq, indexes in accumulated.items():
            prior.setdefault(target_seq, set()).update(indexes)

    return {
        "problems": problems,
        "warnings": warnings,
        "events": len(by_seq),
        "last_seq": last_seq,
        "header": header,
        "nodes": len(nodes),
    }


def is_app_running() -> bool:
    """Best-effort check for a running DeepSeek Harness process."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq DeepSeek Harness.exe", "/NH"],
                capture_output=True, text=True, timeout=20,
            ).stdout
            return "DeepSeek Harness" in out
        out = subprocess.run(["ps", "-A", "-o", "command"], capture_output=True, text=True, timeout=20).stdout
        return "deePSeek harness" in out.lower() or "dsh --profile" in out
    except Exception:
        return False


__all__ = [
    "SESSION_FILENAMES",
    "append_lines",
    "compress_frame",
    "content_parts",
    "decompress",
    "dsh_home",
    "dumps",
    "frame_count",
    "image_parts",
    "is_app_running",
    "list_sessions",
    "load_events",
    "load_lines",
    "message_of",
    "offloaded_indexes",
    "read_bytes",
    "resolve_session",
    "session_id",
    "surface_nodes",
    "validate",
    "validate_image_offload",
    "write_lines",
]
