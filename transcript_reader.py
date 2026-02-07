"""
Read Claude Code transcript JSONL files to get accurate per-turn context usage.

Claude Code stores transcripts at ~/.claude/projects/<encoded-path>/<session_id>.jsonl
where encoded-path replaces / with - (e.g. /home/user/project -> -home-user-project).

Each assistant entry has message.usage with per-turn token counts reflecting the actual
context window fill level, unlike the cumulative counts in the result message.
"""

import json
from pathlib import Path

from logger import logger


def _encode_workdir(workdir: str) -> str:
    """Encode a working directory path for Claude Code's projects directory."""
    encoded = workdir.replace("/", "-")
    if not encoded.startswith("-"):
        encoded = "-" + encoded
    return encoded


def get_projects_dir(workdir: str) -> Path:
    """Get the Claude Code projects directory for a given working directory."""
    return Path.home() / ".claude" / "projects" / _encode_workdir(workdir)


def get_transcript_path(workdir: str, session_id: str) -> Path:
    """Construct the path to a Claude Code transcript file.

    Args:
        workdir: The working directory Claude was started in
        session_id: The session ID from the init message

    Returns:
        Path to the transcript JSONL file
    """
    return get_projects_dir(workdir) / f"{session_id}.jsonl"


def find_latest_session(workdir: str) -> str | None:
    """Find the most recently modified JSONL session file.

    Args:
        workdir: The working directory Claude was started in

    Returns:
        Session ID (filename without .jsonl) or None if no sessions exist
    """
    projects_dir = get_projects_dir(workdir)
    if not projects_dir.is_dir():
        return None

    jsonl_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return None

    return jsonl_files[0].stem


def get_jsonl_line_count(workdir: str, session_id: str) -> int:
    """Get the current number of lines in a JSONL transcript file.

    Args:
        workdir: The working directory Claude was started in
        session_id: The session ID

    Returns:
        Number of lines, or 0 if the file doesn't exist
    """
    path = get_transcript_path(workdir, session_id)
    try:
        with open(path, "r") as f:
            return sum(1 for _ in f)
    except (FileNotFoundError, PermissionError, OSError):
        return 0


def read_new_entries(workdir: str, session_id: str, from_line: int) -> list[dict]:
    """Read JSONL entries starting from a given line offset.

    Args:
        workdir: The working directory Claude was started in
        session_id: The session ID
        from_line: 0-based line index to start reading from

    Returns:
        List of parsed JSON entries (skips blank lines and invalid JSON)
    """
    path = get_transcript_path(workdir, session_id)
    entries = []
    try:
        with open(path, "r") as f:
            for i, line in enumerate(f):
                if i < from_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, PermissionError, OSError) as e:
        logger.debug(f"[TRANSCRIPT] Cannot read {path}: {e}")
    return entries


def read_context_usage(workdir: str, session_id: str) -> dict | None:
    """Read the last assistant message's usage from a Claude Code transcript.

    Finds the last non-sidechain assistant entry with usage data, which
    reflects the actual current context window fill level.

    Args:
        workdir: The working directory Claude was started in
        session_id: The session ID from the init message

    Returns:
        Dict with input_tokens, cache_read_input_tokens,
        cache_creation_input_tokens, output_tokens, or None if unavailable
    """
    path = get_transcript_path(workdir, session_id)

    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except (FileNotFoundError, PermissionError, OSError) as e:
        logger.debug(f"[TRANSCRIPT] Cannot read {path}: {e}")
        return None

    # Iterate in reverse to find the last valid assistant entry
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue

        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Skip non-assistant entries
        if entry.get("type") != "assistant":
            continue

        # Skip sidechain entries (subagent calls)
        if entry.get("isSidechain"):
            continue

        # Get usage from the message
        message = entry.get("message", {})
        usage = message.get("usage")
        if not usage:
            continue

        result = {
            "input_tokens": usage.get("input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }

        logger.debug(
            f"[TRANSCRIPT] Read usage from {path.name}: "
            f"input={result['input_tokens']}, "
            f"cache_read={result['cache_read_input_tokens']}, "
            f"cache_create={result['cache_creation_input_tokens']}, "
            f"output={result['output_tokens']}"
        )
        return result

    logger.debug(f"[TRANSCRIPT] No valid assistant usage found in {path}")
    return None
