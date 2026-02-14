"""
Claude Tmux Session - Run Claude Code interactively inside a tmux session.

Instead of piping stdin/stdout, we launch Claude's full interactive TUI in tmux
and read structured output from the JSONL session files that Claude Code writes
to disk.

Prompts are sent via tmux load-buffer + paste-buffer to avoid escaping issues.

Architecture:
- Background watcher thread continuously polls the JSONL file and fires callbacks
- run() sends prompts via tmux, then waits for the watcher to signal turn completion
- Global callbacks (registered once) broadcast to all WebSocket clients
- Per-request callbacks (passed to run()) handle request-specific logic
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

# How often to refresh the session ID in the background watcher (seconds)
SESSION_REFRESH_INTERVAL = 5.0


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
        on_user_message: Optional[Callable[[str], None]] = None,
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
                content = entry.get("message", {}).get("content", [])
                # Check if this is a user prompt (string content or text item)
                if isinstance(content, str) and content.strip():
                    if on_user_message:
                        on_user_message(content)
                    had_activity = True
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text = item.get("text", "")
                                if text and on_user_message:
                                    on_user_message(text)
                                    had_activity = True
                            elif item.get("type") == "tool_result":
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

        # Background watcher state
        self._callbacks: dict = {}
        self._watcher_thread: Optional[threading.Thread] = None
        self._watcher_running = False

        # Per-turn state (managed by background watcher, consumed by run())
        self._pending_text: list[str] = []
        self._turn_complete = threading.Event()

        # Flag: True when run() is active (server-initiated prompt)
        # Used to suppress on_user_message for server prompts (already added by caller)
        self._server_prompt_active = False

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

    def register_callbacks(
        self,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool: Optional[Callable[[str, dict], None]] = None,
        on_user_message: Optional[Callable[[str], None]] = None,
        on_usage: Optional[Callable[[dict], None]] = None,
        on_turn_complete: Optional[Callable[[str, bool], None]] = None,
    ):
        """Register global callbacks fired for all activity.

        These are called by the background watcher for every entry,
        regardless of whether the prompt came from the server or was typed directly.

        Args:
            on_text: Called with text content from assistant messages
            on_tool: Called with (tool_name, tool_input) for tool invocations
            on_user_message: Called with prompt text when a non-server user message is seen
            on_usage: Called with usage dict when a turn ends
            on_turn_complete: Called with (result_text, server_initiated) when a turn ends
        """
        self._callbacks = {
            "on_text": on_text,
            "on_tool": on_tool,
            "on_user_message": on_user_message,
            "on_usage": on_usage,
            "on_turn_complete": on_turn_complete,
        }
        logger.info("[WRAPPER] Global callbacks registered")

    def start_background_watcher(self):
        """Start the background watcher thread that polls JSONL and fires callbacks."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            logger.warning("[WATCHER] Background watcher already running")
            return

        self._watcher_running = True
        self._watcher_thread = threading.Thread(target=self._background_watcher_loop, daemon=True)
        self._watcher_thread.start()
        logger.info("[WATCHER] Background watcher started")

    def _background_watcher_loop(self):
        """Main loop for the background watcher thread.

        Continuously polls the JSONL file for new entries and dispatches
        to registered callbacks. Detects turn completion via idle timeout.
        """
        watcher: Optional[JsonlWatcher] = None
        last_session_refresh = 0.0
        last_activity = 0.0
        accumulated_text: list[str] = []

        while self._watcher_running:
            # Periodically refresh session ID
            now = time.time()
            if now - last_session_refresh > SESSION_REFRESH_INTERVAL:
                latest = find_latest_session(self.workdir)
                if latest and latest != self.session_id:
                    logger.info(f"[WATCHER] Session ID updated: {self.session_id} -> {latest}")
                    self.session_id = latest
                    # Reset watcher for new session
                    watcher = None
                last_session_refresh = now

            if not self.session_id:
                time.sleep(POLL_INTERVAL)
                continue

            # Create watcher if needed (new session or first run)
            if watcher is None or watcher.session_id != self.session_id:
                start_line = get_jsonl_line_count(self.workdir, self.session_id)
                watcher = JsonlWatcher(self.workdir, self.session_id, start_line)
                accumulated_text.clear()
                last_activity = 0.0

            # Build callbacks that fire both global and accumulate for turn detection
            def on_text(text):
                accumulated_text.append(text)
                self._pending_text.append(text)

                cb = self._callbacks.get("on_text")
                if cb:
                    cb(text)
                logger.debug(f"[WATCHER] Text: {text[:100]}...")

            def on_tool(name, tool_input):
                cb = self._callbacks.get("on_tool")
                if cb:
                    cb(name, tool_input)
                logger.debug(f"[WATCHER] Tool: {name}")

            def on_user_message(text):
                if not self._server_prompt_active:
                    cb = self._callbacks.get("on_user_message")
                    if cb:
                        cb(text)
                logger.info(f"[WATCHER] User message: {text[:50]}...")

            had_activity = watcher.poll(on_text=on_text, on_tool=on_tool, on_user_message=on_user_message)

            if had_activity:
                last_activity = time.time()

            # Check idle timeout for turn completion
            if last_activity > 0 and accumulated_text:
                idle_elapsed = time.time() - last_activity
                if idle_elapsed >= IDLE_TIMEOUT:
                    result = "".join(accumulated_text)
                    logger.info(f"[WATCHER] Turn complete (idle {idle_elapsed:.1f}s)")

                    # Fire usage callback
                    self._update_usage(self._callbacks.get("on_usage"))

                    # Fire turn_complete callback
                    cb = self._callbacks.get("on_turn_complete")
                    if cb:
                        cb(result, self._server_prompt_active)

                    # Signal run() if it's waiting
                    self._turn_complete.set()

                    # Reset for next turn
                    accumulated_text.clear()
                    last_activity = 0.0

            time.sleep(POLL_INTERVAL)

        logger.info("[WATCHER] Background watcher stopped")

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

        The background watcher handles JSONL polling and callback firing.
        This method sends the prompt and waits for turn completion.

        Args:
            prompt: The prompt to send to Claude
            on_text: Per-request callback for text content from assistant
            on_tool: Per-request callback for tool invocations (name, input)
            on_result: Per-request callback when final result is ready
            on_usage: Per-request callback for usage/context stats
            show_terminal: Ignored (kept for backward compat)

        Returns:
            The accumulated result text from Claude
        """
        with self._output_lock:
            self._start_session()

            if not self.is_alive():
                raise RuntimeError("Failed to start Claude tmux session")

            # Always refresh session ID to the latest JSONL file.
            latest = find_latest_session(self.workdir)
            if latest:
                if latest != self.session_id:
                    logger.info(f"[TMUX] Session ID updated: {self.session_id} -> {latest}")
                self.session_id = latest

            if not self.session_id:
                raise RuntimeError("No session ID discovered")

            # Small delay to let the TUI be ready for input on first prompt
            start_line = get_jsonl_line_count(self.workdir, self.session_id)
            if start_line == 0:
                time.sleep(STARTUP_WAIT)

            # Clear turn state and set server flag
            self._pending_text.clear()
            self._turn_complete.clear()
            self._server_prompt_active = True

            # Wrap global callbacks with per-request callbacks
            orig_on_text = self._callbacks.get("on_text")
            orig_on_tool = self._callbacks.get("on_tool")
            orig_on_usage = self._callbacks.get("on_usage")

            def combined_on_text(text):
                if orig_on_text:
                    orig_on_text(text)
                if on_text:
                    on_text(text)

            def combined_on_tool(name, tool_input):
                if orig_on_tool:
                    orig_on_tool(name, tool_input)
                if on_tool:
                    on_tool(name, tool_input)

            def combined_on_usage(usage):
                if orig_on_usage:
                    orig_on_usage(usage)
                if on_usage:
                    on_usage(usage)

            # Temporarily replace global callbacks to include per-request ones
            self._callbacks["on_text"] = combined_on_text
            self._callbacks["on_tool"] = combined_on_tool
            self._callbacks["on_usage"] = combined_on_usage

            # Send prompt
            self._send_prompt_via_tmux(prompt)

            # After sending, Claude may create a new JSONL file
            time.sleep(1.0)
            refreshed = find_latest_session(self.workdir)
            if refreshed and refreshed != self.session_id:
                logger.info(f"[TMUX] Session ID changed after prompt: {self.session_id} -> {refreshed}")
                self.session_id = refreshed

            # Wait for the background watcher to signal turn completion
            completed = self._turn_complete.wait(timeout=TURN_TIMEOUT)

            # Restore original global callbacks
            self._callbacks["on_text"] = orig_on_text
            self._callbacks["on_tool"] = orig_on_tool
            self._callbacks["on_usage"] = orig_on_usage
            self._server_prompt_active = False

            if not completed:
                logger.error("[TMUX] Timeout waiting for turn completion")

            result = "".join(self._pending_text)

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
        self._watcher_running = False
        if self.is_alive():
            subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
            logger.info("[TMUX] Session killed")
        ClaudeTmuxSession._instance = None


# Backward compatibility alias for server.py
ClaudeWrapper = ClaudeTmuxSession
