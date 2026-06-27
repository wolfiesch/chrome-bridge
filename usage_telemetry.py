#!/usr/bin/env python3
import argparse
import collections
import datetime
import glob
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_PROJECTS_DIR = "~/.claude/projects"
DEFAULT_CODEX_DIR = "~/.codex/sessions"
DEFAULT_BRIDGE_AUDIT = os.path.join(SCRIPT_DIR, "bridge_audit.jsonl")
DEFAULT_SERVER_MATCH = r"chrome[-_]devtools"

SOURCES = ("claude", "codex", "bridge")


# --- Claude Code transcripts -------------------------------------------------
# Each assistant turn stores tool_use blocks in message.content; the MCP tool
# name carries the server prefix (e.g. "mcp__chrome-devtools__navigate_page").

def tool_uses_from_line(line):
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return

    message = record.get("message")
    if not isinstance(message, dict):
        return

    content = message.get("content")
    if not isinstance(content, list):
        return

    timestamp = record.get("timestamp")
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if isinstance(name, str):
            yield name, timestamp if isinstance(timestamp, str) else None


def bare_tool_name(name):
    bare = name.rsplit("__", 1)[-1]
    for prefix in ("chrome_devtools_", "chrome-devtools_"):
        if bare.startswith(prefix):
            return bare[len(prefix):]
    return bare


def line_might_match(line, server_re):
    if server_re.pattern == DEFAULT_SERVER_MATCH:
        return "chrome-devtools" in line or "chrome_devtools" in line
    return bool(server_re.search(line))


def iter_candidate_tool_uses(path, server_re):
    try:
        with open(path, "rb") as handle:
            for raw_line in handle:
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if not line_might_match(line, server_re):
                    continue
                yield from tool_uses_from_line(line)
    except OSError:
        return


def collect_claude(projects_dir, server_re, since=None):
    counts = collections.Counter()
    report = _new_source_report()

    pattern = str(Path(projects_dir).expanduser() / "**" / "*.jsonl")
    for file_name in glob.iglob(pattern, recursive=True):
        file_had_match = False
        for name, timestamp in iter_candidate_tool_uses(file_name, server_re):
            if not server_re.search(name):
                continue
            if not timestamp_in_range(timestamp, since):
                continue
            counts[bare_tool_name(name)] += 1
            file_had_match = True
            _track_ts(report, timestamp)
        if file_had_match:
            report["sessions"].add(file_name)

    return _finalize_source(report, counts)


# --- Codex rollout transcripts -----------------------------------------------
# MCP calls land canonically as payloads of type "mcp_tool_call_end" carrying
# invocation.server / invocation.tool. The same call_id ALSO appears as a
# bare-named response_item function_call, so we count only the mcp end event
# (deduped by call_id) to avoid double counting.

def codex_calls_from_line(line, server_re):
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return

    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    if payload.get("type") != "mcp_tool_call_end":
        return

    invocation = payload.get("invocation")
    if not isinstance(invocation, dict):
        return
    server = invocation.get("server") or ""
    tool = invocation.get("tool") or ""
    match_target = "{0}__{1}".format(server, tool)
    if not server_re.search(match_target):
        return

    call_id = payload.get("call_id")
    timestamp = record.get("timestamp")
    yield (
        call_id if isinstance(call_id, str) else None,
        tool or match_target,
        timestamp if isinstance(timestamp, str) else None,
    )


def iter_candidate_codex_calls(path, server_re):
    try:
        with open(path, "rb") as handle:
            for raw_line in handle:
                if b"mcp_tool_call_end" not in raw_line:
                    continue
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                yield from codex_calls_from_line(line, server_re)
    except OSError:
        return


