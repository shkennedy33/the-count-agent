"""Round-trip tests for TrainingLog: JSONL writes, markdown render, comments."""
import json
import tempfile
from pathlib import Path

import pytest

from training_log import TrainingLog


@pytest.fixture
def tmp_session(tmp_path):
    log = TrainingLog(
        base_dir=tmp_path,
        discord_user_id="123",
        session_uuid="abc-uuid",
        user_display_name="TestFriend",
        model="claude-opus-4-7",
        system_prompt_hash="sha256:fake",
    )
    log.open()
    return log


def test_open_creates_dir_and_metadata(tmp_session, tmp_path):
    expected_dir = tmp_path / "123" / "abc-uuid"
    assert expected_dir.exists()
    assert (expected_dir / "metadata.json").exists()
    meta = json.loads((expected_dir / "metadata.json").read_text())
    assert meta["discord_user_id"] == "123"
    assert meta["discord_username"] == "TestFriend"
    assert meta["session_uuid"] == "abc-uuid"
    assert meta["model"] == "claude-opus-4-7"
    assert "started_at" in meta
    assert meta.get("ended_at") is None


def test_append_user_and_assistant_turns_writes_jsonl(tmp_session):
    idx0 = tmp_session.append_user_turn("hello count")
    idx1 = tmp_session.append_assistant_turn(
        "Hello — what brings you?",
        model="claude-opus-4-7",
        tool_calls=[],
    )
    assert idx0 == 0
    assert idx1 == 1

    jsonl = (tmp_session.log_dir / "conversation.jsonl").read_text().strip().splitlines()
    assert len(jsonl) == 2
    turn0 = json.loads(jsonl[0])
    turn1 = json.loads(jsonl[1])
    assert turn0 == {"turn": 0, "role": "user", "content": "hello count", "ts": turn0["ts"]}
    assert turn1["turn"] == 1
    assert turn1["role"] == "assistant"
    assert turn1["content"] == "Hello — what brings you?"
    assert turn1["model"] == "claude-opus-4-7"
    assert turn1["tool_calls"] == []


def test_render_markdown_matches_jsonl(tmp_session):
    tmp_session.append_user_turn("ask about Saint Germain")
    tmp_session.append_assistant_turn(
        "Saint Germain — which thread?",
        model="claude-opus-4-7",
        tool_calls=[{"name": "Read", "args": {"file_path": "vault/X.md"}, "result_preview": "..."}],
    )
    tmp_session.render_markdown()

    md = (tmp_session.log_dir / "conversation.md").read_text()
    assert "TestFriend" in md
    assert "ask about Saint Germain" in md
    assert "Saint Germain — which thread?" in md
    assert "tool: Read" in md


def test_comment_attached_to_turn(tmp_session):
    tmp_session.append_user_turn("hi")
    tmp_session.append_assistant_turn("hello", model="claude-opus-4-7", tool_calls=[])
    tmp_session.append_comment_attached(turn_index=1, text="that was a flat opener")

    comments = (tmp_session.log_dir / "comments.md").read_text()
    assert "Turn 1" in comments
    assert "that was a flat opener" in comments


def test_global_comment_lands_in_separate_section(tmp_session):
    tmp_session.append_user_turn("hi")
    tmp_session.append_assistant_turn("hello", model="claude-opus-4-7", tool_calls=[])
    tmp_session.append_comment_global("his energy was muted today")

    comments = (tmp_session.log_dir / "comments.md").read_text()
    assert "Global comments" in comments
    assert "his energy was muted today" in comments


def test_close_writes_ended_at_and_index(tmp_session, tmp_path):
    tmp_session.append_user_turn("hi")
    tmp_session.append_assistant_turn("hello", model="claude-opus-4-7", tool_calls=[])
    tmp_session.close(total_cost=0.0042)

    meta = json.loads((tmp_session.log_dir / "metadata.json").read_text())
    assert meta["ended_at"] is not None
    assert meta["total_cost"] == 0.0042
    assert meta["turn_count"] == 2

    index_path = tmp_path / "INDEX.jsonl"
    assert index_path.exists()
    entry = json.loads(index_path.read_text().strip().splitlines()[-1])
    assert entry["session_uuid"] == "abc-uuid"
    assert entry["total_cost"] == 0.0042
    assert entry["turn_count"] == 2


def test_jsonl_not_polluted_by_comments(tmp_session):
    """Comments must never appear in the JSONL training data."""
    tmp_session.append_user_turn("hi")
    tmp_session.append_assistant_turn("hello", model="claude-opus-4-7", tool_calls=[])
    tmp_session.append_comment_attached(turn_index=1, text="bad fact")
    tmp_session.append_comment_global("his energy")

    jsonl_text = (tmp_session.log_dir / "conversation.jsonl").read_text()
    assert "bad fact" not in jsonl_text
    assert "his energy" not in jsonl_text
