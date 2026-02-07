"""Unit tests for claude_wrapper.py"""

import json
from unittest.mock import MagicMock, patch

import pytest

from claude_wrapper import ClaudeWrapper


class TestClaudeWrapperInit:
    """Tests for ClaudeWrapper initialization"""

    def test_init_sets_workdir(self):
        """Should set working directory"""
        wrapper = ClaudeWrapper("/home/user/project")
        assert wrapper.workdir == "/home/user/project"

    def test_init_sets_model(self):
        """Should set model when provided"""
        wrapper = ClaudeWrapper("/tmp", model="opus")
        assert wrapper.model == "opus"

    def test_init_no_model(self):
        """Should have None model when not provided"""
        wrapper = ClaudeWrapper("/tmp")
        assert wrapper.model is None

    def test_init_no_process(self):
        """Should start with no process"""
        wrapper = ClaudeWrapper("/tmp")
        assert wrapper.process is None
        assert wrapper.session_id is None


class TestClaudeWrapperSingleton:
    """Tests for singleton pattern"""

    def teardown_method(self):
        """Reset singleton after each test"""
        ClaudeWrapper._instance = None

    def test_get_instance_creates_new(self):
        """Should create new instance when none exists"""
        wrapper = ClaudeWrapper.get_instance("/tmp")
        assert wrapper is not None
        assert ClaudeWrapper._instance is wrapper

    @patch.object(ClaudeWrapper, "is_alive", return_value=True)
    def test_get_instance_returns_existing(self, mock_alive):
        """Should return existing instance"""
        wrapper1 = ClaudeWrapper.get_instance("/tmp")
        wrapper2 = ClaudeWrapper.get_instance("/tmp")
        assert wrapper1 is wrapper2

    @patch.object(ClaudeWrapper, "is_alive", return_value=False)
    def test_get_instance_creates_new_when_dead(self, mock_alive):
        """Should create new instance when existing is dead"""
        wrapper1 = ClaudeWrapper.get_instance("/tmp")
        ClaudeWrapper._instance = wrapper1

        # Process is dead, should create new
        wrapper2 = ClaudeWrapper.get_instance("/tmp")
        assert wrapper2 is not wrapper1


class TestClaudeWrapperIsAlive:
    """Tests for is_alive method"""

    def test_is_alive_no_process(self):
        """Should return False when no process"""
        wrapper = ClaudeWrapper("/tmp")
        assert wrapper.is_alive() is False

    def test_is_alive_process_running(self):
        """Should return True when process is running"""
        wrapper = ClaudeWrapper("/tmp")
        wrapper.process = MagicMock()
        wrapper.process.poll.return_value = None  # None means still running
        assert wrapper.is_alive() is True

    def test_is_alive_process_exited(self):
        """Should return False when process has exited"""
        wrapper = ClaudeWrapper("/tmp")
        wrapper.process = MagicMock()
        wrapper.process.poll.return_value = 0  # Exit code means finished
        assert wrapper.is_alive() is False


class TestClaudeWrapperStartProcess:
    """Tests for _start_process method"""

    def teardown_method(self):
        ClaudeWrapper._instance = None

    @patch("claude_wrapper.subprocess.Popen")
    @patch("claude_wrapper.subprocess.run")
    def test_start_process_creates_popen(self, mock_run, mock_popen):
        """Should create subprocess with correct args"""
        mock_run.return_value.returncode = 0  # tmux session exists
        mock_popen.return_value.pid = 12345

        wrapper = ClaudeWrapper("/tmp")
        wrapper._start_process()

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        assert "claude" in cmd
        assert "-p" in cmd
        assert "--input-format" in cmd
        assert "stream-json" in cmd
        assert "--output-format" in cmd

    @patch("claude_wrapper.subprocess.Popen")
    @patch("claude_wrapper.subprocess.run")
    def test_start_process_sets_env_marker(self, mock_run, mock_popen):
        """Should set CLAUDE_WATCH_SESSION env var"""
        mock_run.return_value.returncode = 0
        mock_popen.return_value.pid = 12345

        wrapper = ClaudeWrapper("/tmp")
        wrapper._start_process()

        call_args = mock_popen.call_args
        env = call_args[1].get("env", {})
        assert env.get("CLAUDE_WATCH_SESSION") == "1"

    @patch("claude_wrapper.subprocess.Popen")
    @patch("claude_wrapper.subprocess.run")
    def test_start_process_includes_model(self, mock_run, mock_popen):
        """Should include model flag when specified"""
        mock_run.return_value.returncode = 0
        mock_popen.return_value.pid = 12345

        wrapper = ClaudeWrapper("/tmp", model="opus")
        wrapper._start_process()

        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "--model" in cmd
        assert "opus" in cmd

    @patch.object(ClaudeWrapper, "is_alive", return_value=True)
    def test_start_process_noop_when_alive(self, mock_alive):
        """Should not start new process when already running"""
        wrapper = ClaudeWrapper("/tmp")
        wrapper.process = MagicMock()

        with patch("claude_wrapper.subprocess.Popen") as mock_popen:
            wrapper._start_process()
            mock_popen.assert_not_called()


