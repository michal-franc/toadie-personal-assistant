"""Unit tests for server.py"""

import json
import os
import sys
import tempfile
import time
from http.client import HTTPConnection
from io import BytesIO
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest


# Mock Deepgram before importing server
sys.modules['deepgram'] = MagicMock()

import server


class TestTranscribeAudio:
    """Tests for transcribe_audio function"""

    def test_transcribe_returns_transcript(self):
        """Should return transcript from Deepgram response"""
        mock_response = MagicMock()
        mock_response.results.channels = [
            MagicMock(alternatives=[MagicMock(transcript="hello world")])
        ]
        server.client.listen.v1.media.transcribe_file.return_value = mock_response

        result = server.transcribe_audio(b"fake audio data")

        assert result == "hello world"

    def test_transcribe_empty_channels(self):
        """Should return empty string when no channels"""
        mock_response = MagicMock()
        mock_response.results.channels = []
        server.client.listen.v1.media.transcribe_file.return_value = mock_response

        result = server.transcribe_audio(b"fake audio data")

        assert result == ""

    def test_transcribe_empty_alternatives(self):
        """Should return empty string when no alternatives"""
        mock_response = MagicMock()
        mock_response.results.channels = [MagicMock(alternatives=[])]
        server.client.listen.v1.media.transcribe_file.return_value = mock_response

        result = server.transcribe_audio(b"fake audio data")

        assert result == ""

    def test_transcribe_no_results_attribute(self):
        """Should return empty string when response has no results"""
        mock_response = MagicMock(spec=[])  # No 'results' attribute
        server.client.listen.v1.media.transcribe_file.return_value = mock_response

        result = server.transcribe_audio(b"fake audio data")

        assert result == ""


class TestRunClaude:
    """Tests for run_claude function"""

    def setup_method(self):
        """Reset cooldown before each test"""
        server.last_claude_launch = 0
        server.claude_workdir = "/tmp"

    @patch("server.ClaudeWrapper")
    def test_run_claude_uses_wrapper(self, mock_wrapper_class):
        """Should use ClaudeWrapper to run Claude"""
        server.claude_workdir = "/home/user/project"
        mock_wrapper = MagicMock()
        mock_wrapper.run.return_value = "test response"
        mock_wrapper.last_usage = None
        mock_wrapper_class.get_instance.return_value = mock_wrapper

        result = server.run_claude("test prompt")

        assert result is True
        mock_wrapper_class.get_instance.assert_called()

    @patch("server.ClaudeWrapper")
    def test_run_claude_cooldown_blocks(self, mock_wrapper_class):
        """Should block new session within cooldown period"""
        mock_wrapper = MagicMock()
        mock_wrapper.run.return_value = "response"
        mock_wrapper.last_usage = None
        mock_wrapper_class.get_instance.return_value = mock_wrapper

        # First call
        server.run_claude("first prompt")

        # Second call - cooldown active
        result = server.run_claude("second prompt")

        assert result is False

    @patch("server.ClaudeWrapper")
    def test_run_claude_passes_model(self, mock_wrapper_class):
        """Should pass model from config to wrapper"""
        server.claude_workdir = "/home/user/project"
        server.transcription_config['claude_model'] = 'opus'
        mock_wrapper = MagicMock()
        mock_wrapper.run.return_value = "response"
        mock_wrapper.last_usage = None
        mock_wrapper_class.get_instance.return_value = mock_wrapper

        server.run_claude("test prompt")

        mock_wrapper_class.get_instance.assert_called_with(
            "/home/user/project", model='opus'
        )

        # Cleanup
        server.transcription_config['claude_model'] = None


