#!/usr/bin/env python3
import argparse
import collections
import glob
import json
import re
import sys
from pathlib import Path

DEFAULT_PROJECTS_DIR = "~/.claude/projects"
DEFAULT_SERVER_MATCH = r"chrome[-_]devtools"


def iter_tool_uses(path):
    try:
        with open(path, "rb") as handle:
            for raw_line in handle:
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if "tool_use" not in line:
                    continue
                yield from tool_uses_from_line(line)
    except OSError:
        return


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


def timestamp_in_range(timestamp, since):
    if since is None:
        return True
    if timestamp is None:
        return False
    return timestamp >= since


def collect(projects_dir, server_re, since=None):
    counts = collections.Counter()
    sessions = set()
    first_ts = None
    last_ts = None

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
            if timestamp is not None:
                if first_ts is None or timestamp < first_ts:
                    first_ts = timestamp
                if last_ts is None or timestamp > last_ts:
                    last_ts = timestamp
        if file_had_match:
            sessions.add(file_name)

    return {
        "server_match": server_re.pattern,
        "total_calls": sum(counts.values()),
        "sessions": len(sessions),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "per_tool": dict(counts.most_common()),
    }


def render_text(report):
    if report["total_calls"] == 0:
        return "No matching tool calls found.\n"

    first_ts = report["first_ts"] or "unknown"
    last_ts = report["last_ts"] or "unknown"
    lines = [
        "{total_calls} calls across {sessions} transcript files; date range: {first_ts} to {last_ts}".format(
            total_calls=report["total_calls"],
            sessions=report["sessions"],
            first_ts=first_ts,
            last_ts=last_ts,
        )
    ]

    width = max(len(str(count)) for count in report["per_tool"].values())
    for tool, count in report["per_tool"].items():
        lines.append("{count:>{width}}  {tool}".format(count=count, width=width, tool=tool))
    return "\n".join(lines) + "\n"


def render_json(report):
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Mine Claude Code transcripts for MCP server tool usage."
    )
    parser.add_argument(
        "--projects-dir",
        default=DEFAULT_PROJECTS_DIR,
        help="Claude Code projects transcript directory (default: %(default)s).",
    )
    parser.add_argument(
        "--server-match",
        default=DEFAULT_SERVER_MATCH,
        help="Regex used to match MCP tool names (default: %(default)s).",
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
    return parser.parse_args(argv)


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

    report = collect(Path(args.projects_dir).expanduser(), server_re, args.since)
    if args.format == "json":
        output = render_json(report)
    else:
        output = render_text(report)
    write_output(output, args.output)


if __name__ == "__main__":
    main()
