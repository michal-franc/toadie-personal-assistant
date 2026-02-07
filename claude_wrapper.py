"""
Claude Tmux Session - Run Claude Code interactively inside a tmux session.

Instead of piping stdin/stdout, we launch Claude's full interactive TUI in tmux
and read structured output from the JSONL session files that Claude Code writes
to disk.

Prompts are sent via tmux load-buffer + paste-buffer to avoid escaping issues.
"""

import subprocess
import threading
import time
from typing import Callable, Optional

from logger import logger
from transcript_reader import (
    find_latest_session,
    get_jsonl_line_count,
    get_projects_dir,
    read_context_usage,
    read_new_entries,
)

# Tmux session name for Claude's interactive TUI
TMUX_SESSION = "claude-watch"

# Temp file for prompt delivery via tmux load-buffer
PROMPT_BUFFER_FILE = "/tmp/claude-watch-prompt.txt"

# How long to wait for Claude TUI to initialize before sending first prompt
STARTUP_WAIT = 3.0

# JSONL polling interval (seconds)
POLL_INTERVAL = 0.3

# Idle timeout: after this many seconds with no new JSONL activity, consider the turn done
IDLE_TIMEOUT = 3.0

# Maximum time to wait for a turn to complete (seconds)
TURN_TIMEOUT = 300


class JsonlWatcher:
    """Watch a JSONL file for new entries and fire callbacks."""

    # Entry types to skip when processing
    SKIP_TYPES = frozenset({"file-history-snapshot", "change", "queue-operation"})

    def __init__(self, workdir: str, session_id: str, from_line: int):
        self.workdir = workdir
        self.session_id = session_id
        self._processed_up_to = from_line

    def poll(
        self,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool: Optional[Callable[[str, dict], None]] = None,
    ) -> bool:
        """Poll for new entries and fire callbacks.

        Returns True if new entries were found, False otherwise.
        """
        entries = read_new_entries(self.workdir, self.session_id, self._processed_up_to)
        if not entries:
            return False

        self._processed_up_to += len(entries)
        had_activity = False

        for entry in entries:
            entry_type = entry.get("type")

            # Skip noise entries
            if entry_type in self.SKIP_TYPES:
                continue

            # Skip sidechain (subagent) entries
            if entry.get("isSidechain"):
                continue

            if entry_type == "assistant":
                content = entry.get("message", {}).get("content", [])
                for item in content:
                    item_type = item.get("type")

                    if item_type == "text":
                        text = item.get("text", "")
                        if text and on_text:
                            on_text(text)
                            had_activity = True

                    elif item_type == "tool_use":
                        tool_name = item.get("name", "unknown")
                        tool_input = item.get("input", {})
                        if on_tool:
                            on_tool(tool_name, tool_input)
                        had_activity = True

            elif entry_type == "user":
                # Tool results - just log for debugging
                content = entry.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            logger.debug("[WATCHER] Tool result received")

        return had_activity

    @property
    def current_line(self) -> int:
        return self._processed_up_to