class TestClaudeWrapperRun:
    """Tests for run method"""

    def teardown_method(self):
        ClaudeWrapper._instance = None

    @patch.object(ClaudeWrapper, "_start_process")
    @patch.object(ClaudeWrapper, "_process_output")
    @patch.object(ClaudeWrapper, "is_alive", return_value=True)
    @patch.object(ClaudeWrapper, "_write_to_log")
    def test_run_sends_json_message(self, mock_log, mock_alive, mock_process, mock_start):
        """Should send prompt as JSON to stdin"""
        mock_process.return_value = "response"

        wrapper = ClaudeWrapper("/tmp")
        wrapper.process = MagicMock()
        wrapper.process.stdin = MagicMock()

        wrapper.run("test prompt")

        # Check JSON was written to stdin
        write_calls = wrapper.process.stdin.write.call_args_list
        assert len(write_calls) > 0

        written = write_calls[0][0][0]
        msg = json.loads(written.strip())
        assert msg["type"] == "user"
        assert msg["message"]["role"] == "user"
        assert msg["message"]["content"] == "test prompt"

    @patch.object(ClaudeWrapper, "_start_process")
    @patch.object(ClaudeWrapper, "is_alive", return_value=False)
    def test_run_raises_when_process_fails(self, mock_alive, mock_start):
        """Should raise error when process fails to start"""
        wrapper = ClaudeWrapper("/tmp")

        with pytest.raises(RuntimeError, match="Failed to start"):
            wrapper.run("test")


class TestClaudeWrapperUsageTracking:
    """Tests for usage/context tracking"""

    def test_init_usage_defaults(self):
        """Should initialize with default usage values"""
        wrapper = ClaudeWrapper("/tmp")
        assert wrapper.last_usage is None
        assert wrapper.total_cost_usd == 0.0
        assert wrapper.context_window == 200000


class TestClaudeWrapperFormatTerminal:
    """Tests for _format_for_terminal method"""

    def test_format_system_init(self):
        """Should format system init message"""
        wrapper = ClaudeWrapper("/tmp")
        msg = json.dumps({"type": "system", "subtype": "init"})
        result = wrapper._format_for_terminal(msg)
        assert "init" in result
        assert "Session started" in result

    def test_format_bash_tool(self):
        """Should format Bash tool nicely"""
        wrapper = ClaudeWrapper("/tmp")
        msg = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls -la", "description": "List files"},
                        }
                    ]
                },
            }
        )
        result = wrapper._format_for_terminal(msg)
        assert "BASH" in result
        assert "ls -la" in result
        assert "List files" in result

    def test_format_edit_tool(self):
        """Should format Edit tool with diff preview"""
        wrapper = ClaudeWrapper("/tmp")
        msg = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "/tmp/test.py", "old_string": "old code", "new_string": "new code"},
                        }
                    ]
                },
            }
        )
        result = wrapper._format_for_terminal(msg)
        assert "EDIT" in result
        assert "/tmp/test.py" in result
        assert "old code" in result
        assert "new code" in result

    def test_format_tool_result_success(self):
        """Should format successful tool result"""
        wrapper = ClaudeWrapper("/tmp")
        msg = json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "file created", "is_error": False}]},
            }
        )
        result = wrapper._format_for_terminal(msg)
        assert "✓" in result

    def test_format_tool_result_error(self):
        """Should format error tool result"""
        wrapper = ClaudeWrapper("/tmp")
        msg = json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "Permission denied", "is_error": True}]},
            }
        )
        result = wrapper._format_for_terminal(msg)
        assert "ERROR" in result

    def test_format_result_with_usage(self):
        """Should format result with context info"""
        wrapper = ClaudeWrapper("/tmp")
        msg = json.dumps(
            {
                "type": "result",
                "result": "Done!",
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 1000},
                "total_cost_usd": 0.05,
            }
        )
        result = wrapper._format_for_terminal(msg)
        assert "DONE" in result
        assert "6,100" in result  # total context
        assert "0.05" in result

    def test_format_invalid_json(self):
        """Should return original line for invalid JSON"""
        wrapper = ClaudeWrapper("/tmp")
        result = wrapper._format_for_terminal("not json")
        assert result == "not json"


class TestClaudeWrapperCancel:
    """Tests for cancel method"""

    def test_cancel_terminates_process(self):
        """Should terminate running process"""
        wrapper = ClaudeWrapper("/tmp")
        mock_process = MagicMock()
        wrapper.process = mock_process

        wrapper.cancel()

        mock_process.terminate.assert_called_once()
        assert wrapper.process is None

    def test_cancel_noop_no_process(self):
        """Should do nothing when no process"""
        wrapper = ClaudeWrapper("/tmp")
        wrapper.cancel()  # Should not raise


class TestClaudeWrapperShutdown:
    """Tests for shutdown method"""

    def teardown_method(self):
        ClaudeWrapper._instance = None

    def test_shutdown_clears_singleton(self):
        """Should clear singleton instance"""
        wrapper = ClaudeWrapper.get_instance("/tmp")
        assert ClaudeWrapper._instance is not None

        wrapper.shutdown()

        assert ClaudeWrapper._instance is None