def collect_codex(sessions_dir, server_re, since=None):
    counts = collections.Counter()
    report = _new_source_report()
    seen_ids = set()

    pattern = str(Path(sessions_dir).expanduser() / "**" / "*.jsonl")
    for file_name in glob.iglob(pattern, recursive=True):
        file_had_match = False
        for call_id, tool, timestamp in iter_candidate_codex_calls(file_name, server_re):
            if not timestamp_in_range(timestamp, since):
                continue
            if call_id is not None:
                if call_id in seen_ids:
                    continue
                seen_ids.add(call_id)
            counts[tool] += 1
            file_had_match = True
            _track_ts(report, timestamp)
        if file_had_match:
            report["sessions"].add(file_name)

    return _finalize_source(report, counts)


# --- Bridge audit log --------------------------------------------------------
# bridge_audit.jsonl is already source-specific (every row is a bridge action),
# so --server-match is NOT applied here. Forwarded actions emit two rows sharing
# a requestId (allow + extension_success); we collapse those to one logical
# call. Lease-family / pre-forward decisions have a null requestId and each such
# row is counted as its own event (never collapsed together).

_BRIDGE_DENY_DECISIONS = ("deny", "lease_deny", "extension_error", "confirmation_required")


def collect_bridge(audit_path, since=None, include_denied=True):
    counts = collections.Counter()
    report = _new_source_report()
    seen_requests = set()

    path = Path(audit_path).expanduser()
    try:
        handle = open(path, "rb")
    except OSError:
        return _finalize_source(report, counts)

    with handle:
        for raw_line in handle:
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue

            action = record.get("action")
            if not isinstance(action, str):
                continue
            decision = record.get("decision")
            if not include_denied and decision in _BRIDGE_DENY_DECISIONS:
                continue

            timestamp = _bridge_ts_to_iso(record.get("ts"))
            if not timestamp_in_range(timestamp, since):
                continue

            # Only collapse on a real requestId; null-id rows (lease/deny) each
            # count individually rather than collapsing into one.
            request_id = record.get("requestId")
            if isinstance(request_id, str):
                if request_id in seen_requests:
                    continue
                seen_requests.add(request_id)

            counts[action] += 1
            _track_ts(report, timestamp)

    if counts:
        report["sessions"].add(str(path))
    return _finalize_source(report, counts)


