"""Per-conversation on-disk training log: JSONL canonical + markdown render.

Layout per session:
    base_dir / <discord_user_id> / <session_uuid> /
        conversation.jsonl   — one JSON object per turn (canonical)
        conversation.md      — human-readable, regenerated each turn
        comments.md          — turn-attached + global feedback (markdown only)
        metadata.json        — session-level metadata

INDEX.jsonl at base_dir gets one line appended per session on close().
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write content to path via write-then-rename. Safe across crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class TrainingLog:
    def __init__(
        self,
        base_dir: Path,
        discord_user_id: str,
        session_uuid: str,
        user_display_name: str,
        model: str,
        system_prompt_hash: str,
    ):
        self.base_dir = Path(base_dir)
        self.discord_user_id = discord_user_id
        self.session_uuid = session_uuid
        self.user_display_name = user_display_name
        self.model = model
        self.system_prompt_hash = system_prompt_hash
        self.log_dir = self.base_dir / discord_user_id / session_uuid
        self.turn_count = 0
        self.started_at: str | None = None
        self.ended_at: str | None = None

    def open(self) -> None:
        """Create directory and write initial metadata.json."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = _now_iso()
        meta = {
            "discord_user_id": self.discord_user_id,
            "discord_username": self.user_display_name,
            "session_uuid": self.session_uuid,
            "model": self.model,
            "system_prompt_hash": self.system_prompt_hash,
            "started_at": self.started_at,
            "ended_at": None,
            "turn_count": 0,
            "total_cost": 0.0,
        }
        _atomic_write_text(self.log_dir / "metadata.json", json.dumps(meta, indent=2))
        # Touch conversation.jsonl so it exists even before the first turn.
        (self.log_dir / "conversation.jsonl").touch(exist_ok=True)

    def _append_jsonl(self, record: dict) -> None:
        path = self.log_dir / "conversation.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_user_turn(self, content: str) -> int:
        idx = self.turn_count
        self._append_jsonl({
            "turn": idx,
            "role": "user",
            "content": content,
            "ts": _now_iso(),
        })
        self.turn_count += 1
        return idx

    def append_assistant_turn(self, content: str, model: str, tool_calls: list[dict]) -> int:
        idx = self.turn_count
        self._append_jsonl({
            "turn": idx,
            "role": "assistant",
            "content": content,
            "ts": _now_iso(),
            "model": model,
            "tool_calls": tool_calls,
        })
        self.turn_count += 1
        return idx

    def append_comment_attached(self, turn_index: int, text: str) -> None:
        """Append a feedback comment bound to a specific turn. Markdown only."""
        path = self.log_dir / "comments.md"
        block = f"\n### Turn {turn_index}\n> [{_now_iso()}] {text}\n"

        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if "## Turn-attached comments" not in existing:
            existing = "## Turn-attached comments\n" + existing + "\n## Global comments\n"
        # Insert the block at the end of the turn-attached section.
        sections = existing.split("## Global comments", 1)
        head = sections[0].rstrip() + block + "\n"
        tail = "## Global comments" + (sections[1] if len(sections) > 1 else "\n")
        path.write_text(head + tail, encoding="utf-8")

    def append_comment_global(self, text: str) -> None:
        """Append a free-floating session-level comment. Markdown only."""
        path = self.log_dir / "comments.md"
        block = f"> [{_now_iso()}] {text}\n"

        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if "## Global comments" not in existing:
            existing = (
                ("## Turn-attached comments\n" if "## Turn-attached comments" not in existing else existing)
                + "\n## Global comments\n"
            )
        path.write_text(existing.rstrip() + "\n" + block, encoding="utf-8")

    def render_markdown(self) -> None:
        """Re-render conversation.md from conversation.jsonl. Atomic write."""
        jsonl_path = self.log_dir / "conversation.jsonl"
        if not jsonl_path.exists():
            return
        lines = []
        lines.append(f"# Conversation with {self.user_display_name}")
        lines.append(f"_session: {self.session_uuid} · started {self.started_at}_\n")

        for raw in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            turn = json.loads(raw)
            ts = turn.get("ts", "")
            if turn["role"] == "user":
                lines.append(f"## {self.user_display_name} ({ts})")
                lines.append(turn["content"])
            else:
                model = turn.get("model", "")
                lines.append(f"## Count ({ts}, {model})")
                lines.append(turn["content"])
                for tc in turn.get("tool_calls") or []:
                    args_preview = json.dumps(tc.get("args", {}), ensure_ascii=False)[:200]
                    lines.append(f"\n> tool: {tc.get('name')} {args_preview}")
            lines.append("")  # blank line between turns

        _atomic_write_text(self.log_dir / "conversation.md", "\n".join(lines))

    def close(self, total_cost: float) -> None:
        """Finalize: write ended_at + total_cost into metadata, append INDEX.jsonl."""
        self.ended_at = _now_iso()
        meta_path = self.log_dir / "metadata.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        meta["ended_at"] = self.ended_at
        meta["turn_count"] = self.turn_count
        meta["total_cost"] = total_cost
        _atomic_write_text(meta_path, json.dumps(meta, indent=2))

        index_entry = {
            "discord_user_id": self.discord_user_id,
            "session_uuid": self.session_uuid,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turn_count": self.turn_count,
            "total_cost": total_cost,
            "system_prompt_hash": self.system_prompt_hash,
        }
        index_path = self.base_dir / "INDEX.jsonl"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(index_entry, ensure_ascii=False) + "\n")
