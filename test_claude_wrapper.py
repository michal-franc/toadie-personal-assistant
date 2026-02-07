"""Unit tests for claude_wrapper.py (ClaudeTmuxSession + JsonlWatcher)"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from claude_wrapper import ClaudeTmuxSession, ClaudeWrapper, JsonlWatcher


class TestBackwardCompatAlias:
    """ClaudeWrapper should be an alias for ClaudeTmuxSession"""

    def test_alias_points_to_new_class(self):
        assert ClaudeWrapper is ClaudeTmuxSession


class TestClaudeTmuxSessionInit:
    """Tests for ClaudeTmuxSession initialization"""

    def test_init_sets_workdir(self):
        session = ClaudeTmuxSession("/home/user/project")
        assert session.workdir == "/home/user/project"

    def test_init_sets_model(self):
        session = ClaudeTmuxSession("/tmp", model="opus")
        assert session.model == "opus"

    def test_init_no_model(self):
        session = ClaudeTmuxSession("/tmp")
        assert session.model is None

    def test_init_no_session_id(self):
        session = ClaudeTmuxSession("/tmp")
        assert session.session_id is None


class TestClaudeTmuxSessionSingleton:
    """Tests for singleton pattern"""

    def teardown_method(self):
        ClaudeTmuxSession._instance = None

    @patch.object(ClaudeTmuxSession, "is_alive", return_value=False)
    def test_get_instance_creates_new(self, mock_alive):
        wrapper = ClaudeTmuxSession.get_instance("/tmp")
        assert wrapper is not None
        assert ClaudeTmuxSession._instance is wrapper

    @patch.object(ClaudeTmuxSession, "is_alive", return_value=True)
    def test_get_instance_returns_existing(self, mock_alive):
        wrapper1 = ClaudeTmuxSession.get_instance("/tmp")
        wrapper2 = ClaudeTmuxSession.get_instance("/tmp")
        assert wrapper1 is wrapper2

    @patch.object(ClaudeTmuxSession, "is_alive", return_value=False)
    def test_get_instance_creates_new_when_dead(self, mock_alive):
        wrapper1 = ClaudeTmuxSession.get_instance("/tmp")
        ClaudeTmuxSession._instance = wrapper1

        wrapper2 = ClaudeTmuxSession.get_instance("/tmp")
        assert wrapper2 is not wrapper1


class TestClaudeTmuxSessionIsAlive:
    """Tests for is_alive method"""

    @patch("claude_wrapper.subprocess.run")
    def test_is_alive_when_tmux_exists(self, mock_run):
        mock_run.return_value.returncode = 0
        session = ClaudeTmuxSession("/tmp")
        assert session.is_alive() is True
        mock_run.assert_called_with(
            ["tmux", "has-session", "-t", "claude-watch"],
            capture_output=True,
        )

    @patch("claude_wrapper.subprocess.run")
    def test_is_alive_when_tmux_missing(self, mock_run):
        mock_run.return_value.returncode = 1
        session = ClaudeTmuxSession("/tmp")
        assert session.is_alive() is False


class TestClaudeTmuxSessionStartSession:
    """Tests for _start_session method"""

    def teardown_method(self):
        ClaudeTmuxSession._instance = None

    @patch.object(ClaudeTmuxSession, "_discover_session_id")
    @patch("claude_wrapper.get_projects_dir")
    @patch("claude_wrapper.subprocess.run")
    def test_start_session_creates_tmux(self, mock_run, mock_projects_dir, mock_discover):
        # First call: has-session returns 1 (not running)
        # Second call: new-session returns 0
        mock_run.return_value.returncode = 1

        session = ClaudeTmuxSession("/tmp/project")
        session._start_session()

        # Check that tmux new-session was called
        calls = mock_run.call_args_list
        # First call is is_alive check (has-session), second is new-session
        new_session_call = calls[1]
        cmd = new_session_call[0][0]

        assert "tmux" in cmd
        assert "new-session" in cmd
        assert "-d" in cmd
        assert "-s" in cmd
        assert "claude-watch" in cmd
        assert "claude" in cmd

    @patch.object(ClaudeTmuxSession, "_discover_session_id")
    @patch("claude_wrapper.get_projects_dir")
    @patch("claude_wrapper.subprocess.run")
    def test_start_session_passes_env(self, mock_run, mock_projects_dir, mock_discover):
        mock_run.return_value.returncode = 1

        session = ClaudeTmuxSession("/tmp")
        session._start_session()

        calls = mock_run.call_args_list
        new_session_call = calls[1]
        cmd = new_session_call[0][0]

        # Should include -e CLAUDE_WATCH_SESSION=1
        assert "-e" in cmd
        assert "CLAUDE_WATCH_SESSION=1" in cmd

    @patch.object(ClaudeTmuxSession, "_discover_session_id")
    @patch("claude_wrapper.get_projects_dir")
    @patch("claude_wrapper.subprocess.run")
    def test_start_session_includes_model(self, mock_run, mock_projects_dir, mock_discover):
        mock_run.return_value.returncode = 1

        session = ClaudeTmuxSession("/tmp", model="opus")
        session._start_session()

        calls = mock_run.call_args_list
        new_session_call = calls[1]
        cmd = new_session_call[0][0]

        assert "--model" in cmd
        assert "opus" in cmd

    @patch.object(ClaudeTmuxSession, "is_alive", return_value=True)
    @patch("claude_wrapper.subprocess.run")
    def test_start_session_noop_when_alive(self, mock_run, mock_alive):
        session = ClaudeTmuxSession("/tmp")
        session._start_session()
        # subprocess.run should not be called (is_alive is patched)
        mock_run.assert_not_called()


class TestClaudeTmuxSessionSendPrompt:
    """Tests for _send_prompt_via_tmux"""

    @patch("claude_wrapper.subprocess.run")
    def test_send_prompt_uses_load_buffer(self, mock_run):
        session = ClaudeTmuxSession("/tmp")

        with patch("builtins.open", create=True) as mock_open:
            mock_file = MagicMock()
            mock_open.return_value.__enter__ = MagicMock(return_value=mock_file)
            mock_open.return_value.__exit__ = MagicMock(return_value=False)

            session._send_prompt_via_tmux("test prompt")

        # Should make 3 subprocess calls: load-buffer, paste-buffer, send-keys Enter
        assert mock_run.call_count == 3

        load_call = mock_run.call_args_list[0][0][0]
        assert "load-buffer" in load_call

        paste_call = mock_run.call_args_list[1][0][0]
        assert "paste-buffer" in paste_call

        enter_call = mock_run.call_args_list[2][0][0]
        assert "send-keys" in enter_call
        assert "Enter" in enter_call


class TestClaudeTmuxSessionRun:
    """Tests for run method"""

    def teardown_method(self):
        ClaudeTmuxSession._instance = None

    @patch.object(ClaudeTmuxSession, "_update_usage")
    @patch.object(ClaudeTmuxSession, "_send_prompt_via_tmux")
    @patch("claude_wrapper.find_latest_session", return_value=None)
    @patch("claude_wrapper.get_jsonl_line_count", return_value=5)
    @patch.object(ClaudeTmuxSession, "is_alive", return_value=True)
    @patch.object(ClaudeTmuxSession, "_start_session")
    def test_run_raises_when_no_session_id(
        self, mock_start, mock_alive, mock_count, mock_latest, mock_send, mock_usage
    ):
        session = ClaudeTmuxSession("/tmp")
        session.session_id = None

        with pytest.raises(RuntimeError, match="No session ID"):
            session.run("test")

    @patch.object(ClaudeTmuxSession, "_update_usage")
    @patch.object(ClaudeTmuxSession, "_start_session")
    @patch.object(ClaudeTmuxSession, "is_alive", return_value=False)
    def test_run_raises_when_session_fails(self, mock_alive, mock_start, mock_usage):
        session = ClaudeTmuxSession("/tmp")

        with pytest.raises(RuntimeError, match="Failed to start"):
            session.run("test")


class TestClaudeTmuxSessionUsageTracking:
    """Tests for usage/context tracking"""

    def test_init_usage_defaults(self):
        session = ClaudeTmuxSession("/tmp")
        assert session.last_usage is None
        assert session.total_cost_usd == 0.0
        assert session.context_window == 200000

    @patch("claude_wrapper.read_context_usage")
    def test_update_usage_fires_callback(self, mock_read):
        mock_read.return_value = {
            "input_tokens": 1000,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 100,
        }

        session = ClaudeTmuxSession("/tmp")
        session.session_id = "test-session"
        callback = MagicMock()

        session._update_usage(on_usage=callback)

        callback.assert_called_once()
        usage = callback.call_args[0][0]
        assert usage["input_tokens"] == 1000
        assert usage["total_context"] == 6200  # 1000 + 5000 + 200
        assert usage["context_window"] == 200000

    @patch("claude_wrapper.read_context_usage", return_value=None)
    def test_update_usage_noop_when_no_transcript(self, mock_read):
        session = ClaudeTmuxSession("/tmp")
        session.session_id = "test-session"
        callback = MagicMock()

        session._update_usage(on_usage=callback)

        callback.assert_not_called()


class TestClaudeTmuxSessionCancel:
    """Tests for cancel method"""

    @patch("claude_wrapper.subprocess.run")
    def test_cancel_sends_ctrl_c(self, mock_run):
        # First call (is_alive check) returns success
        mock_run.return_value.returncode = 0

        session = ClaudeTmuxSession("/tmp")
        session.cancel()

        # Should have called has-session and then send-keys C-c
        assert mock_run.call_count == 2
        cancel_call = mock_run.call_args_list[1][0][0]
        assert "send-keys" in cancel_call
        assert "C-c" in cancel_call

    @patch("claude_wrapper.subprocess.run")
    def test_cancel_noop_when_not_alive(self, mock_run):
        mock_run.return_value.returncode = 1  # session doesn't exist
        session = ClaudeTmuxSession("/tmp")
        session.cancel()

        # Only has-session check, no send-keys
        assert mock_run.call_count == 1


class TestClaudeTmuxSessionShutdown:
    """Tests for shutdown method"""

    def teardown_method(self):
        ClaudeTmuxSession._instance = None

    @patch("claude_wrapper.subprocess.run")
    def test_shutdown_kills_session(self, mock_run):
        mock_run.return_value.returncode = 0  # session exists

        wrapper = ClaudeTmuxSession.get_instance("/tmp")
        wrapper.shutdown()

        # Should have called kill-session
        kill_calls = [c for c in mock_run.call_args_list if "kill-session" in c[0][0]]
        assert len(kill_calls) == 1
        assert ClaudeTmuxSession._instance is None

    @patch("claude_wrapper.subprocess.run")
    def test_shutdown_clears_singleton_even_when_dead(self, mock_run):
        mock_run.return_value.returncode = 1  # session doesn't exist

        ClaudeTmuxSession._instance = ClaudeTmuxSession("/tmp")
        ClaudeTmuxSession._instance.shutdown()

        assert ClaudeTmuxSession._instance is None


class TestJsonlWatcher:
    """Tests for JsonlWatcher"""

    def test_init(self):
        watcher = JsonlWatcher("/tmp", "session-1", 0)
        assert watcher.workdir == "/tmp"
        assert watcher.session_id == "session-1"
        assert watcher.current_line == 0

    @patch("claude_wrapper.read_new_entries")
    def test_poll_returns_false_no_entries(self, mock_read):
        mock_read.return_value = []
        watcher = JsonlWatcher("/tmp", "sess", 0)
        assert watcher.poll() is False

    @patch("claude_wrapper.read_new_entries")
    def test_poll_fires_on_text(self, mock_read):
        mock_read.return_value = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello world"}]},
            }
        ]

        watcher = JsonlWatcher("/tmp", "sess", 0)
        text_cb = MagicMock()
        result = watcher.poll(on_text=text_cb)

        assert result is True
        text_cb.assert_called_once_with("Hello world")
        assert watcher.current_line == 1

    @patch("claude_wrapper.read_new_entries")
    def test_poll_fires_on_tool(self, mock_read):
        mock_read.return_value = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    ]
                },
            }
        ]

        watcher = JsonlWatcher("/tmp", "sess", 0)
        tool_cb = MagicMock()
        result = watcher.poll(on_tool=tool_cb)

        assert result is True
        tool_cb.assert_called_once_with("Bash", {"command": "ls"})

    @patch("claude_wrapper.read_new_entries")
    def test_poll_skips_noise_types(self, mock_read):
        mock_read.return_value = [
            {"type": "file-history-snapshot", "data": {}},
            {"type": "change", "data": {}},
            {"type": "queue-operation", "data": {}},
        ]

        watcher = JsonlWatcher("/tmp", "sess", 0)
        text_cb = MagicMock()
        result = watcher.poll(on_text=text_cb)

        assert result is False
        text_cb.assert_not_called()
        # But line count still advances
        assert watcher.current_line == 3

    @patch("claude_wrapper.read_new_entries")
    def test_poll_skips_sidechain(self, mock_read):
        mock_read.return_value = [
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {"content": [{"type": "text", "text": "sidechain text"}]},
            }
        ]

        watcher = JsonlWatcher("/tmp", "sess", 0)
        text_cb = MagicMock()
        result = watcher.poll(on_text=text_cb)

        assert result is False
        text_cb.assert_not_called()

    @patch("claude_wrapper.read_new_entries")
    def test_poll_advances_line_count(self, mock_read):
        mock_read.return_value = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        ]

        watcher = JsonlWatcher("/tmp", "sess", 5)
        watcher.poll(on_text=MagicMock())

        assert watcher.current_line == 7

    @patch("claude_wrapper.read_new_entries")
    def test_poll_skips_empty_text(self, mock_read):
        mock_read.return_value = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": ""}]}},
        ]

        watcher = JsonlWatcher("/tmp", "sess", 0)
        text_cb = MagicMock()
        result = watcher.poll(on_text=text_cb)

        assert result is False
        text_cb.assert_not_called()

    @patch("claude_wrapper.read_new_entries")
    def test_poll_fires_on_user_message_string(self, mock_read):
        mock_read.return_value = [
            {"type": "user", "message": {"content": "hello from tmux"}},
        ]

        watcher = JsonlWatcher("/tmp", "sess", 0)
        user_cb = MagicMock()
        result = watcher.poll(on_user_message=user_cb)

        assert result is True
        user_cb.assert_called_once_with("hello from tmux")

    @patch("claude_wrapper.read_new_entries")
    def test_poll_fires_on_user_message_text_item(self, mock_read):
        mock_read.return_value = [
            {"type": "user", "message": {"content": [{"type": "text", "text": "typed prompt"}]}},
        ]

        watcher = JsonlWatcher("/tmp", "sess", 0)
        user_cb = MagicMock()
        result = watcher.poll(on_user_message=user_cb)

        assert result is True
        user_cb.assert_called_once_with("typed prompt")

    @patch("claude_wrapper.read_new_entries")
    def test_poll_does_not_fire_user_message_for_tool_results(self, mock_read):
        mock_read.return_value = [
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        ]

        watcher = JsonlWatcher("/tmp", "sess", 0)
        user_cb = MagicMock()
        watcher.poll(on_user_message=user_cb)

        user_cb.assert_not_called()


class TestClaudeTmuxSessionRegisterCallbacks:
    """Tests for register_callbacks"""

    def test_register_stores_all_callbacks(self):
        session = ClaudeTmuxSession("/tmp")
        on_text = MagicMock()
        on_tool = MagicMock()
        on_user = MagicMock()
        on_usage = MagicMock()
        on_turn = MagicMock()

        session.register_callbacks(
            on_text=on_text,
            on_tool=on_tool,
            on_user_message=on_user,
            on_usage=on_usage,
            on_turn_complete=on_turn,
        )

        assert session._callbacks["on_text"] is on_text
        assert session._callbacks["on_tool"] is on_tool
        assert session._callbacks["on_user_message"] is on_user
        assert session._callbacks["on_usage"] is on_usage
        assert session._callbacks["on_turn_complete"] is on_turn

    def test_register_allows_partial(self):
        session = ClaudeTmuxSession("/tmp")
        on_text = MagicMock()

        session.register_callbacks(on_text=on_text)

        assert session._callbacks["on_text"] is on_text
        assert session._callbacks["on_tool"] is None


class TestClaudeTmuxSessionBackgroundWatcher:
    """Tests for start_background_watcher"""

    def test_starts_daemon_thread(self):
        session = ClaudeTmuxSession("/tmp")

        with patch.object(session, "_background_watcher_loop"):
            session.start_background_watcher()

        assert session._watcher_running is True
        assert session._watcher_thread is not None
        assert session._watcher_thread.daemon is True

        session._watcher_running = False

    def test_noop_when_already_running(self):
        session = ClaudeTmuxSession("/tmp")

        keep_alive = threading.Event()

        def fake_loop():
            keep_alive.wait(timeout=5)

        with patch.object(session, "_background_watcher_loop", side_effect=fake_loop):
            session.start_background_watcher()
            first_thread = session._watcher_thread
            session.start_background_watcher()
            assert session._watcher_thread is first_thread

        session._watcher_running = False
        keep_alive.set()


class TestClaudeTmuxSessionInitState:
    """Tests for new init state"""

    def test_init_watcher_state(self):
        session = ClaudeTmuxSession("/tmp")
        assert session._callbacks == {}
        assert session._watcher_thread is None
        assert session._watcher_running is False
        assert session._pending_text == []
        assert not session._turn_complete.is_set()
        assert session._server_prompt_active is False

    def test_shutdown_stops_watcher(self):
        session = ClaudeTmuxSession("/tmp")
        session._watcher_running = True

        with patch("claude_wrapper.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1  # session doesn't exist
            session.shutdown()

        assert session._watcher_running is False