class ClaudeTmuxSession:
    """Run Claude Code interactively in a tmux session, read output from JSONL files."""

    _instance: Optional["ClaudeTmuxSession"] = None
    _lock = threading.Lock()

    def __init__(self, workdir: str, model: str = None):
        self.workdir = workdir
        self.model = model
        self.session_id: Optional[str] = None
        self._output_lock = threading.Lock()

        # Context/usage tracking
        self.last_usage: Optional[dict] = None
        self.total_cost_usd: float = 0.0
        self.context_window: int = 200000

    @classmethod
    def get_instance(cls, workdir: str, model: str = None) -> "ClaudeTmuxSession":
        """Get or create the singleton instance."""
        with cls._lock:
            if cls._instance is None or not cls._instance.is_alive():
                cls._instance = cls(workdir, model)
            return cls._instance

    def is_alive(self) -> bool:
        """Check if the tmux session is running."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", TMUX_SESSION],
            capture_output=True,
        )
        return result.returncode == 0

    def _start_session(self):
        """Start Claude interactively in a tmux session."""
        if self.is_alive():
            return

        # Snapshot existing JSONL files BEFORE starting tmux
        # so we can detect the new file Claude creates
        projects_dir = get_projects_dir(self.workdir)
        existing_files = set()
        if projects_dir.is_dir():
            existing_files = {f.name for f in projects_dir.glob("*.jsonl")}

        cmd = ["tmux", "new-session", "-d", "-s", TMUX_SESSION]

        # Pass environment variable so hooks know this is a server session
        # tmux -e requires tmux 3.2+
        cmd.extend(["-e", "CLAUDE_WATCH_SESSION=1"])

        # Set working directory
        cmd.extend(["-c", self.workdir])

        # The command to run inside tmux: claude [--model X]
        claude_cmd = ["claude"]
        if self.model:
            claude_cmd.extend(["--model", self.model])

        cmd.extend(claude_cmd)

        logger.info(f"[TMUX] Starting session: {' '.join(cmd)}")
        subprocess.run(cmd, check=False)

        # Wait for Claude TUI to initialize and create its JSONL file
        self._discover_session_id(existing_files)

    def _discover_session_id(self, existing_files: set[str]):
        """Wait for Claude to create a JSONL file and extract the session ID.

        Args:
            existing_files: Set of JSONL filenames that existed before tmux started
        """
        projects_dir = get_projects_dir(self.workdir)

        # Poll for new file
        deadline = time.time() + STARTUP_WAIT + 10
        while time.time() < deadline:
            time.sleep(0.5)
            if not projects_dir.is_dir():
                continue

            current_files = {f.name for f in projects_dir.glob("*.jsonl")}
            new_files = current_files - existing_files
            if new_files:
                # Use the newest new file
                newest = max(new_files, key=lambda f: (projects_dir / f).stat().st_mtime)
                self.session_id = newest.replace(".jsonl", "")
                logger.info(f"[TMUX] Discovered session: {self.session_id}")
                return

        # Fallback: use the latest session file
        latest = find_latest_session(self.workdir)
        if latest:
            self.session_id = latest
            logger.info(f"[TMUX] Using latest session (fallback): {self.session_id}")
        else:
            logger.warning("[TMUX] Could not discover session ID")

    def _send_prompt_via_tmux(self, prompt: str):
        """Send a prompt to the Claude TUI using tmux load-buffer + paste-buffer."""
        # Write prompt to temp file (avoids all escaping issues with send-keys)
        with open(PROMPT_BUFFER_FILE, "w") as f:
            f.write(prompt)

        # Load into tmux buffer and paste it
        subprocess.run(["tmux", "load-buffer", PROMPT_BUFFER_FILE], check=False)
        subprocess.run(["tmux", "paste-buffer", "-t", TMUX_SESSION], check=False)

        # Send Enter to submit
        subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Enter"], check=False)

        logger.info(f"[TMUX] Sent prompt: '{prompt[:50]}...'")

    def run(
        self,
        prompt: str,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool: Optional[Callable[[str, dict], None]] = None,
        on_result: Optional[Callable[[str], None]] = None,
        on_usage: Optional[Callable[[dict], None]] = None,
        show_terminal: bool = True,  # kept for backward compat, no-op now
    ) -> str:
        """Send a prompt to Claude and wait for the response.

        Args:
            prompt: The prompt to send to Claude
            on_text: Callback for text content from assistant
            on_tool: Callback for tool invocations (name, input)
            on_result: Callback when final result is ready
            on_usage: Callback for usage/context stats
            show_terminal: Ignored (kept for backward compat)

        Returns:
            The accumulated result text from Claude
        """
        with self._output_lock:
            self._start_session()

            if not self.is_alive():
                raise RuntimeError("Failed to start Claude tmux session")

            # Always refresh session ID to the latest JSONL file.
            # The tmux session may have been reused from a previous run,
            # or Claude may have started a new session within the same tmux.
            latest = find_latest_session(self.workdir)
            if latest:
                if latest != self.session_id:
                    logger.info(f"[TMUX] Session ID updated: {self.session_id} -> {latest}")
                self.session_id = latest

            if not self.session_id:
                raise RuntimeError("No session ID discovered")

            # Record current JSONL position before sending prompt
            start_line = get_jsonl_line_count(self.workdir, self.session_id)

            # Small delay to let the TUI be ready for input
            if start_line == 0:
                time.sleep(STARTUP_WAIT)

            # Send prompt
            self._send_prompt_via_tmux(prompt)

            # After sending, Claude may create a new JSONL file (especially
            # for the first prompt in a new tmux session). Re-check.
            time.sleep(1.0)
            refreshed = find_latest_session(self.workdir)
            if refreshed and refreshed != self.session_id:
                logger.info(f"[TMUX] Session ID changed after prompt: {self.session_id} -> {refreshed}")
                self.session_id = refreshed
                start_line = 0  # New file, start from beginning

            # Poll JSONL for new entries
            watcher = JsonlWatcher(self.workdir, self.session_id, start_line)
            accumulated_text = []

            def text_handler(text):
                accumulated_text.append(text)
                if on_text:
                    on_text(text)
                logger.debug(f"[TMUX] Text: {text[:100]}...")

            last_activity = time.time()
            deadline = time.time() + TURN_TIMEOUT

            while time.time() < deadline:
                had_activity = watcher.poll(on_text=text_handler, on_tool=on_tool)

                if had_activity:
                    last_activity = time.time()

                # Check idle timeout - turn is complete when no new activity
                idle_elapsed = time.time() - last_activity
                if idle_elapsed >= IDLE_TIMEOUT and accumulated_text:
                    logger.info(f"[TMUX] Turn complete (idle {idle_elapsed:.1f}s)")
                    break

                # Also break if tmux session died
                if not self.is_alive():
                    logger.error("[TMUX] Session died while waiting for output")
                    break

                time.sleep(POLL_INTERVAL)

            result = "".join(accumulated_text)

            # Read usage from transcript
            self._update_usage(on_usage)

            if on_result:
                on_result(result)

            return result

    def _update_usage(self, on_usage: Optional[Callable[[dict], None]] = None):
        """Read latest usage info from the transcript."""
        if not self.session_id:
            return

        transcript_usage = read_context_usage(self.workdir, self.session_id)
        if not transcript_usage:
            return

        input_tokens = transcript_usage["input_tokens"]
        cache_read = transcript_usage["cache_read_input_tokens"]
        cache_creation = transcript_usage["cache_creation_input_tokens"]
        output_tokens = transcript_usage["output_tokens"]
        total_context = input_tokens + cache_read + cache_creation

        self.last_usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "total_context": total_context,
            "context_window": self.context_window,
            "context_percent": (round(total_context / self.context_window * 100, 1) if self.context_window > 0 else 0),
            "cost_usd": 0,  # Not available from transcript alone
        }

        ctx_pct = self.last_usage["context_percent"]
        logger.info(f"[TMUX] Context: {total_context:,}/{self.context_window:,} ({ctx_pct}%)")

        if on_usage:
            on_usage(self.last_usage)

    def cancel(self):
        """Cancel the running Claude operation by sending Ctrl+C."""
        if self.is_alive():
            subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"], check=False)
            logger.info("[TMUX] Sent Ctrl+C to cancel")

    def shutdown(self):
        """Kill the tmux session entirely."""
        if self.is_alive():
            subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
            logger.info("[TMUX] Session killed")
        ClaudeTmuxSession._instance = None


# Backward compatibility alias for server.py
ClaudeWrapper = ClaudeTmuxSession
