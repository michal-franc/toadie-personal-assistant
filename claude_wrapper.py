"""
Claude Wrapper - Persistent Claude process with stdin/stdout communication.

Keeps a single Claude process running and sends prompts via stdin,
avoiding the overhead of spawning a new process for each request.

Uses bidirectional JSON streaming:
- Input: {"type": "user", "message": {"role": "user", "content": "prompt"}}
- Output: stream-json format with result message indicating completion
"""

import json
import os
import select
import subprocess
import threading
import time
from typing import Callable, Optional

from logger import logger

# Tmux session name for terminal output
TMUX_SESSION = "claude-watch"
# Log file for terminal output (tmux runs tail -f on this)
TERMINAL_LOG = "/tmp/claude-watch-output.log"


class ClaudeWrapper:
    """Wrapper for running Claude with persistent stdin/stdout communication."""

    _instance: Optional["ClaudeWrapper"] = None
    _lock = threading.Lock()

    def __init__(self, workdir: str, model: str = None):
        """
        Initialize the wrapper.

        Args:
            workdir: Working directory for Claude
            model: Optional model name (e.g., 'sonnet', 'opus')
        """
        self.workdir = workdir
        self.model = model
        self.process: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = None
        self._running = False
        self._output_lock = threading.Lock()

        # Context/usage tracking
        self.last_usage: Optional[dict] = None
        self.total_cost_usd: float = 0.0
        self.context_window: int = 200000  # Default for Claude

    @classmethod
    def get_instance(cls, workdir: str, model: str = None) -> "ClaudeWrapper":
        """Get or create the singleton wrapper instance."""
        with cls._lock:
            if cls._instance is None or not cls._instance.is_alive():
                cls._instance = cls(workdir, model)
            return cls._instance

    def is_alive(self) -> bool:
        """Check if the Claude process is still running."""
        return self.process is not None and self.process.poll() is None

    def _start_process(self):
        """Start the Claude process if not already running."""
        if self.is_alive():
            return

        cmd = [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            # Note: No --permission-mode flag - permissions handled by PreToolUse hook
        ]
        if self.model:
            cmd.extend(["--model", self.model])

        logger.info(f"[WRAPPER] Starting persistent Claude process: {' '.join(cmd)}")

        # Ensure tmux session exists for terminal output
        self._ensure_tmux_session()

        # Set environment marker so hooks know this is a server session
        env = os.environ.copy()
        env["CLAUDE_WATCH_SESSION"] = "1"

        self.process = subprocess.Popen(
            cmd,
            cwd=self.workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._running = True
        logger.info(f"[WRAPPER] Claude process started (PID: {self.process.pid})")

    def run(
        self,
        prompt: str,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool: Optional[Callable[[str, dict], None]] = None,
        on_result: Optional[Callable[[str], None]] = None,
        on_usage: Optional[Callable[[dict], None]] = None,
        show_terminal: bool = True,
    ) -> str:
        """
        Send a prompt to Claude and stream the response.

        Args:
            prompt: The prompt to send to Claude
            on_text: Callback for text content from assistant
            on_tool: Callback for tool invocations (name, input)
            on_result: Callback when final result is ready
            show_terminal: Whether to show output in tmux session

        Returns:
            The final result text from Claude
        """
        with self._output_lock:
            self._start_process()

            if not self.is_alive():
                raise RuntimeError("Failed to start Claude process")

            logger.info(f"[WRAPPER] Sending prompt: '{prompt[:50]}...'")

            if show_terminal:
                self._write_to_log(f"\n--- New Request ---\n> {prompt}\n")

            # Send prompt as JSON message
            msg = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})

            try:
                self.process.stdin.write(msg + "\n")
                self.process.stdin.flush()
            except BrokenPipeError:
                logger.error("[WRAPPER] Broken pipe - restarting process")
                self._restart_process()
                self.process.stdin.write(msg + "\n")
                self.process.stdin.flush()

            # Process output until we get a result
            result = self._process_output(
                on_text=on_text, on_tool=on_tool, on_result=on_result, on_usage=on_usage, show_terminal=show_terminal
            )

            return result or ""

    def _restart_process(self):
        """Restart the Claude process."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self._start_process()

    def _process_output(
        self,
        on_text: Optional[Callable[[str], None]],
        on_tool: Optional[Callable[[str, dict], None]],
        on_result: Optional[Callable[[str], None]],
        on_usage: Optional[Callable[[dict], None]],
        show_terminal: bool,
        timeout: float = 300,  # 5 minute timeout
    ) -> Optional[str]:
        """Process JSON lines from Claude's stdout until result received."""
        result = None
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Use select to avoid blocking forever
            ready, _, _ = select.select([self.process.stdout], [], [], 1.0)

            if not ready:
                # Check if process died
                if not self.is_alive():
                    logger.error("[WRAPPER] Process died while waiting for output")
                    break
                continue

            line = self.process.stdout.readline()
            if not line:
                continue

            line = line.strip()
            if not line:
                continue

            # Write to log for tmux visibility
            if show_terminal:
                formatted = self._format_for_terminal(line)
                if formatted:
                    self._write_to_log(formatted)

            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"[WRAPPER] Invalid JSON: {e} - {line[:100]}")
                continue

            msg_type = msg.get("type")

            if msg_type == "system":
                subtype = msg.get("subtype")
                if subtype == "init":
                    self.session_id = msg.get("session_id")
                    logger.info(f"[WRAPPER] Session: {self.session_id}")

            elif msg_type == "assistant":
                content = msg.get("message", {}).get("content", [])
                for item in content:
                    item_type = item.get("type")

                    if item_type == "text":
                        text = item.get("text", "")
                        if text:
                            if on_text:
                                on_text(text)
                            logger.debug(f"[WRAPPER] Text: {text[:100]}...")

                    elif item_type == "tool_use":
                        tool_name = item.get("name", "unknown")
                        tool_input = item.get("input", {})
                        if on_tool:
                            on_tool(tool_name, tool_input)
                        logger.debug(f"[WRAPPER] Tool: {tool_name}")

            elif msg_type == "user":
                # Tool results - logged for debugging
                content = msg.get("message", {}).get("content", [])
                for item in content:
                    if item.get("type") == "tool_result":
                        logger.debug("[WRAPPER] Tool result received")

            elif msg_type == "result":
                subtype = msg.get("subtype")
                result = msg.get("result", "")

                if subtype == "success":
                    logger.info(f"[WRAPPER] Success: {result[:100]}...")
                elif subtype == "error":
                    error = msg.get("error", "Unknown error")
                    logger.error(f"[WRAPPER] Error: {error}")
                    result = f"Error: {error}"

                # Extract usage information
                usage = msg.get("usage", {})
                model_usage = msg.get("modelUsage", {})
                total_cost = msg.get("total_cost_usd", 0)

                # Calculate total context tokens
                input_tokens = usage.get("input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_creation = usage.get("cache_creation_input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                total_context = input_tokens + cache_read + cache_creation

                # Get context window from model usage
                for model_info in model_usage.values():
                    if "contextWindow" in model_info:
                        self.context_window = model_info["contextWindow"]
                        break

                self.last_usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_creation_tokens": cache_creation,
                    "total_context": total_context,
                    "context_window": self.context_window,
                    "context_percent": (
                        round(total_context / self.context_window * 100, 1) if self.context_window > 0 else 0
                    ),
                    "cost_usd": total_cost,
                }
                self.total_cost_usd += total_cost

                ctx_pct = self.last_usage["context_percent"]
                logger.info(f"[WRAPPER] Context: {total_context:,}/{self.context_window:,} ({ctx_pct}%)")

                if on_usage:
                    on_usage(self.last_usage)

                if on_result:
                    on_result(result)

                # Result received - stop processing for this request
                return result

        logger.error("[WRAPPER] Timeout waiting for result")
        return result

    def _format_for_terminal(self, json_line: str) -> str:
        """Format JSON line for human-readable terminal output."""
        try:
            msg = json.loads(json_line)
            msg_type = msg.get("type")

            if msg_type == "system":
                subtype = msg.get("subtype", "")
                return f"\033[36m━━━ [{subtype}] Session started ━━━\033[0m"

            elif msg_type == "assistant":
                content = msg.get("message", {}).get("content", [])
                parts = []
                for item in content:
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            # Wrap long text
                            parts.append(f"\033[37m{text}\033[0m")
                    elif item.get("type") == "tool_use":
                        name = item.get("name", "tool")
                        tool_input = item.get("input", {})

                        # Format based on tool type
                        if name == "Bash":
                            cmd = tool_input.get("command", "")
                            desc = tool_input.get("description", "")
                            parts.append(f"\n\033[33m┌─ BASH {'─' * 50}\033[0m")
                            if desc:
                                parts.append(f"\033[33m│\033[0m \033[90m# {desc}\033[0m")
                            parts.append(f"\033[33m│\033[0m \033[93m$ {cmd}\033[0m")
                            parts.append(f"\033[33m└{'─' * 56}\033[0m")
                        elif name == "Write":
                            path = tool_input.get("file_path", "")
                            content_preview = tool_input.get("content", "")[:100]
                            parts.append(f"\n\033[35m┌─ WRITE {'─' * 49}\033[0m")
                            parts.append(f"\033[35m│\033[0m {path}")
                            ellipsis = "..." if len(tool_input.get("content", "")) > 100 else ""
                            parts.append(f"\033[35m│\033[0m \033[90m{content_preview}{ellipsis}\033[0m")
                            parts.append(f"\033[35m└{'─' * 56}\033[0m")
                        elif name == "Edit":
                            path = tool_input.get("file_path", "")
                            old = tool_input.get("old_string", "")[:50]
                            new = tool_input.get("new_string", "")[:50]
                            parts.append(f"\n\033[35m┌─ EDIT {'─' * 50}\033[0m")
                            parts.append(f"\033[35m│\033[0m {path}")
                            old_ellipsis = "..." if len(tool_input.get("old_string", "")) > 50 else ""
                            new_ellipsis = "..." if len(tool_input.get("new_string", "")) > 50 else ""
                            parts.append(f"\033[35m│\033[0m \033[91m- {old}{old_ellipsis}\033[0m")
                            parts.append(f"\033[35m│\033[0m \033[92m+ {new}{new_ellipsis}\033[0m")
                            parts.append(f"\033[35m└{'─' * 56}\033[0m")
                        elif name == "Read":
                            path = tool_input.get("file_path", "")
                            parts.append(f"\033[36m[READ]\033[0m {path}")
                        elif name == "Glob":
                            pattern = tool_input.get("pattern", "")
                            parts.append(f"\033[36m[GLOB]\033[0m {pattern}")
                        elif name == "Grep":
                            pattern = tool_input.get("pattern", "")
                            parts.append(f"\033[36m[GREP]\033[0m {pattern}")
                        else:
                            parts.append(f"\033[33m[{name}]\033[0m")

                return "\n".join(parts) if parts else ""

            elif msg_type == "user":
                content = msg.get("message", {}).get("content", [])
                parts = []
                for item in content:
                    if item.get("type") == "tool_result":
                        result_content = item.get("content", "")
                        is_error = item.get("is_error", False)

                        if is_error:
                            parts.append(f"\033[91m┌─ ERROR {'─' * 49}\033[0m")
                            # Show first few lines of error
                            lines = str(result_content).split("\n")[:5]
                            for line in lines:
                                parts.append(f"\033[91m│\033[0m {line[:80]}")
                            if len(str(result_content).split("\n")) > 5:
                                parts.append("\033[91m│\033[0m ...")
                            parts.append(f"\033[91m└{'─' * 56}\033[0m")
                        else:
                            # Show brief result
                            result_str = str(result_content)
                            if len(result_str) > 200:
                                parts.append(f"\033[32m[✓ result]\033[0m {result_str[:200]}...")
                            elif result_str:
                                lines = result_str.split("\n")[:3]
                                if len(lines) == 1:
                                    parts.append(f"\033[32m[✓]\033[0m {lines[0][:100]}")
                                else:
                                    parts.append("\033[32m[✓ result]\033[0m")
                                    for line in lines:
                                        parts.append(f"    {line[:80]}")
                                    if len(result_str.split("\n")) > 3:
                                        parts.append("    ...")
                            else:
                                parts.append("\033[32m[✓]\033[0m")

                return "\n".join(parts) if parts else ""

            elif msg_type == "result":
                result = msg.get("result", "")
                usage = msg.get("usage", {})
                cost = msg.get("total_cost_usd", 0)

                # Calculate context
                total_ctx = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                )

                output = ["\n\033[32m━━━ DONE ━━━\033[0m"]
                if result:
                    # Show first 300 chars of result
                    preview = result[:300].replace("\n", " ")
                    output.append(f"\033[37m{preview}{'...' if len(result) > 300 else ''}\033[0m")
                output.append(f"\033[90mContext: {total_ctx:,} tokens | Cost: ${cost:.4f}\033[0m")
                return "\n".join(output)

            return ""

        except json.JSONDecodeError:
            return json_line

    def _ensure_tmux_session(self):
        """Ensure tmux session exists for output display."""
        # Check if session exists
        result = subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True)

        if result.returncode != 0:
            # Initialize log file with header
            with open(TERMINAL_LOG, "w") as f:
                f.write("=== Claude Watch Output ===\n")
                f.write(f"Working dir: {self.workdir}\n")
                f.write("=" * 30 + "\n\n")

            # Create new session running tail -f on the log
            subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",  # detached
                    "-s",
                    TMUX_SESSION,
                    "-c",
                    self.workdir,
                    "tail",
                    "-f",
                    TERMINAL_LOG,
                ],
                check=False,
            )
            logger.info(f"[WRAPPER] Created tmux session '{TMUX_SESSION}' (attach with: tmux attach -t {TMUX_SESSION})")

    def _write_to_log(self, text: str):
        """Write text to the terminal log file."""
        try:
            with open(TERMINAL_LOG, "a") as f:
                f.write(text + "\n")
        except Exception as e:
            logger.debug(f"[WRAPPER] log write error: {e}")

    def cancel(self):
        """Cancel the running Claude process."""
        if self.process:
            self.process.terminate()
            logger.info("[WRAPPER] Process terminated")
            self.process = None

    def shutdown(self):
        """Shutdown the persistent process."""
        self.cancel()
        ClaudeWrapper._instance = None
