#!/usr/bin/env python3
"""
Permission hook for Claude Code.

Intercepts tool calls and requests approval from the claude-watch server,
which broadcasts to connected mobile/watch apps for user approval.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

# Server configuration
SERVER_HOST = "localhost"
SERVER_PORT = 5566
POLL_INTERVAL = 0.5  # seconds
TIMEOUT = 120  # seconds to wait for approval

# Tools that always need approval
SENSITIVE_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit"}

# Patterns that are auto-approved (safe operations)
AUTO_APPROVE_PATTERNS = {
    "Bash": [
        "ls ",
        "cat ",
        "head ",
        "tail ",
        "grep ",
        "find ",
        "echo ",
        "pwd",
        "whoami",
        "date",
        "which ",
        "type ",
        "file ",
    ],
    "Read": True,  # Always auto-approve reads
    "Glob": True,
    "Grep": True,
}


def is_safe_operation(tool_name: str, tool_input: dict) -> bool:
    """Check if operation is safe to auto-approve."""
    if tool_name in AUTO_APPROVE_PATTERNS:
        patterns = AUTO_APPROVE_PATTERNS[tool_name]
        if patterns is True:
            return True
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            return any(command.startswith(p) for p in patterns)
    return False


def request_permission(tool_name: str, tool_input: dict, tool_use_id: str) -> dict:
    """Send permission request to server and wait for response."""
    url = f"http://{SERVER_HOST}:{SERVER_PORT}/api/permission/request"

    request_data = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
        "timestamp": time.time(),
    }

    # Send request
    try:
        req = urllib.request.Request(
            url, data=json.dumps(request_data).encode(), headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
            request_id = result.get("request_id")
    except Exception as e:
        # If server unavailable, deny by default
        sys.stderr.write(f"Failed to contact server: {e}\n")
        return {"decision": "deny", "reason": "Permission server unavailable"}

    # Poll for response
    poll_url = f"http://{SERVER_HOST}:{SERVER_PORT}/api/permission/status/{request_id}"
    start_time = time.time()

    while time.time() - start_time < TIMEOUT:
        try:
            with urllib.request.urlopen(poll_url, timeout=5) as resp:
                status = json.loads(resp.read().decode())
                if status.get("status") == "pending":
                    time.sleep(POLL_INTERVAL)
                    continue
                return {
                    "decision": status.get("decision", "deny"),
                    "reason": status.get("reason", ""),
                }
        except Exception as e:
            sys.stderr.write(f"Poll error: {e}\n")
            time.sleep(POLL_INTERVAL)

    return {"decision": "deny", "reason": "Permission request timed out"}


def main():
    # Check for bypass mode
    if os.environ.get("CLAUDE_SKIP_HOOKS") == "1":
        sys.exit(0)  # Auto-approve everything

    # Read hook input from stdin
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)  # No input, allow by default

    # For manual sessions (not server-spawned), let Claude show terminal prompt
    if os.environ.get("CLAUDE_WATCH_SESSION") != "1":
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})

        # Auto-approve safe operations
        if is_safe_operation(tool_name, tool_input):
            sys.exit(0)

        # For other operations, return "ask" to show Claude's built-in terminal prompt
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "Manual session - confirm in terminal",
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_use_id = data.get("tool_use_id", "")

    # Check if this tool needs approval
    if tool_name not in SENSITIVE_TOOLS:
        # Non-sensitive tools are auto-approved
        sys.exit(0)

    # Check if it's a safe operation
    if is_safe_operation(tool_name, tool_input):
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Auto-approved safe operation",
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    # Request permission from server/mobile app
    result = request_permission(tool_name, tool_input, tool_use_id)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": result["decision"],
            "permissionDecisionReason": result.get("reason", ""),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
