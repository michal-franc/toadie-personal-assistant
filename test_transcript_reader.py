"""Unit tests for transcript_reader.py"""

import json
import time
from pathlib import Path
from unittest.mock import patch

from transcript_reader import (
    find_latest_session,
    get_jsonl_line_count,
    get_projects_dir,
    get_transcript_path,
    read_context_usage,
    read_new_entries,
)


class TestGetTranscriptPath:
    """Tests for get_transcript_path"""

    def test_encodes_absolute_path(self):
        """Should replace / with - in workdir"""
        path = get_transcript_path("/home/user/project", "abc-123")
        assert path == Path.home() / ".claude/projects/-home-user-project/abc-123.jsonl"

    def test_encodes_nested_path(self):
        """Should handle deeply nested paths"""
        path = get_transcript_path("/home/user/work/my-project", "sess-1")
        assert path == Path.home() / ".claude/projects/-home-user-work-my-project/sess-1.jsonl"

    def test_prepends_dash_if_missing(self):
        """Should prepend - if workdir doesn't start with /"""
        path = get_transcript_path("relative/path", "sess-1")
        assert path == Path.home() / ".claude/projects/-relative-path/sess-1.jsonl"

    def test_no_double_dash(self):
        """Should not double the leading dash for absolute paths"""
        path = get_transcript_path("/home/user", "sess-1")
        encoded = path.parent.name
        assert encoded == "-home-user"
        assert not encoded.startswith("--")