class TestDictationHandler:
    """Tests for HTTP request handling"""

    @pytest.fixture
    def mock_handler(self):
        """Create a mock handler for testing"""
        handler = MagicMock(spec=server.DictationHandler)
        handler.headers = {
            'Content-Length': '100',
            'Content-Type': 'audio/mp4'
        }
        handler.path = '/transcribe'
        handler.rfile = BytesIO(b"fake audio data")
        handler.wfile = BytesIO()
        handler.client_address = ('127.0.0.1', 12345)
        return handler

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    @patch("server.transcribe_audio")
    @patch("server.run_claude")
    def test_do_post_success(self, mock_run_claude, mock_transcribe):
        """Should transcribe audio and return transcript"""
        mock_transcribe.return_value = "hello world"

        handler = server.DictationHandler()
        handler.headers = {'Content-Length': '10', 'Content-Type': 'audio/mp4'}
        handler.path = '/transcribe'
        handler.rfile = BytesIO(b"fake audio")
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()

        mock_transcribe.assert_called_once()
        mock_run_claude.assert_called_once()
        # Check first argument is the transcript (second is request_id)
        call_args = mock_run_claude.call_args[0]
        assert call_args[0] == "hello world"

        response = handler.wfile.getvalue()
        data = json.loads(response)
        assert data['status'] == 'ok'
        assert data['transcript'] == 'hello world'

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    @patch("server.transcribe_audio")
    @patch("server.run_claude")
    def test_do_post_empty_transcript(self, mock_run_claude, mock_transcribe):
        """Should not run claude when transcript is empty"""
        mock_transcribe.return_value = ""

        handler = server.DictationHandler()
        handler.headers = {'Content-Length': '10', 'Content-Type': 'audio/mp4'}
        handler.path = '/transcribe'
        handler.rfile = BytesIO(b"fake audio")
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()

        mock_run_claude.assert_not_called()

        response = handler.wfile.getvalue()
        data = json.loads(response)
        assert data['status'] == 'ok'
        assert data['message'] == 'No speech detected'

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    @patch("server.transcribe_audio")
    def test_do_post_error(self, mock_transcribe):
        """Should return 500 on transcription error"""
        mock_transcribe.side_effect = Exception("API error")

        handler = server.DictationHandler()
        handler.headers = {'Content-Length': '10', 'Content-Type': 'audio/mp4'}
        handler.path = '/transcribe'
        handler.rfile = BytesIO(b"fake audio")
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()

        handler.send_response.assert_called_with(500)
        response = handler.wfile.getvalue()
        data = json.loads(response)
        assert data['status'] == 'error'
        assert 'API error' in data['message']

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    def test_do_get_health(self):
        """Should return ok for health check"""
        handler = server.DictationHandler()
        handler.path = '/health'
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()

        handler.send_response.assert_called_with(200)
        response = handler.wfile.getvalue()
        data = json.loads(response)
        assert data['status'] == 'ok'

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    def test_do_get_not_found(self):
        """Should return 404 for unknown paths"""
        handler = server.DictationHandler()
        handler.path = '/unknown'
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()

        handler.send_response.assert_called_with(404)

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    def test_do_get_api_history(self):
        """Should return history JSON"""
        server.request_history = [
            {'id': 1, 'transcript': 'test', 'status': 'completed'}
        ]
        server.claude_workdir = '/test/dir'

        handler = server.DictationHandler()
        handler.path = '/api/history'
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()

        handler.send_response.assert_called_with(200)
        response = handler.wfile.getvalue()
        data = json.loads(response)
        assert data['workdir'] == '/test/dir'
        assert len(data['history']) == 1
        assert data['history'][0]['transcript'] == 'test'

        # Cleanup
        server.request_history = []

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    def test_do_get_dashboard(self):
        """Should serve dashboard HTML"""
        handler = server.DictationHandler()
        handler.path = '/'
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()

        handler.send_response.assert_called_with(200)
        response = handler.wfile.getvalue()
        assert b'<!DOCTYPE html>' in response
        assert b'Claude Watch' in response