def _bridge_ts_to_iso(ts):
    if not isinstance(ts, (int, float)):
        return None
    try:
        moment = datetime.datetime.fromtimestamp(ts / 1000.0, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


# --- Shared helpers ----------------------------------------------------------

def timestamp_in_range(timestamp, since):
    if since is None:
        return True
    if timestamp is None:
        return False
    return timestamp >= since


def _new_source_report():
    return {"sessions": set(), "first_ts": None, "last_ts": None}


def _track_ts(report, timestamp):
    if timestamp is None:
        return
    if report["first_ts"] is None or timestamp < report["first_ts"]:
        report["first_ts"] = timestamp
    if report["last_ts"] is None or timestamp > report["last_ts"]:
        report["last_ts"] = timestamp


def _finalize_source(report, counts):
    return {
        "calls": sum(counts.values()),
        "sessions": len(report["sessions"]),
        "first_ts": report["first_ts"],
        "last_ts": report["last_ts"],
        "per_tool": dict(counts.most_common()),
    }


def collect(args, server_re):
    selected = args.sources
    sources = {}

    if "claude" in selected:
        sources["claude"] = collect_claude(
            Path(args.projects_dir).expanduser(), server_re, args.since
        )
    if "codex" in selected:
        sources["codex"] = collect_codex(
            Path(args.codex_dir).expanduser(), server_re, args.since
        )
    if "bridge" in selected:
        sources["bridge"] = collect_bridge(
            Path(args.bridge_audit).expanduser(), args.since, include_denied=not args.exclude_denied
        )

    total_calls = sum(s["calls"] for s in sources.values())

    combined = collections.Counter()
    first_ts = None
    last_ts = None
    sessions = 0
    by_source = {}
    for name, src in sources.items():
        combined.update(src["per_tool"])
        sessions += src["sessions"]
        if src["first_ts"] is not None and (first_ts is None or src["first_ts"] < first_ts):
            first_ts = src["first_ts"]
        if src["last_ts"] is not None and (last_ts is None or src["last_ts"] > last_ts):
            last_ts = src["last_ts"]
        share = (src["calls"] / total_calls) if total_calls else 0.0
        by_source[name] = {"calls": src["calls"], "share": round(share, 4)}

    return {
        "server_match": server_re.pattern,
        "total_calls": total_calls,
        "sessions": sessions,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "by_source": by_source,
        "sources": sources,
        "per_tool": dict(combined.most_common()),
    }


# --- Rendering ---------------------------------------------------------------

def render_text(report):
    if report["total_calls"] == 0:
        return "No matching tool calls found.\n"

    first_ts = report["first_ts"] or "unknown"
    last_ts = report["last_ts"] or "unknown"
    lines = [
        "{total_calls} calls across {sessions} transcript/log files; date range: {first_ts} to {last_ts}".format(
            total_calls=report["total_calls"],
            sessions=report["sessions"],
            first_ts=first_ts,
            last_ts=last_ts,
        ),
        "",
        "By source:",
    ]

    src_width = max((len(name) for name in report["by_source"]), default=0)
    for name, info in sorted(report["by_source"].items(), key=lambda kv: kv[1]["calls"], reverse=True):
        lines.append(
            "  {name:<{w}}  {calls:>6}  {pct:5.1f}%".format(
                name=name, w=src_width, calls=info["calls"], pct=info["share"] * 100
            )
        )

    for name, src in report["sources"].items():
        if not src["per_tool"]:
            continue
        lines.append("")
        lines.append("{name} ({calls} calls, {sessions} files):".format(
            name=name, calls=src["calls"], sessions=src["sessions"]))
        width = max(len(str(c)) for c in src["per_tool"].values())
        for tool, count in src["per_tool"].items():
            lines.append("  {count:>{width}}  {tool}".format(count=count, width=width, tool=tool))

    return "\n".join(lines) + "\n"


def render_json(report):
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Mine Claude Code, Codex, and bridge-audit logs for MCP/bridge tool usage."
    )
    parser.add_argument(
        "--projects-dir",
        default=DEFAULT_PROJECTS_DIR,
        help="Claude Code projects transcript directory (default: %(default)s).",
    )
    parser.add_argument(
        "--codex-dir",
        default=DEFAULT_CODEX_DIR,
        help="Codex rollout sessions directory (default: %(default)s).",
    )
    parser.add_argument(
        "--bridge-audit",
        default=DEFAULT_BRIDGE_AUDIT,
        help="Bridge audit JSONL log (default: %(default)s).",
    )
    parser.add_argument(
        "--sources",
        default=",".join(SOURCES),
        help="Comma-separated subset of sources to include: {0} (default: all).".format(
            ", ".join(SOURCES)
        ),
    )
    parser.add_argument(
        "--exclude-denied",
        action="store_true",
        help="Drop denied/blocked bridge-audit requests from the count.",
    )
    parser.add_argument(
        "--server-match",
        default=DEFAULT_SERVER_MATCH,
        help="Regex matching MCP tool/server names in Claude+Codex logs (default: %(default)s).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: %(default)s).",
    )
    parser.add_argument(
        "--output",
        help="Optional output path; stdout is used when omitted.",
    )
    parser.add_argument(
        "--since",
        help="Only count calls with timestamp >= this ISO date (YYYY-MM-DD).",
    )
    args = parser.parse_args(argv)

    requested = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in requested if s not in SOURCES]
    if unknown:
        parser.error("unknown source(s): {0}; choose from {1}".format(
            ", ".join(unknown), ", ".join(SOURCES)))
    args.sources = requested or list(SOURCES)
    return args


def write_output(text, output_path):
    if output_path is None:
        sys.stdout.write(text)
        return

    with open(Path(output_path).expanduser(), "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        server_re = re.compile(args.server_match)
    except re.error as exc:
        raise SystemExit("invalid --server-match regex: {0}".format(exc))

    report = collect(args, server_re)
    if args.format == "json":
        output = render_json(report)
    else:
        output = render_text(report)
    write_output(output, args.output)


if __name__ == "__main__":
    main()