class TestReadContextUsage:
    """Tests for read_context_usage"""

    def _make_assistant_entry(self, usage, is_sidechain=False):
        """Helper to create an assistant transcript entry."""
        entry = {
            "type": "assistant",
            "isSidechain": is_sidechain,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
                "usage": usage,
            },
        }
        return json.dumps(entry)

    def _make_user_entry(self):
        """Helper to create a user transcript entry."""
        return json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "test prompt"},
            }
        )

    def test_returns_usage_from_last_assistant(self, tmp_path):
        """Should return usage from the last assistant entry"""
        usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        }
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(self._make_user_entry() + "\n" + self._make_assistant_entry(usage) + "\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result == {
            "input_tokens": 100,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        }

    def test_returns_last_assistant_not_first(self, tmp_path):
        """Should pick the last assistant entry, not the first"""
        old_usage = {
            "input_tokens": 10,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
            "output_tokens": 5,
        }
        new_usage = {
            "input_tokens": 500,
            "cache_read_input_tokens": 80000,
            "cache_creation_input_tokens": 1000,
            "output_tokens": 300,
        }
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(
            self._make_assistant_entry(old_usage)
            + "\n"
            + self._make_user_entry()
            + "\n"
            + self._make_assistant_entry(new_usage)
            + "\n"
        )

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result["input_tokens"] == 500
        assert result["cache_read_input_tokens"] == 80000

    def test_skips_sidechain_entries(self, tmp_path):
        """Should skip entries where isSidechain is true"""
        main_usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        }
        sidechain_usage = {
            "input_tokens": 9999,
            "cache_read_input_tokens": 9999,
            "cache_creation_input_tokens": 9999,
            "output_tokens": 9999,
        }
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(
            self._make_assistant_entry(main_usage)
            + "\n"
            + self._make_assistant_entry(sidechain_usage, is_sidechain=True)
            + "\n"
        )

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result["input_tokens"] == 100
        assert result["input_tokens"] != 9999

    def test_skips_user_entries(self, tmp_path):
        """Should skip non-assistant entry types"""
        usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        }
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(self._make_assistant_entry(usage) + "\n" + self._make_user_entry() + "\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result is not None
        assert result["input_tokens"] == 100

    def test_skips_assistant_without_usage(self, tmp_path):
        """Should skip assistant entries that have no usage field"""
        good_usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        }
        no_usage_entry = json.dumps(
            {
                "type": "assistant",
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "thinking..."}],
                },
            }
        )
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(self._make_assistant_entry(good_usage) + "\n" + no_usage_entry + "\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result is not None
        assert result["input_tokens"] == 100

    def test_returns_none_for_missing_file(self):
        """Should return None when transcript file doesn't exist"""
        fake_path = Path("/nonexistent/path/sess.jsonl")
        with patch("transcript_reader.get_transcript_path", return_value=fake_path):
            result = read_context_usage("/fake/dir", "sess")

        assert result is None

    def test_returns_none_for_empty_file(self, tmp_path):
        """Should return None when transcript file is empty"""
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text("")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result is None

    def test_returns_none_for_no_assistant_entries(self, tmp_path):
        """Should return None when transcript has no assistant entries"""
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(self._make_user_entry() + "\n" + self._make_user_entry() + "\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result is None

    def test_handles_invalid_json_lines(self, tmp_path):
        """Should skip invalid JSON lines gracefully"""
        usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        }
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(self._make_assistant_entry(usage) + "\n" + "not valid json\n" + "{broken json\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result is not None
        assert result["input_tokens"] == 100

    def test_handles_blank_lines(self, tmp_path):
        """Should skip blank lines in transcript"""
        usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        }
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(self._make_assistant_entry(usage) + "\n" + "\n" + "  \n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result is not None

    def test_defaults_missing_usage_fields_to_zero(self, tmp_path):
        """Should default missing token fields to 0"""
        # Minimal usage with only input_tokens
        usage = {"input_tokens": 100}
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(self._make_assistant_entry(usage) + "\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result["input_tokens"] == 100
        assert result["cache_read_input_tokens"] == 0
        assert result["cache_creation_input_tokens"] == 0
        assert result["output_tokens"] == 0

    def test_only_sidechain_entries_returns_none(self, tmp_path):
        """Should return None when all assistant entries are sidechains"""
        usage = {
            "input_tokens": 9999,
            "cache_read_input_tokens": 9999,
            "cache_creation_input_tokens": 9999,
            "output_tokens": 9999,
        }
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(
            self._make_assistant_entry(usage, is_sidechain=True)
            + "\n"
            + self._make_assistant_entry(usage, is_sidechain=True)
            + "\n"
        )

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_context_usage("/fake/dir", "sess")

        assert result is None


class TestGetProjectsDir:
    """Tests for get_projects_dir"""

    def test_returns_path(self):
        result = get_projects_dir("/home/user/project")
        assert result == Path.home() / ".claude/projects/-home-user-project"

    def test_consistent_with_get_transcript_path(self):
        projects_dir = get_projects_dir("/home/user/project")
        transcript = get_transcript_path("/home/user/project", "sess-1")
        assert transcript.parent == projects_dir


class TestFindLatestSession:
    """Tests for find_latest_session"""

    def test_returns_none_for_missing_dir(self):
        with patch("transcript_reader.get_projects_dir", return_value=Path("/nonexistent/dir")):
            result = find_latest_session("/fake")
        assert result is None

    def test_returns_none_for_empty_dir(self, tmp_path):
        with patch("transcript_reader.get_projects_dir", return_value=tmp_path):
            result = find_latest_session("/fake")
        assert result is None

    def test_returns_latest_by_mtime(self, tmp_path):
        # Create older file
        old = tmp_path / "old-session.jsonl"
        old.write_text('{"type": "user"}\n')

        # Small delay to ensure different mtime
        time.sleep(0.05)

        # Create newer file
        new = tmp_path / "new-session.jsonl"
        new.write_text('{"type": "user"}\n')

        with patch("transcript_reader.get_projects_dir", return_value=tmp_path):
            result = find_latest_session("/fake")

        assert result == "new-session"

    def test_returns_stem_without_extension(self, tmp_path):
        (tmp_path / "abc-123-456.jsonl").write_text("")

        with patch("transcript_reader.get_projects_dir", return_value=tmp_path):
            result = find_latest_session("/fake")

        assert result == "abc-123-456"


class TestGetJsonlLineCount:
    """Tests for get_jsonl_line_count"""

    def test_returns_zero_for_missing_file(self):
        fake_path = Path("/nonexistent/sess.jsonl")
        with patch("transcript_reader.get_transcript_path", return_value=fake_path):
            result = get_jsonl_line_count("/fake", "sess")
        assert result == 0

    def test_counts_lines(self, tmp_path):
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text("line1\nline2\nline3\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = get_jsonl_line_count("/fake", "sess")

        assert result == 3

    def test_empty_file(self, tmp_path):
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text("")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = get_jsonl_line_count("/fake", "sess")

        assert result == 0


class TestReadNewEntries:
    """Tests for read_new_entries"""

    def test_reads_all_from_start(self, tmp_path):
        transcript = tmp_path / "sess.jsonl"
        entries = [
            json.dumps({"type": "user", "message": "hello"}),
            json.dumps({"type": "assistant", "message": "world"}),
        ]
        transcript.write_text("\n".join(entries) + "\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_new_entries("/fake", "sess", 0)

        assert len(result) == 2
        assert result[0]["type"] == "user"
        assert result[1]["type"] == "assistant"

    def test_reads_from_offset(self, tmp_path):
        transcript = tmp_path / "sess.jsonl"
        entries = [
            json.dumps({"type": "user", "n": 1}),
            json.dumps({"type": "user", "n": 2}),
            json.dumps({"type": "assistant", "n": 3}),
        ]
        transcript.write_text("\n".join(entries) + "\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_new_entries("/fake", "sess", 2)

        assert len(result) == 1
        assert result[0]["n"] == 3

    def test_returns_empty_past_end(self, tmp_path):
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(json.dumps({"type": "user"}) + "\n")

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_new_entries("/fake", "sess", 10)

        assert result == []

    def test_skips_blank_and_invalid_lines(self, tmp_path):
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text(
            json.dumps({"type": "user"}) + "\n" + "\n" + "invalid json\n" + json.dumps({"type": "assistant"}) + "\n"
        )

        with patch("transcript_reader.get_transcript_path", return_value=transcript):
            result = read_new_entries("/fake", "sess", 0)

        assert len(result) == 2

    def test_returns_empty_for_missing_file(self):
        fake_path = Path("/nonexistent/sess.jsonl")
        with patch("transcript_reader.get_transcript_path", return_value=fake_path):
            result = read_new_entries("/fake", "sess", 0)
        assert result == []