class TestMainArgumentParsing:
    """Tests for main() argument parsing"""

    def test_valid_directory(self):
        """Should accept valid directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('sys.argv', ['server.py', tmpdir]):
                with patch.object(server, 'HTTPServer') as mock_server:
                    mock_server.return_value.serve_forever.side_effect = KeyboardInterrupt

                    try:
                        server.main()
                    except SystemExit:
                        pass

                    assert server.claude_workdir == tmpdir

    def test_invalid_directory(self):
        """Should exit with error for invalid directory"""
        with patch('sys.argv', ['server.py', '/nonexistent/path']):
            with pytest.raises(SystemExit) as exc_info:
                server.main()
            assert exc_info.value.code == 1

    def test_missing_argument(self):
        """Should exit with error when no argument provided"""
        with patch('sys.argv', ['server.py']):
            with pytest.raises(SystemExit) as exc_info:
                server.main()
            assert exc_info.value.code == 2  # argparse exits with 2 for missing args

    def test_expands_user_path(self):
        """Should expand ~ in path"""
        home = os.path.expanduser("~")
        with patch('sys.argv', ['server.py', '~']):
            with patch.object(server, 'HTTPServer') as mock_server:
                mock_server.return_value.serve_forever.side_effect = KeyboardInterrupt

                try:
                    server.main()
                except SystemExit:
                    pass

                assert server.claude_workdir == home


class TestPermissionEndpoints:
    """Tests for permission handling endpoints"""

    def setup_method(self):
        """Reset pending permissions before each test"""
        server.pending_permissions = {}

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    @patch('server.broadcast_message')
    def test_permission_request_creates_pending(self, mock_broadcast):
        """Should create pending permission and broadcast"""
        handler = server.DictationHandler()
        handler.path = '/api/permission/request'
        handler.headers = {'Content-Length': '100'}
        handler.rfile = BytesIO(json.dumps({
            'tool_name': 'Bash',
            'tool_input': {'command': 'rm test'},
            'tool_use_id': 'tool123'
        }).encode())
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.handle_permission_request(100)

        # Check response
        handler.send_response.assert_called_with(200)
        response = json.loads(handler.wfile.getvalue())
        assert response['status'] == 'ok'
        assert 'request_id' in response

        # Check pending permission created
        request_id = response['request_id']
        assert request_id in server.pending_permissions
        assert server.pending_permissions[request_id]['tool_name'] == 'Bash'
        assert server.pending_permissions[request_id]['status'] == 'pending'

        # Check broadcasts (prompt + permission)
        assert mock_broadcast.call_count == 2
        # First call is for set_current_prompt (type: prompt)
        prompt_call = mock_broadcast.call_args_list[0][0][0]
        assert prompt_call['type'] == 'prompt'
        assert prompt_call['prompt']['isPermission'] == True
        assert prompt_call['prompt']['request_id'] == request_id
        # Second call is for permission broadcast
        permission_call = mock_broadcast.call_args_list[1][0][0]
        assert permission_call['type'] == 'permission'
        assert permission_call['request_id'] == request_id

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    def test_permission_status_pending(self):
        """Should return pending status"""
        server.pending_permissions['test123'] = {
            'tool_name': 'Bash',
            'status': 'pending',
            'decision': None,
            'reason': None
        }

        handler = server.DictationHandler()
        handler.path = '/api/permission/status/test123'
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.handle_permission_status()

        response = json.loads(handler.wfile.getvalue())
        assert response['status'] == 'pending'
        assert response['decision'] is None

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    def test_permission_status_resolved(self):
        """Should return resolved status with decision"""
        server.pending_permissions['test456'] = {
            'tool_name': 'Write',
            'status': 'resolved',
            'decision': 'allow',
            'reason': 'User approved'
        }

        handler = server.DictationHandler()
        handler.path = '/api/permission/status/test456'
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.handle_permission_status()

        response = json.loads(handler.wfile.getvalue())
        assert response['status'] == 'resolved'
        assert response['decision'] == 'allow'
        assert response['reason'] == 'User approved'

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    def test_permission_status_not_found(self):
        """Should return 404 for unknown request"""
        handler = server.DictationHandler()
        handler.path = '/api/permission/status/unknown'
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.handle_permission_status()

        handler.send_response.assert_called_with(404)

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    @patch('server.broadcast_message')
    def test_permission_respond_allow(self, mock_broadcast):
        """Should update permission to allowed"""
        server.pending_permissions['test789'] = {
            'tool_name': 'Bash',
            'status': 'pending',
            'decision': None,
            'reason': None
        }

        handler = server.DictationHandler()
        handler.path = '/api/permission/respond'
        handler.headers = {'Content-Length': '100'}
        handler.rfile = BytesIO(json.dumps({
            'request_id': 'test789',
            'decision': 'allow',
            'reason': 'User approved'
        }).encode())
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.handle_permission_respond(100)

        handler.send_response.assert_called_with(200)
        assert server.pending_permissions['test789']['status'] == 'resolved'
        assert server.pending_permissions['test789']['decision'] == 'allow'

        # Check broadcast
        mock_broadcast.assert_called_once()
        broadcast_data = mock_broadcast.call_args[0][0]
        assert broadcast_data['type'] == 'permission_resolved'
        assert broadcast_data['decision'] == 'allow'

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    @patch('server.broadcast_message')
    def test_permission_respond_deny(self, mock_broadcast):
        """Should update permission to denied"""
        server.pending_permissions['testdeny'] = {
            'tool_name': 'Write',
            'status': 'pending',
            'decision': None,
            'reason': None
        }

        handler = server.DictationHandler()
        handler.path = '/api/permission/respond'
        handler.headers = {'Content-Length': '100'}
        handler.rfile = BytesIO(json.dumps({
            'request_id': 'testdeny',
            'decision': 'deny',
            'reason': 'Too dangerous'
        }).encode())
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.handle_permission_respond(100)

        assert server.pending_permissions['testdeny']['decision'] == 'deny'
        assert server.pending_permissions['testdeny']['reason'] == 'Too dangerous'

    @patch.object(server.DictationHandler, '__init__', lambda x, *args: None)
    def test_permission_respond_not_found(self):
        """Should return 404 for unknown request"""
        handler = server.DictationHandler()
        handler.path = '/api/permission/respond'
        handler.headers = {'Content-Length': '100'}
        handler.rfile = BytesIO(json.dumps({
            'request_id': 'unknown',
            'decision': 'allow'
        }).encode())
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.handle_permission_respond(100)

        handler.send_response.assert_called_with(404)


class TestCheckHooksConfigured:
    """Tests for check_hooks_configured function"""

    def test_hooks_found_in_project(self, tmp_path):
        """Should find hooks in project settings"""
        # Create project settings with hook
        claude_dir = tmp_path / '.claude'
        claude_dir.mkdir()
        settings = claude_dir / 'settings.json'
        settings.write_text(json.dumps({
            'hooks': {
                'PreToolUse': [{
                    'hooks': [{'command': '/path/to/permission_hook.py'}]
                }]
            }
        }))

        # Should not raise, just print
        server.check_hooks_configured(str(tmp_path))

    def test_hooks_not_found_warns(self, tmp_path, capsys):
        """Should warn when no hooks configured"""
        server.check_hooks_configured(str(tmp_path))

        captured = capsys.readouterr()
        assert 'WARNING' in captured.out
        assert 'hooks not configured' in captured.out.lower()
