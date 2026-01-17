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

    @patch("server.subprocess.Popen")
    def test_run_claude_launches_alacritty(self, mock_popen):
        """Should launch alacritty with claude command"""
        server.claude_workdir = "/home/user/project"

        result = server.run_claude("test prompt")

        assert result is True
        mock_popen.assert_called_once_with([
            'alacritty', '--working-directory', '/home/user/project',
            '-e', 'claude', 'test prompt'
        ])

    @patch("server.subprocess.Popen")
    def test_run_claude_cooldown_blocks_second_call(self, mock_popen):
        """Should block launch within cooldown period"""
        server.run_claude("first prompt")
        result = server.run_claude("second prompt")

        assert result is False
        assert mock_popen.call_count == 1

    @patch("server.subprocess.Popen")
    def test_run_claude_allows_after_cooldown(self, mock_popen):
        """Should allow launch after cooldown expires"""
        server.run_claude("first prompt")
        server.last_claude_launch = time.time() - server.LAUNCH_COOLDOWN - 1

        result = server.run_claude("second prompt")

        assert result is True
        assert mock_popen.call_count == 2


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
        mock_run_claude.assert_called_once_with("hello world")

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
