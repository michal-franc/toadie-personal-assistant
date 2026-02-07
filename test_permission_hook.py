"""Unit tests for permission_hook.py"""

import json
import os
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

# Import the module functions
import permission_hook


class TestIsSafeOperation:
    """Tests for is_safe_operation function"""

    def test_read_always_safe(self):
        """Read tool should always be safe"""
        assert permission_hook.is_safe_operation("Read", {"file_path": "/etc/passwd"}) is True

    def test_glob_always_safe(self):
        """Glob tool should always be safe"""
        assert permission_hook.is_safe_operation("Glob", {"pattern": "**/*.py"}) is True

    def test_grep_always_safe(self):
        """Grep tool should always be safe"""
        assert permission_hook.is_safe_operation("Grep", {"pattern": "TODO"}) is True

    def test_bash_ls_safe(self):
        """ls command should be safe"""
        assert permission_hook.is_safe_operation("Bash", {"command": "ls -la"}) is True

    def test_bash_cat_safe(self):
        """cat command should be safe"""
        assert permission_hook.is_safe_operation("Bash", {"command": "cat file.txt"}) is True

    def test_bash_grep_safe(self):
        """grep command should be safe"""
        assert permission_hook.is_safe_operation("Bash", {"command": "grep pattern file"}) is True

    def test_bash_echo_safe(self):
        """echo command should be safe"""
        assert permission_hook.is_safe_operation("Bash", {"command": "echo hello"}) is True

    def test_bash_rm_not_safe(self):
        """rm command should not be safe"""
        assert permission_hook.is_safe_operation("Bash", {"command": "rm -rf /"}) is False

    def test_bash_sudo_not_safe(self):
        """sudo command should not be safe"""
        assert permission_hook.is_safe_operation("Bash", {"command": "sudo rm file"}) is False

    def test_write_not_safe(self):
        """Write tool should not be safe"""
        assert permission_hook.is_safe_operation("Write", {"file_path": "/tmp/test"}) is False

    def test_edit_not_safe(self):
        """Edit tool should not be safe"""
        assert permission_hook.is_safe_operation("Edit", {"file_path": "/tmp/test"}) is False

    def test_unknown_tool_not_safe(self):
        """Unknown tools should not be safe"""
        assert permission_hook.is_safe_operation("UnknownTool", {}) is False


class TestMainBypassMode:
    """Tests for bypass mode via environment variable"""

    @patch.dict(os.environ, {"CLAUDE_SKIP_HOOKS": "1"})
    def test_skip_hooks_env_exits_zero(self):
        """Should exit 0 when CLAUDE_SKIP_HOOKS=1"""
        with patch("sys.stdin", StringIO("{}")):
            with pytest.raises(SystemExit) as exc_info:
                permission_hook.main()
            assert exc_info.value.code == 0

    @patch.dict(os.environ, {"CLAUDE_SKIP_HOOKS": "0"}, clear=False)
    def test_skip_hooks_zero_continues(self):
        """Should not skip when CLAUDE_SKIP_HOOKS=0"""
        # This should continue to process, not exit early
        with patch("sys.stdin", StringIO('{"tool_name": "Read", "tool_input": {}}')):
            with patch.dict(os.environ, {"CLAUDE_WATCH_SESSION": "1"}):
                with pytest.raises(SystemExit) as exc_info:
                    permission_hook.main()
                # Should exit 0 because Read is safe
                assert exc_info.value.code == 0


class TestMainManualSession:
    """Tests for manual session (no CLAUDE_WATCH_SESSION)"""

    @patch.dict(os.environ, {}, clear=True)
    def test_manual_session_safe_op_auto_approve(self):
        """Manual session with safe op should auto-approve"""
        input_data = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        with patch("sys.stdin", StringIO(json.dumps(input_data))):
            with pytest.raises(SystemExit) as exc_info:
                permission_hook.main()
            assert exc_info.value.code == 0

    @patch.dict(os.environ, {}, clear=True)
    def test_manual_session_unsafe_op_returns_ask(self):
        """Manual session with unsafe op should return 'ask'"""
        input_data = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/test"}}
        captured_output = StringIO()

        with patch("sys.stdin", StringIO(json.dumps(input_data))):
            with patch("sys.stdout", captured_output):
                with pytest.raises(SystemExit) as exc_info:
                    permission_hook.main()

        assert exc_info.value.code == 0
        output = json.loads(captured_output.getvalue())
        assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


class TestMainServerSession:
    """Tests for server-spawned session (CLAUDE_WATCH_SESSION=1)"""

    @patch.dict(os.environ, {"CLAUDE_WATCH_SESSION": "1"})
    def test_server_session_safe_op_auto_approve(self):
        """Server session with safe op should auto-approve"""
        input_data = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        captured_output = StringIO()

        with patch("sys.stdin", StringIO(json.dumps(input_data))):
            with patch("sys.stdout", captured_output):
                with pytest.raises(SystemExit) as exc_info:
                    permission_hook.main()

        assert exc_info.value.code == 0
        output = json.loads(captured_output.getvalue())
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    @patch.dict(os.environ, {"CLAUDE_WATCH_SESSION": "1"})
    @patch("permission_hook.request_permission")
    def test_server_session_unsafe_op_requests_permission(self, mock_request):
        """Server session with unsafe op should request permission from server"""
        mock_request.return_value = {"decision": "allow", "reason": "User approved"}

        input_data = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/test"}, "tool_use_id": "test123"}
        captured_output = StringIO()

        with patch("sys.stdin", StringIO(json.dumps(input_data))):
            with patch("sys.stdout", captured_output):
                with pytest.raises(SystemExit) as exc_info:
                    permission_hook.main()

        assert exc_info.value.code == 0
        mock_request.assert_called_once()
        output = json.loads(captured_output.getvalue())
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    @patch.dict(os.environ, {"CLAUDE_WATCH_SESSION": "1"})
    @patch("permission_hook.request_permission")
    def test_server_session_denied(self, mock_request):
        """Server session denied should return deny"""
        mock_request.return_value = {"decision": "deny", "reason": "User denied"}

        input_data = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/etc/passwd", "content": "bad"},
            "tool_use_id": "test456",
        }
        captured_output = StringIO()

        with patch("sys.stdin", StringIO(json.dumps(input_data))):
            with patch("sys.stdout", captured_output):
                with pytest.raises(SystemExit) as exc_info:
                    permission_hook.main()

        assert exc_info.value.code == 0
        output = json.loads(captured_output.getvalue())
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestMainNonSensitiveTools:
    """Tests for non-sensitive tools"""

    @patch.dict(os.environ, {"CLAUDE_WATCH_SESSION": "1"})
    def test_non_sensitive_tool_auto_approve(self):
        """Non-sensitive tools should auto-approve without server"""
        input_data = {"tool_name": "WebSearch", "tool_input": {"query": "python tutorial"}}

        with patch("sys.stdin", StringIO(json.dumps(input_data))):
            with pytest.raises(SystemExit) as exc_info:
                permission_hook.main()

        # Should exit 0 (auto-approve) without calling server
        assert exc_info.value.code == 0


class TestMainInvalidInput:
    """Tests for invalid input handling"""

    @patch.dict(os.environ, {"CLAUDE_WATCH_SESSION": "1"})
    def test_invalid_json_exits_zero(self):
        """Invalid JSON should exit 0 (allow by default)"""
        with patch("sys.stdin", StringIO("not valid json")):
            with pytest.raises(SystemExit) as exc_info:
                permission_hook.main()
            assert exc_info.value.code == 0

    @patch.dict(os.environ, {"CLAUDE_WATCH_SESSION": "1"})
    def test_empty_input_exits_zero(self):
        """Empty input should exit 0"""
        with patch("sys.stdin", StringIO("")):
            with pytest.raises(SystemExit) as exc_info:
                permission_hook.main()
            assert exc_info.value.code == 0


class TestRequestPermission:
    """Tests for request_permission function"""

    @patch("permission_hook.urllib.request.urlopen")
    def test_request_sends_correct_data(self, mock_urlopen):
        """Should send tool info to server"""
        # Mock initial request
        mock_response1 = MagicMock()
        mock_response1.read.return_value = b'{"request_id": "abc123"}'
        mock_response1.__enter__ = MagicMock(return_value=mock_response1)
        mock_response1.__exit__ = MagicMock(return_value=False)

        # Mock poll response
        mock_response2 = MagicMock()
        mock_response2.read.return_value = b'{"status": "resolved", "decision": "allow"}'
        mock_response2.__enter__ = MagicMock(return_value=mock_response2)
        mock_response2.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [mock_response1, mock_response2]

        result = permission_hook.request_permission("Bash", {"command": "rm test"}, "tool123")

        assert result["decision"] == "allow"

    @patch("permission_hook.urllib.request.urlopen")
    def test_request_server_unavailable_denies(self, mock_urlopen):
        """Should deny when server is unavailable"""
        mock_urlopen.side_effect = Exception("Connection refused")

        result = permission_hook.request_permission("Bash", {"command": "rm test"}, "tool123")

        assert result["decision"] == "deny"
        assert "unavailable" in result["reason"].lower()

    @patch("permission_hook.urllib.request.urlopen")
    @patch("permission_hook.time.time")
    @patch("permission_hook.time.sleep")
    def test_request_timeout_denies(self, mock_sleep, mock_time, mock_urlopen):
        """Should deny when request times out"""
        # Mock initial request
        mock_response1 = MagicMock()
        mock_response1.read.return_value = b'{"request_id": "abc123"}'
        mock_response1.__enter__ = MagicMock(return_value=mock_response1)
        mock_response1.__exit__ = MagicMock(return_value=False)

        # Mock poll responses (always pending)
        mock_response2 = MagicMock()
        mock_response2.read.return_value = b'{"status": "pending"}'
        mock_response2.__enter__ = MagicMock(return_value=mock_response2)
        mock_response2.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [mock_response1] + [mock_response2] * 100

        # Simulate time passing beyond timeout
        mock_time.side_effect = [0, 0, 150]  # Start, first check, timeout exceeded

        result = permission_hook.request_permission("Bash", {"command": "rm test"}, "tool123")

        assert result["decision"] == "deny"
        assert "timed out" in result["reason"].lower()
