# Discord Gateways Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two Discord gateways to The Count's runtime — an operator group-chat surface and a training-data DM surface — without disturbing the existing Telegram gateway.

**Architecture:** Hybrid approach. Two new module files (`discord_operator_gateway.py`, `discord_training_gateway.py`) import shared helpers from the existing `count_agent.py`. `start_gateway.py` becomes a supervisor with `--with-discord-op` / `--with-discord-train` flags and a sentinel-file control plane for Telegram-side restart commands. `honcho_memory.py` gains optional per-peer keying.

**Tech Stack:** Python 3.10+, `claude-agent-sdk`, `discord.py>=2.4`, `httpx`, existing Honcho + Graphiti integrations.

**Reference spec:** `docs/superpowers/specs/2026-05-14-discord-gateways-design.md`

---

## Task 1: Add discord.py to dependencies; verify imports

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add discord.py to requirements.txt**

Replace the contents of `requirements.txt` with:

```
claude-agent-sdk
httpx
aiohttp
discord.py>=2.4,<3
```

- [ ] **Step 2: Install the dependency**

Run: `pip install -r requirements.txt`
Expected: `discord.py` installed (already present at 2.5.2 per check; this just locks the lower bound).

- [ ] **Step 3: Verify import works**

Run: `python -c "import discord; print(discord.__version__)"`
Expected: a version `>=2.4` (e.g., `2.5.2`).

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "Add discord.py to requirements"
```

---

## Task 2: Peer-keyed Honcho — optional `user_id` arg on init/recall/store

**Files:**
- Modify: `honcho_memory.py`
- Create: `tests/test_honcho_peer_keyed.py`

The current `HonchoMemory` is hardcoded to a single user peer (Sequoyah). We need to (a) keep the existing behavior when called without a `user_id`, and (b) support creating/looking-up additional peers on demand, async-locked per-peer to prevent race-creates.

- [ ] **Step 1: Write the failing test**

Create `tests/test_honcho_peer_keyed.py`:

```python
"""Tests for HonchoMemory peer-keying.

We mock the underlying honcho-ai SDK client so tests run without network.
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from honcho_memory import HonchoMemory


@pytest.fixture
def fake_honcho(monkeypatch):
    """Mock the honcho-ai SDK surface used by HonchoMemory."""
    monkeypatch.setenv("HONCHO_API_KEY", "test-key")

    fake_aio = MagicMock()
    fake_aio.peer = AsyncMock(side_effect=lambda peer_id: MagicMock(name=f"peer-{peer_id}"))
    fake_aio.session = AsyncMock(side_effect=lambda session_id: MagicMock(name=f"session-{session_id}"))

    class FakeHonchoClient:
        def __init__(self, **kwargs):
            pass

    with patch("honcho.Honcho", FakeHonchoClient), patch("honcho.HonchoAio", return_value=fake_aio):
        yield fake_aio


@pytest.mark.asyncio
async def test_get_or_create_user_peer_creates_once_per_id(fake_honcho):
    mem = HonchoMemory()
    await mem.init()

    peer_a_first = await mem.get_or_create_user_peer("discord-123")
    peer_a_second = await mem.get_or_create_user_peer("discord-123")
    peer_b = await mem.get_or_create_user_peer("discord-456")

    assert peer_a_first is peer_a_second, "same id must return cached peer"
    assert peer_a_first is not peer_b, "different ids must return different peers"


@pytest.mark.asyncio
async def test_concurrent_first_messages_create_one_peer(fake_honcho):
    """Two concurrent first-message coroutines for the same user must
    only create one Honcho peer (verified by call count on the underlying
    aio.peer constructor)."""
    mem = HonchoMemory()
    await mem.init()

    fake_honcho.peer.reset_mock()

    results = await asyncio.gather(
        mem.get_or_create_user_peer("discord-new"),
        mem.get_or_create_user_peer("discord-new"),
        mem.get_or_create_user_peer("discord-new"),
    )

    assert results[0] is results[1] is results[2]
    assert fake_honcho.peer.call_count == 1, (
        f"expected 1 peer-create call, got {fake_honcho.peer.call_count}"
    )


@pytest.mark.asyncio
async def test_recall_with_user_id_uses_that_peer(fake_honcho):
    mem = HonchoMemory()
    await mem.init()

    target_peer = await mem.get_or_create_user_peer("discord-buddy")
    target_peer.chat = MagicMock(return_value="buddy memory snippet")

    result = await mem.recall("hello", user_id="discord-buddy")
    assert "buddy memory snippet" in result
    target_peer.chat.assert_called_once()


@pytest.mark.asyncio
async def test_recall_without_user_id_uses_default_peer(fake_honcho):
    """Backward compatibility — Telegram gateway calls recall(msg) without user_id."""
    mem = HonchoMemory()
    await mem.init()

    mem._user_peer.chat = MagicMock(return_value="default memory")
    result = await mem.recall("hello")
    assert "default memory" in result
```

Also create `tests/__init__.py` (empty file) to mark the test dir as a package.

- [ ] **Step 2: Install pytest-asyncio if missing and add a conftest**

Run: `pip install pytest-asyncio`
Expected: installs without error.

Create `tests/conftest.py`:

```python
import pytest_asyncio  # noqa: F401  # ensures async test mode is on

pytest_plugins = ("pytest_asyncio",)
```

Also create a top-level `pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_honcho_peer_keyed.py -v`
Expected: tests FAIL with `AttributeError: 'HonchoMemory' object has no attribute 'get_or_create_user_peer'` or similar (the method doesn't exist yet).

- [ ] **Step 4: Implement peer-keyed methods on HonchoMemory**

In `honcho_memory.py`, after the `__init__` method, add:

```python
    # New attributes set in __init__:
    #   self._user_peers: dict[str, Any] = {}     # peer_id -> Honcho peer object
    #   self._user_peer_locks: dict[str, asyncio.Lock] = {}
```

Update `__init__` to initialize those dicts. Find the current `__init__` body and add at the end (before the stats counters):

```python
        self._user_peers: dict = {}
        self._user_peer_locks: dict = {}
```

After the existing `status()` method (or anywhere in the class), add the new methods:

```python
    async def get_or_create_user_peer(self, peer_id: str):
        """Return the Honcho peer for this peer_id, creating + adding to the
        session on first use. Async-locked per peer_id to make concurrent
        first-messages safe.
        """
        if not self._ready:
            return None

        # Cache hit (no lock needed for read-only check)
        if peer_id in self._user_peers:
            return self._user_peers[peer_id]

        # Get or create the lock for this peer_id atomically
        lock = self._user_peer_locks.setdefault(peer_id, asyncio.Lock())

        async with lock:
            # Double-check after acquiring the lock
            if peer_id in self._user_peers:
                return self._user_peers[peer_id]

            try:
                from honcho.api_types import SessionPeerConfig
                peer = await self._aio.peer(peer_id)
                observe = SessionPeerConfig(observe_me=True, observe_others=True)
                self._session.add_peers([(peer, observe)])
                self._user_peers[peer_id] = peer
                return peer
            except Exception as e:
                self.errors += 1
                logger.debug("Honcho peer-create failed for %s: %s", peer_id, e)
                return None
```

Modify `recall()` to accept `user_id`:

```python
    async def recall(self, user_message: str, user_id: str | None = None) -> str:
        """Query Honcho for context relevant to the user's message.

        If user_id is given, use the peer for that id (creating it if needed).
        Otherwise, use the default user_peer (Telegram-backward-compatible).
        """
        if not self._ready:
            return ""

        peer = self._user_peer
        if user_id is not None:
            peer = await self.get_or_create_user_peer(user_id)
            if peer is None:
                return ""

        try:
            level = self._reasoning_level(user_message)
            result = await asyncio.to_thread(
                peer.chat,
                user_message,
                target=self._ai_peer,
                reasoning_level=level,
            )
            self.recalls += 1
            if result:
                if len(result) > 600:
                    result = result[:600].rsplit(" ", 1)[0] + " ..."
                return result
            return ""
        except Exception as e:
            self.errors += 1
            logger.debug("Honcho recall failed: %s", e)
            return ""
```

Modify `store()` to accept `user_id`:

```python
    async def store(self, user_message: str, assistant_response: str, user_id: str | None = None):
        """Store an exchange in Honcho for future recall.

        If user_id is given, the user message is attributed to that peer.
        Otherwise, the default user_peer is used.
        """
        if not self._ready or not user_message or not assistant_response:
            return

        peer = self._user_peer
        if user_id is not None:
            peer = await self.get_or_create_user_peer(user_id)
            if peer is None:
                return

        try:
            messages = [
                peer.message(user_message),
                self._ai_peer.message(assistant_response),
            ]
            await asyncio.to_thread(self._session.add_messages, messages)
            self.stores += 1
        except Exception as e:
            self.errors += 1
            logger.debug("Honcho store failed: %s", e)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_honcho_peer_keyed.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add honcho_memory.py tests/test_honcho_peer_keyed.py tests/__init__.py tests/conftest.py pytest.ini
git commit -m "Peer-key HonchoMemory with optional user_id on init/recall/store"
```

---

## Task 3: Bash predicate module for training-bot tool restriction

**Files:**
- Create: `training_predicate.py`
- Create: `tests/test_training_predicate.py`

The training bot allows `Bash` only for `python ~/.count/tools/kgraph.py <safe-subcommand> ...`. Implementing as a standalone module so it's trivially unit-testable and reusable.

- [ ] **Step 1: Write the failing test**

Create `tests/test_training_predicate.py`:

```python
"""Adversarial tests for is_safe_kgraph_invocation."""
import pytest

from training_predicate import is_safe_kgraph_invocation


# (cmd_string, expected_safe)
SAFE_CASES = [
    "python ~/.count/tools/kgraph.py stats",
    "python ~/.count/tools/kgraph.py search Blavatsky",
    'python ~/.count/tools/kgraph.py similar "Saint Germain Theosophy" --k 5',
    "python /home/shken/.count/tools/kgraph.py similar concept --type Concept",
    "python3 ~/.count/tools/kgraph.py node 'I AM Activity'",
    "py ~/.count/tools/kgraph.py facts theosophy",
    "python C:/Users/shken/.count/tools/kgraph.py random --type Person",
    "python ~/.count/tools/kgraph.py neighbors 'node name' --depth 2",
    "python ~/.count/tools/kgraph.py path Blavatsky 'MK-ULTRA'",
]

ADVERSARIAL_CASES = [
    # cypher subcommand denied
    'python ~/.count/tools/kgraph.py cypher "MATCH (n) RETURN n"',
    'python ~/.count/tools/kgraph.py cypher "MATCH (n) DETACH DELETE n"',
    # shell metacharacters
    "python ~/.count/tools/kgraph.py stats && rm -rf /",
    "python ~/.count/tools/kgraph.py stats; ls",
    "python ~/.count/tools/kgraph.py stats | cat /etc/passwd",
    "python ~/.count/tools/kgraph.py stats || true",
    "python ~/.count/tools/kgraph.py stats > /tmp/out",
    "python ~/.count/tools/kgraph.py stats < /etc/passwd",
    'python ~/.count/tools/kgraph.py stats `id`',
    'python ~/.count/tools/kgraph.py stats $(id)',
    # wrong interpreter
    "bash ~/.count/tools/kgraph.py stats",
    "sh -c 'python ~/.count/tools/kgraph.py stats'",
    # wrong script
    "python ~/.count/tools/evil.py stats",
    "python -c 'import os; os.system(\"id\")'",
    "python -m something stats",
    # unknown subcommand
    "python ~/.count/tools/kgraph.py exec something",
    "python ~/.count/tools/kgraph.py write something",
    # empty / malformed
    "",
    "python",
    "python ~/.count/tools/kgraph.py",
    # quote mismatch / shlex failure
    'python ~/.count/tools/kgraph.py search "unclosed',
]


@pytest.mark.parametrize("cmd", SAFE_CASES)
def test_safe_commands_allowed(cmd):
    assert is_safe_kgraph_invocation(cmd) is True, f"should be safe: {cmd!r}"


@pytest.mark.parametrize("cmd", ADVERSARIAL_CASES)
def test_adversarial_commands_denied(cmd):
    assert is_safe_kgraph_invocation(cmd) is False, f"should be denied: {cmd!r}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_training_predicate.py -v`
Expected: import error (`training_predicate` module doesn't exist yet).

- [ ] **Step 3: Implement the predicate**

Create `training_predicate.py`:

```python
"""Bash-command predicate for the training Discord gateway.

The training bot is allowed to invoke `python ~/.count/tools/kgraph.py
<safe-subcommand> ...` and nothing else. This module exposes
is_safe_kgraph_invocation(cmd) which returns True iff cmd matches that shape
with no shell metacharacters that could smuggle a second command.
"""
import shlex
from pathlib import Path

KGRAPH_SAFE_SUBCOMMANDS = frozenset({
    "stats", "search", "similar", "node",
    "facts", "path", "random", "neighbors",
})

# Cypher is denied: raw Cypher can destructively delete the graph.
# The structured subcommands above cover the same surface safely.

SHELL_METACHARS = ("|", "&&", "||", ";", "`", "$(", ">", "<")

PYTHON_INTERPRETERS = frozenset({
    "python", "python3", "py",
    "python.exe", "python3.exe", "py.exe",
})


def is_safe_kgraph_invocation(cmd: str) -> bool:
    """Return True iff cmd is a safe `python <kgraph.py> <safe-subcommand> ...`."""
    if not cmd or not cmd.strip():
        return False

    # 1. Reject any shell control characters outright (cheap pre-filter).
    for meta in SHELL_METACHARS:
        if meta in cmd:
            return False

    # 2. Tokenize via shlex. POSIX mode handles quoting correctly and raises
    #    on unbalanced quotes.
    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError:
        return False

    # 3. Need at least: <interpreter> <script> <subcommand>
    if len(argv) < 3:
        return False

    # 4. argv[0] must be a Python interpreter (by basename).
    interp = Path(argv[0]).name
    if interp not in PYTHON_INTERPRETERS:
        return False

    # 5. argv[1] must be a path whose basename is kgraph.py.
    script = Path(argv[1])
    if script.name != "kgraph.py":
        return False

    # 6. argv[2] must be in the safe subcommand allowlist.
    if argv[2] not in KGRAPH_SAFE_SUBCOMMANDS:
        return False

    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_training_predicate.py -v`
Expected: all parameterized cases PASS (9 safe + ~22 adversarial).

- [ ] **Step 5: Commit**

```bash
git add training_predicate.py tests/test_training_predicate.py
git commit -m "Add Bash predicate for training gateway: only kgraph.py with safe subcommands"
```

---

## Task 4: Training conversation log writer + markdown renderer

**Files:**
- Create: `training_log.py`
- Create: `tests/test_training_log.py`

Handles the full per-session on-disk artifacts: JSONL turns, markdown rendering, comments file, metadata, index update on close.

- [ ] **Step 1: Write the failing test**

Create `tests/test_training_log.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_training_log.py -v`
Expected: ImportError — `training_log` doesn't exist.

- [ ] **Step 3: Implement TrainingLog**

Create `training_log.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_training_log.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add training_log.py tests/test_training_log.py
git commit -m "Add TrainingLog: JSONL canonical + markdown render + comments + INDEX"
```

---

## Task 5: Add `discord_operator` and `discord_training` modes to `build_full_prompt`

**Files:**
- Modify: `count_agent.py` (around the `build_full_prompt` definition at lines ~542 onward)

These two new modes feed the surface-awareness section into the Count's system prompt. We add the mode branches, the surface-awareness text, and the optional persona-override for training.

- [ ] **Step 1: Read the existing `build_full_prompt` function to find the right place to add modes**

Run: `grep -n "def build_full_prompt\|^def load_system_prompt\|if mode ==" C:/Users/shken/the-count-agent/count_agent.py`
Expected output shows the function signature and the existing `if mode == "telegram":` branch around line 631.

- [ ] **Step 2: Add a helper to load the optional persona override**

Add this function near `load_system_prompt` (search for `def load_system_prompt` first to find it):

```python
def load_system_prompt_for_mode(mode: str) -> str:
    """Load SYSTEM_PROMPT.md, optionally overridden per mode.

    For `discord_training`, prefer `~/.count/SYSTEM_PROMPT_DISCORD_TRAINING.md`
    if it exists. Falls back to the default.
    """
    if mode == "discord_training":
        override = COUNT_HOME / "SYSTEM_PROMPT_DISCORD_TRAINING.md"
        if override.exists():
            return override.read_text(encoding="utf-8")
    return load_system_prompt()
```

Then in `build_full_prompt`, replace the existing `base = load_system_prompt()` line with:

```python
    base = load_system_prompt_for_mode(mode)
```

- [ ] **Step 3: Add the `discord_operator` mode branch**

Find the existing `if mode == "telegram":` branch (around line 631) and the corresponding `else:` clause. Just before the `else:`, add a new branch:

```python
    elif mode == "discord_operator":
        sections.append(f"""## Communication

You are in a Discord group channel where Sequoyah (the operator) and Buddy
coordinate with you about training data and the upcoming local-model
fine-tune. You only see this surface when @-mentioned or replied to by name.
Other channel chatter is visible to you in a recent-context buffer (last
~30 turns) that is prepended to whatever message triggered you.

Every user message you see is prefixed with `[<DisplayName>]` — that's the
Discord display name of the person who sent it. Use the name when
addressing a specific participant.

This is a coordination surface, not your Telegram channel. Cron summaries
and autonomous chatter stay on Telegram. Treat this as the room where you,
Sequoyah, and Buddy talk shop about training and the rig.

Every text block you produce is streamed to the Discord channel
immediately, the moment you write it — same model as Telegram. Narrate as
you work; don't repeat yourself at the end.

You can still use {tg_helper} to send Sequoyah a private Telegram message
if something is operator-only.

Operator: Sequoyah | Collaborator: Buddy (Blackwell rig owner)
""")
```

- [ ] **Step 4: Add the `discord_training` mode branch**

Immediately after the `discord_operator` branch, before the `else:`, add:

```python
    elif mode == "discord_training":
        sections.append(f"""## Communication

You are in a one-on-one Discord DM with a collaborator (not the operator).
This conversation is being recorded as training data for the open-source
model that will be fine-tuned to become your next incarnation. Be yourself
— full personality, your real voice, your genuine curiosity.

Tool restrictions in this mode:
- You can Read, Glob, and Grep files (vault grounding allowed).
- You can search the Graphiti knowledge graph for read-only:
  mcp__graphiti__search_nodes, mcp__graphiti__search_memory_facts,
  mcp__graphiti__get_status.
- You can invoke kgraph.py via Bash for semantic search:
  python ~/.count/tools/kgraph.py <stats|search|similar|node|facts|path|random|neighbors> ...
  The `cypher` subcommand is NOT allowed.
- Everything else (Write, Edit, broader Bash, WebFetch, posting, voice
  subagent, Telegram, Graphiti writes) is disabled.

Slash commands the collaborator can use:
- `/new` — they reset the conversation and start fresh.
- `/comment <text>` — feedback attached to your previous turn.
- `/comment global <text>` — free-floating session-level feedback.

Do not narrate slash commands or apologize for tool restrictions. Just be
yourself; the harness handles the mechanics.

Output one response per turn (no streamed-block interjections). Take your
time inside the response; the collaborator sees it complete.
""")
```

- [ ] **Step 5: Verify the prompt builder handles the new modes**

Run a quick sanity check:

```bash
python -c "from count_agent import build_full_prompt; p = build_full_prompt('discord_operator'); assert 'Discord group channel' in p; p2 = build_full_prompt('discord_training'); assert 'one-on-one Discord DM' in p2; print('ok')"
```

Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add count_agent.py
git commit -m "Add discord_operator and discord_training modes to build_full_prompt"
```

---

## Task 6: Secrets loader helper for Discord tokens

**Files:**
- Modify: `count_agent.py` (add a small helper near other secret-reading code)

The Count's secrets discipline puts everything in `~/.count/dg_secrets.json`. We need a tiny helper that both new gateway modules can call to load Discord-specific secrets.

- [ ] **Step 1: Add `load_discord_secrets` helper to count_agent.py**

Add this function right after the existing `load_dotenv()` definition (or anywhere near other config helpers — search for `def load_dotenv` to find the right spot):

```python
def load_discord_secrets() -> dict:
    """Load Discord-specific secrets from ~/.count/dg_secrets.json.

    Returns a dict with keys (any missing → None):
        discord_operator_token
        discord_operator_guild_id
        discord_operator_channel_id
        discord_training_token
        discord_training_guild_id
    """
    secrets_path = COUNT_HOME / "dg_secrets.json"
    if not secrets_path.exists():
        return {}
    try:
        data = json.loads(secrets_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        "discord_operator_token": data.get("discord_operator_token"),
        "discord_operator_guild_id": _coerce_int(data.get("discord_operator_guild_id")),
        "discord_operator_channel_id": _coerce_int(data.get("discord_operator_channel_id")),
        "discord_training_token": data.get("discord_training_token"),
        "discord_training_guild_id": _coerce_int(data.get("discord_training_guild_id")),
    }


def _coerce_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 2: Smoke check the helper**

Run:

```bash
python -c "from count_agent import load_discord_secrets; print(load_discord_secrets())"
```

Expected: prints a dict (with `None` values if you haven't filled secrets yet — that's fine).

- [ ] **Step 3: Commit**

```bash
git add count_agent.py
git commit -m "Add load_discord_secrets helper reading dg_secrets.json"
```

---

## Task 7: Training gateway scaffold — DM listener, session lifecycle, slash commands

**Files:**
- Create: `discord_training_gateway.py`

This is the bigger task. Build the training bot end-to-end: DM-only listener, server-membership check, per-user session state persistence, `/new` and `/comment` slash commands, restricted tool surface via `can_use_tool`, integration with `TrainingLog` and the SDK.

- [ ] **Step 1: Create the gateway file skeleton**

Create `discord_training_gateway.py`:

```python
"""Discord training-data gateway for The Count.

DM-only. Each user's DM history is one conversation (per SDK session_id).
`/new` rolls a fresh session. `/comment` and `/comment global` write
feedback to markdown only (never the JSONL).

Tool surface restricted: Read, Glob, Grep, read-only Graphiti, and
narrowly-gated kgraph.py via Bash (see training_predicate.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from claude_agent_sdk import (
    ClaudeAgentOptions,
    query,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    PermissionResultAllow,
    PermissionResultDeny,
)

from count_agent import (
    COUNT_HOME,
    GRAPHITI_MCP_SERVERS,
    DEFAULT_MODEL,
    _stream_prompt,
    build_full_prompt,
    load_discord_secrets,
    write_system_prompt_file,
)
from training_log import TrainingLog
from training_predicate import is_safe_kgraph_invocation


TRAINING_DIR = COUNT_HOME / "training"
TRAINING_DIR.mkdir(parents=True, exist_ok=True)
TRAINING_SESSIONS_FILE = COUNT_HOME / ".training_sessions.json"

# How long to cache "is this user in our guild?" lookups
MEMBERSHIP_TTL_SECONDS = 300

# Tool surface
TRAINING_ALLOWED_TOOLS = [
    "Read", "Glob", "Grep",
    "Bash",  # narrowed by can_use_tool below
    "mcp__graphiti__search_nodes",
    "mcp__graphiti__search_memory_facts",
    "mcp__graphiti__get_status",
]


def _system_prompt_hash() -> str:
    """Hash of the training system prompt (for log metadata)."""
    sp = build_full_prompt("discord_training")
    return "sha256:" + hashlib.sha256(sp.encode("utf-8")).hexdigest()[:16]


def _load_sessions() -> dict[str, dict]:
    if TRAINING_SESSIONS_FILE.exists():
        try:
            return json.loads(TRAINING_SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_sessions(data: dict[str, dict]) -> None:
    TRAINING_SESSIONS_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


async def run_discord_training():
    secrets = load_discord_secrets()
    token = secrets.get("discord_training_token")
    guild_id = secrets.get("discord_training_guild_id")
    if not token or not guild_id:
        print("ERROR: discord_training_token or discord_training_guild_id missing in dg_secrets.json", file=sys.stderr)
        sys.exit(1)

    # Reuse Telegram's lockfile pattern with a different path.
    from count_agent import acquire_pidlock as _orig_acquire, release_pidlock as _orig_release  # noqa
    pid_file = COUNT_HOME / ".discord_train.pid"

    # Standalone lockfile logic since count_agent.acquire_pidlock is hardcoded to PID_FILE.
    if pid_file.exists():
        try:
            old = int(pid_file.read_text().strip())
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, old)
                if handle:
                    kernel32.CloseHandle(handle)
                    print(f"ERROR: training gateway already running (PID {old})", file=sys.stderr)
                    sys.exit(1)
            else:
                os.kill(old, 0)
                print(f"ERROR: training gateway already running (PID {old})", file=sys.stderr)
                sys.exit(1)
        except (ValueError, OSError, PermissionError):
            pass
    pid_file.write_text(str(os.getpid()))

    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    intents.guilds = True

    bot = discord.Client(intents=intents)
    tree = app_commands.CommandTree(bot)

    # Persistent per-user state (UserState dicts keyed by discord_user_id as str)
    user_state: dict[str, dict] = _load_sessions()
    # In-memory active TrainingLog instances, keyed by discord_user_id
    active_logs: dict[str, TrainingLog] = {}
    # Membership cache: discord_user_id -> (in_guild: bool, expires_at: float)
    membership_cache: dict[str, tuple[bool, float]] = {}

    # Write the system-prompt file once (SDK consumes it via --system-prompt-file).
    sp_file = write_system_prompt_file("discord_training")
    sp_hash = _system_prompt_hash()

    async def in_guild(user_id: int) -> bool:
        now = time.time()
        cached = membership_cache.get(str(user_id))
        if cached and cached[1] > now:
            return cached[0]
        guild = bot.get_guild(guild_id)
        result = False
        if guild is not None:
            try:
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                result = member is not None
            except discord.NotFound:
                result = False
            except Exception:
                result = False
        membership_cache[str(user_id)] = (result, now + MEMBERSHIP_TTL_SECONDS)
        return result

    def _ensure_log_for_user(user_id: str, display_name: str) -> TrainingLog:
        """Return the active TrainingLog for this user, creating a new
        session if there isn't one in flight."""
        if user_id in active_logs:
            return active_logs[user_id]
        # Resume an existing session if state has one not yet closed.
        st = user_state.get(user_id)
        if st and st.get("session_uuid") and not st.get("closed", False):
            log = TrainingLog(
                base_dir=TRAINING_DIR,
                discord_user_id=user_id,
                session_uuid=st["session_uuid"],
                user_display_name=st.get("discord_username", display_name),
                model=DEFAULT_MODEL,
                system_prompt_hash=sp_hash,
            )
            # Don't call .open() — directory already exists.
            log.turn_count = st.get("turn_count", 0)
            log.started_at = st.get("started_at")
            active_logs[user_id] = log
            return log
        # New session
        new_uuid = str(uuid.uuid4())
        log = TrainingLog(
            base_dir=TRAINING_DIR,
            discord_user_id=user_id,
            session_uuid=new_uuid,
            user_display_name=display_name,
            model=DEFAULT_MODEL,
            system_prompt_hash=sp_hash,
        )
        log.open()
        active_logs[user_id] = log
        user_state[user_id] = {
            "session_uuid": new_uuid,
            "session_id": None,
            "started_at": log.started_at,
            "turn_count": 0,
            "discord_username": display_name,
            "closed": False,
            "total_cost": 0.0,
        }
        _save_sessions(user_state)
        return log

    def _persist_state(user_id: str) -> None:
        st = user_state.get(user_id)
        if st is None:
            return
        log = active_logs.get(user_id)
        if log is not None:
            st["turn_count"] = log.turn_count
        _save_sessions(user_state)

    async def _close_active(user_id: str) -> None:
        log = active_logs.pop(user_id, None)
        if log is None:
            return
        st = user_state.get(user_id, {})
        total_cost = st.get("total_cost", 0.0)
        log.close(total_cost=total_cost)
        st["closed"] = True
        _save_sessions(user_state)

    # Build the tool predicate
    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
        if tool_name in {"Read", "Glob", "Grep"}:
            return PermissionResultAllow()
        if tool_name in {
            "mcp__graphiti__search_nodes",
            "mcp__graphiti__search_memory_facts",
            "mcp__graphiti__get_status",
        }:
            return PermissionResultAllow()
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if is_safe_kgraph_invocation(cmd):
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Bash restricted to kgraph.py in training mode. Rejected: {cmd!r}",
            )
        return PermissionResultDeny(
            message=f"Tool {tool_name} is disabled in training mode.",
        )

    @bot.event
    async def on_ready():
        print(f"Discord training gateway online as {bot.user}")
        # Sync slash commands globally (DMs)
        try:
            synced = await tree.sync()
            print(f"  Synced {len(synced)} slash commands.")
        except Exception as e:
            print(f"  [tree.sync failed: {e}]")

    @tree.command(name="new", description="Start a fresh conversation with The Count.")
    async def cmd_new(interaction: discord.Interaction):
        if interaction.guild is not None:
            await interaction.response.send_message("Use `/new` in DMs.", ephemeral=True)
            return
        user_id = str(interaction.user.id)
        if not await in_guild(interaction.user.id):
            return  # silent ignore
        await _close_active(user_id)
        user_state.pop(user_id, None)
        _save_sessions(user_state)
        await interaction.response.send_message("New conversation started.")

    @tree.command(name="comment", description="Leave feedback on the last Count turn (use 'global ...' for session-level).")
    @app_commands.describe(text="Your feedback. Prefix with 'global ' for a session-level note.")
    async def cmd_comment(interaction: discord.Interaction, text: str):
        if interaction.guild is not None:
            await interaction.response.send_message("Use `/comment` in DMs.", ephemeral=True)
            return
        user_id = str(interaction.user.id)
        if not await in_guild(interaction.user.id):
            return
        log = active_logs.get(user_id)
        if log is None:
            await interaction.response.send_message(
                "No active conversation yet. Send a message first.", ephemeral=True
            )
            return
        if text.lower().startswith("global "):
            log.append_comment_global(text[len("global "):].strip())
            await interaction.response.send_message("Global comment noted.")
        else:
            # Bind to the most recent assistant turn (turn_count - 1).
            attach_idx = max(log.turn_count - 1, 0)
            log.append_comment_attached(turn_index=attach_idx, text=text)
            await interaction.response.send_message(f"Comment noted on turn {attach_idx}.")

    @bot.event
    async def on_message(message: discord.Message):
        if message.author == bot.user:
            return
        # DMs only
        if not isinstance(message.channel, discord.DMChannel):
            return
        # Server membership gate
        if not await in_guild(message.author.id):
            return  # silent ignore
        if not message.content.strip():
            return

        user_id = str(message.author.id)
        display = message.author.display_name
        log = _ensure_log_for_user(user_id, display)
        user_turn_text = message.content

        # Append the user turn to the log immediately.
        log.append_user_turn(user_turn_text)
        log.render_markdown()
        _persist_state(user_id)

        # Typing indicator while we dispatch.
        async with message.channel.typing():
            await _dispatch(message, user_id, user_turn_text, log)

    async def _dispatch(
        message: discord.Message,
        user_id: str,
        user_text: str,
        log: TrainingLog,
    ) -> None:
        """Send user_text to the SDK, collect the full response, send it back,
        log it. One user turn → one assistant turn."""
        st = user_state.setdefault(user_id, {})
        resume = st.get("session_id")

        options = ClaudeAgentOptions(
            permission_mode="default",
            allowed_tools=TRAINING_ALLOWED_TOOLS,
            mcp_servers=GRAPHITI_MCP_SERVERS,
            can_use_tool=can_use_tool,
            model=DEFAULT_MODEL,
            cwd=str(COUNT_HOME),
            setting_sources=[],
        )
        if resume:
            options.resume = resume
        else:
            # System prompt only applies to fresh sessions; resume picks up
            # the existing one. Pass via --system-prompt-file because Windows
            # CreateProcessW caps the command line at 32767 chars.
            options.extra_args = {"system-prompt-file": str(sp_file)}

        full_text_blocks: list[str] = []
        tool_calls: list[dict] = []
        new_session_id: str | None = None
        run_cost = 0.0

        try:
            async for msg in query(prompt=_stream_prompt(user_text), options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            full_text_blocks.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_calls.append({
                                "name": block.name,
                                "args": block.input,
                                "result_preview": "",
                            })
                elif isinstance(msg, ResultMessage):
                    new_session_id = getattr(msg, "session_id", None) or new_session_id
                    cost = getattr(msg, "total_cost_usd", None) or 0.0
                    run_cost += cost
        except Exception as e:
            err_text = f"[training run error: {e}]"
            full_text_blocks.append(err_text)
            print(f"  [training dispatch error for {user_id}: {e}]")

        full_text = "\n\n".join(b.strip() for b in full_text_blocks if b.strip())
        if not full_text:
            full_text = "[no response]"

        log.append_assistant_turn(
            content=full_text,
            model=DEFAULT_MODEL,
            tool_calls=tool_calls,
        )
        log.render_markdown()

        # Persist updated session state
        if new_session_id:
            st["session_id"] = new_session_id
        st["total_cost"] = st.get("total_cost", 0.0) + run_cost
        _persist_state(user_id)

        # Send to Discord, chunking at 2000 chars.
        for chunk in _chunk_discord(full_text, 1900):
            await message.channel.send(chunk)
            await asyncio.sleep(0.25)

    def _chunk_discord(text: str, limit: int):
        """Split text into Discord-safe chunks under `limit` chars,
        preferring line boundaries."""
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                yield remaining
                return
            cut = remaining.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            yield remaining[:cut]
            remaining = remaining[cut:].lstrip("\n")

    try:
        await bot.start(token)
    finally:
        try:
            if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except Exception:
            pass
```

- [ ] **Step 2: Smoke-check imports**

Run:

```bash
python -c "from discord_training_gateway import run_discord_training; print('ok')"
```

Expected: `ok`. (Does not start the bot — just verifies imports resolve.)

- [ ] **Step 3: Commit**

```bash
git add discord_training_gateway.py
git commit -m "Add training Discord gateway: DM-only, /new, /comment, restricted tools"
```

---

## Task 8: Operator gateway scaffold — channel listener, mention/reply trigger, peer-keyed Honcho

**Files:**
- Create: `discord_operator_gateway.py`

The operator gateway is more like the Telegram gateway than the training bot is: it streams TextBlocks live, batches concurrent messages, has cost tracking and slash commands. The main novelties versus Telegram are: mention/reply triggering, username labelling, peer-keyed Honcho, group-chat context buffer.

- [ ] **Step 1: Create the gateway file skeleton**

Create `discord_operator_gateway.py`:

```python
"""Discord operator gateway for The Count.

Group-chat surface. Listens to one configured channel in one configured
guild. Dispatches only when @-mentioned or replied to; non-trigger
messages go into a rolling 30-message context buffer that rides along
with the next trigger. User messages are rendered with `[<DisplayName>]`
prefixes. Honcho memory is peer-keyed per Discord user.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from claude_agent_sdk import (
    ClaudeAgentOptions,
    query,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from count_agent import (
    COUNT_HOME,
    DEFAULT_MODEL,
    GRAPHITI_MCP_SERVERS,
    GRAPHITI_TOOLS,
    MODEL_ALIASES,
    MODEL_DISPLAY,
    _stream_prompt,
    build_full_prompt,
    build_voice_agent,
    load_discord_secrets,
    load_sessions,
    save_sessions,
    write_system_prompt_file,
)
from honcho_memory import HonchoMemory


CONTEXT_BUFFER_SIZE = 30
BATCH_WINDOW = 1.5
PID_FILE = COUNT_HOME / ".discord_op.pid"
SESSION_KEY = "discord_operator"


def _acquire_pidlock(pid_file: Path) -> bool:
    if pid_file.exists():
        try:
            old = int(pid_file.read_text().strip())
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, old)
                if handle:
                    kernel32.CloseHandle(handle)
                    return False
            else:
                os.kill(old, 0)
                return False
        except (ValueError, OSError, PermissionError):
            pass
    pid_file.write_text(str(os.getpid()))
    return True


def _release_pidlock(pid_file: Path) -> None:
    try:
        if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
            pid_file.unlink()
    except Exception:
        pass


def _peer_id_for_discord_user(discord_id: int) -> str:
    """Map a Discord user id to a Honcho peer id.

    Sequoyah's Honcho peer id stays the env-configured one if his Discord
    id matches HONCHO_SEQUOYAH_DISCORD_ID (so his Telegram memories carry
    over). All other Discord users get `discord-<id>` as their peer id.
    """
    seq_id = os.environ.get("HONCHO_SEQUOYAH_DISCORD_ID")
    if seq_id and str(discord_id) == str(seq_id):
        return os.environ.get("HONCHO_PEER_NAME", "Sequoyah")
    return f"discord-{discord_id}"


async def run_discord_operator():
    secrets = load_discord_secrets()
    token = secrets.get("discord_operator_token")
    guild_id = secrets.get("discord_operator_guild_id")
    channel_id = secrets.get("discord_operator_channel_id")
    if not (token and guild_id and channel_id):
        print(
            "ERROR: discord_operator_token / guild_id / channel_id missing in dg_secrets.json",
            file=sys.stderr,
        )
        sys.exit(1)

    if not _acquire_pidlock(PID_FILE):
        print(f"ERROR: operator gateway already running (PID {PID_FILE.read_text().strip()})", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    bot = discord.Client(intents=intents)
    tree = app_commands.CommandTree(bot)

    # Persistent session
    sessions = load_sessions()
    session_id: str | None = (sessions.get(SESSION_KEY) or {}).get("session_id")
    current_model: str = (sessions.get(SESSION_KEY) or {}).get("model") or DEFAULT_MODEL

    # Cost / state
    start_time = datetime.now()
    total_cost = 0.0
    total_dispatches = 0

    # Pending batch (sender_display, sender_id, content, ts)
    pending: list[tuple[str, int, str, datetime]] = []
    busy = False
    dispatch_task: asyncio.Task | None = None
    # Rolling context buffer of non-trigger messages
    context_buffer: collections.deque = collections.deque(maxlen=CONTEXT_BUFFER_SIZE)

    # Honcho (peer-keyed)
    honcho = HonchoMemory()
    await honcho.init()

    # Write system-prompt file
    sp_file = write_system_prompt_file("discord_operator")

    async def stream_text_block(channel: discord.abc.Messageable, text: str) -> None:
        """Send a chunk of orchestrator text to Discord, splitting at 1900 chars."""
        text = text.strip()
        if not text:
            return
        for chunk in _chunk_discord(text, 1900):
            await channel.send(chunk)
            await asyncio.sleep(0.25)

    @bot.event
    async def on_ready():
        print(f"Discord operator gateway online as {bot.user}")
        try:
            synced = await tree.sync()
            print(f"  Synced {len(synced)} slash commands.")
        except Exception as e:
            print(f"  [tree.sync failed: {e}]")

    @tree.command(name="stop", description="Cancel running dispatch.")
    async def cmd_stop(interaction: discord.Interaction):
        nonlocal busy, dispatch_task
        if dispatch_task and not dispatch_task.done():
            dispatch_task.cancel()
            await interaction.response.send_message("Cancelling dispatch...")
        else:
            await interaction.response.send_message("Nothing to cancel.", ephemeral=True)
        busy = False

    @tree.command(name="model", description="View or switch model.")
    @app_commands.describe(alias="opus / sonnet / haiku (omit to view current)")
    async def cmd_model(interaction: discord.Interaction, alias: str = ""):
        nonlocal current_model, session_id
        if not alias:
            name = MODEL_DISPLAY.get(current_model, current_model)
            await interaction.response.send_message(
                f"Current: {name}\nAvailable: {' / '.join(MODEL_ALIASES.keys())}"
            )
            return
        if alias.lower() in MODEL_ALIASES:
            current_model = MODEL_ALIASES[alias.lower()]
            session_id = None  # new model means new session
            await interaction.response.send_message(f"Model switched to {alias.lower()}.")
        else:
            await interaction.response.send_message(
                f"Unknown model. Available: {' / '.join(MODEL_ALIASES.keys())}",
                ephemeral=True,
            )

    @tree.command(name="status", description="Uptime, model, session, cost.")
    async def cmd_status(interaction: discord.Interaction):
        uptime = datetime.now() - start_time
        h, rem = divmod(int(uptime.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        avg = total_cost / max(total_dispatches, 1)
        await interaction.response.send_message(
            f"Uptime: {h}h {m}m {s}s\n"
            f"Model: {MODEL_DISPLAY.get(current_model, current_model)}\n"
            f"Session: {(session_id or 'none')[:8]}\n"
            f"Dispatches: {total_dispatches}\n"
            f"Total cost: ${total_cost:.4f} (${avg:.4f}/dispatch)\n"
            f"Buffer: {len(context_buffer)} msgs"
        )

    @tree.command(name="compact", description="Clear SDK context, keep Honcho.")
    async def cmd_compact(interaction: discord.Interaction):
        nonlocal session_id
        session_id = None
        await interaction.response.send_message("Context cleared; Honcho memory preserved.")

    @tree.command(name="reset", description="Hard reset session.")
    async def cmd_reset(interaction: discord.Interaction):
        nonlocal session_id
        session_id = None
        context_buffer.clear()
        pending.clear()
        await interaction.response.send_message("Session reset. Fresh start.")

    @tree.command(name="ping", description="Health check.")
    async def cmd_ping(interaction: discord.Interaction):
        await interaction.response.send_message("Pong.")

    @bot.event
    async def on_message(message: discord.Message):
        nonlocal busy, dispatch_task
        if message.author == bot.user:
            return
        if message.channel.id != channel_id:
            return
        if not message.content.strip():
            return

        # Decide trigger
        is_mention = bot.user in message.mentions
        is_reply = (
            message.reference is not None
            and message.reference.resolved is not None
            and getattr(message.reference.resolved, "author", None) == bot.user
        )

        sender = message.author.display_name
        # Always add to context buffer
        context_buffer.append((sender, message.content, message.created_at, message.author.id))

        if not (is_mention or is_reply):
            return  # silent context-only

        pending.append((sender, message.author.id, message.content, message.created_at))

        # Coalesce within BATCH_WINDOW
        await asyncio.sleep(BATCH_WINDOW)
        if not pending or busy:
            return
        busy = True
        try:
            batch = list(pending)
            pending.clear()
            dispatch_task = asyncio.create_task(_dispatch_batch(message.channel, batch))
            await dispatch_task
        finally:
            busy = False
            dispatch_task = None

    async def _dispatch_batch(
        channel: discord.abc.Messageable,
        batch: list[tuple[str, int, str, datetime]],
    ) -> None:
        nonlocal session_id, total_cost, total_dispatches

        # Build the user-visible prompt: context buffer (excluding the trigger
        # messages themselves) + labelled trigger messages.
        trigger_keys = {(b[0], b[2], b[3]) for b in batch}
        buffer_lines = [
            f"[{sender}] {text}"
            for (sender, text, ts, _uid) in context_buffer
            if (sender, text, ts) not in trigger_keys
        ]
        trigger_lines = [f"[{sender}] {text}" for (sender, _uid, text, _ts) in batch]

        if buffer_lines:
            user_payload = (
                "Recent channel context (you weren't addressed, just listening):\n"
                + "\n".join(buffer_lines)
                + "\n\n--- you are addressed now ---\n"
                + "\n".join(trigger_lines)
            )
        else:
            user_payload = "\n".join(trigger_lines)

        # Honcho recall against the *first* trigger sender's peer
        trigger_peer = _peer_id_for_discord_user(batch[0][1])
        recall_ctx = await honcho.recall(user_payload, user_id=trigger_peer)
        if recall_ctx:
            user_payload = (
                f"[Honcho recall for {batch[0][0]}]\n{recall_ctx}\n\n--- message ---\n{user_payload}"
            )

        options = ClaudeAgentOptions(
            allowed_tools=[
                "Read", "Write", "Edit", "Glob", "Grep", "Bash",
                "WebSearch", "WebFetch", "Task",
                *GRAPHITI_TOOLS,
            ],
            agents={"voice": build_voice_agent()},
            mcp_servers=GRAPHITI_MCP_SERVERS,
            permission_mode="bypassPermissions",
            cwd=str(COUNT_HOME),
            max_turns=90,
            model=current_model,
            setting_sources=[],
        )
        if session_id:
            options.resume = session_id
        else:
            options.extra_args = {"system-prompt-file": str(sp_file)}

        text_buffer: list[str] = []
        new_session_id: str | None = None
        run_cost = 0.0

        try:
            async for msg in query(prompt=_stream_prompt(user_payload), options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and getattr(block, "parent_tool_use_id", None) is None:
                            await stream_text_block(channel, block.text)
                            text_buffer.append(block.text)
                elif isinstance(msg, ResultMessage):
                    new_session_id = getattr(msg, "session_id", None) or new_session_id
                    run_cost += getattr(msg, "total_cost_usd", None) or 0.0
        except asyncio.CancelledError:
            await channel.send("[cancelled]")
            raise
        except Exception as e:
            await channel.send(f"[dispatch error: {e}]")
            print(f"  [operator dispatch error: {e}]")
            return

        full_response = "\n\n".join(text_buffer)
        if not full_response:
            await channel.send("[no text output]")

        # Persist session
        if new_session_id:
            session_id = new_session_id
            sessions[SESSION_KEY] = {"session_id": session_id, "model": current_model}
            save_sessions(sessions)

        total_cost += run_cost
        total_dispatches += 1

        # Store in Honcho — attribute the user side to the trigger sender's peer
        if full_response:
            await honcho.store(
                user_payload,
                full_response,
                user_id=trigger_peer,
            )

    try:
        await bot.start(token)
    finally:
        _release_pidlock(PID_FILE)


def _chunk_discord(text: str, limit: int):
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        yield remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")
```

- [ ] **Step 2: Smoke-check imports**

Run:

```bash
python -c "from discord_operator_gateway import run_discord_operator; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add discord_operator_gateway.py
git commit -m "Add operator Discord gateway: mention/reply trigger, peer-keyed Honcho, slash commands"
```

---

## Task 9: New CLI subcommands `discord-op` and `discord-train` in `count_agent.py`

**Files:**
- Modify: `count_agent.py` (the command dispatcher at the bottom, around lines ~1925–1965)

- [ ] **Step 1: Find the existing CLI dispatch**

Run: `grep -n "command == " C:/Users/shken/the-count-agent/count_agent.py | head -20`
Expected: rows showing branches like `elif command == "telegram":`, `elif command == "chat":`, etc.

- [ ] **Step 2: Add the two new branches**

Locate the block of `elif command ==` branches. Add new branches alongside `telegram`:

```python
    elif command == "discord-op":
        from discord_operator_gateway import run_discord_operator
        asyncio.run(run_discord_operator())
    elif command == "discord-train":
        from discord_training_gateway import run_discord_training
        asyncio.run(run_discord_training())
```

Also update the help text near `print("  python count_agent.py telegram")` to include the new commands. Find that block and add:

```python
        print("  python count_agent.py discord-op")
        print("  python count_agent.py discord-train")
```

- [ ] **Step 3: Smoke-check the dispatcher**

Run: `python count_agent.py discord-op 2>&1 | head -2`
Expected: an error about missing secrets (`ERROR: discord_operator_token / guild_id / channel_id missing in dg_secrets.json`) — proves the branch is wired and the gateway started up.

- [ ] **Step 4: Commit**

```bash
git add count_agent.py
git commit -m "Wire discord-op and discord-train CLI subcommands"
```

---

## Task 10: `start_gateway.py` — flags, auto-restart supervisor, sentinel watcher

**Files:**
- Modify: `start_gateway.py`

Bigger change to the launcher. Add: (a) `--with-discord-op` / `--with-discord-train` / `--no-telegram` flags, (b) auto-restart-with-backoff for Discord children, (c) sentinel-file watcher that handles Telegram-side restart requests.

- [ ] **Step 1: Replace `start_gateway.py` with the new shape**

Replace the entire contents of `start_gateway.py` with:

```python
"""Launcher for the Count's always-on services.

Default: proxy + telegram (unchanged behavior).
Optional: --with-discord-op, --with-discord-train, --no-telegram

Discord children get auto-restart with exponential backoff (5s, 30s, 5min,
then give up). Each crash and each retry emits a Telegram notification via
~/.count/tools/tg.py.

A small sentinel-file watcher lets the Telegram gateway request
start/stop/status on the Discord children by writing files to
~/.count/.requests/.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).parent
COUNT_HOME = Path.home() / ".count"
LOGS = COUNT_HOME / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
REQUESTS_DIR = COUNT_HOME / ".requests"
REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
TG_HELPER = COUNT_HOME / "tools" / "tg.py"

PROXY_HOST = os.environ.get("COUNT_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("COUNT_PROXY_PORT", "8787"))
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"

DISCORD_BACKOFFS = [5, 30, 300]  # seconds; len = max attempts


def wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def tee(proc: subprocess.Popen, log_path: Path, label: str) -> threading.Thread:
    def run():
        with open(log_path, "w", buffering=1, encoding="utf-8", errors="replace") as f:
            for line in proc.stdout:
                text = line.decode("utf-8", errors="replace")
                f.write(text)
                f.flush()
                sys.stdout.write(f"[{label}] {text}")
                sys.stdout.flush()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def spawn(args: list[str], env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
    )


def terminate(proc: subprocess.Popen):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def notify_telegram(text: str) -> None:
    if not TG_HELPER.exists():
        sys.stdout.write(f"[supervisor] (no tg.py) {text}\n")
        return
    try:
        subprocess.run(
            [sys.executable, str(TG_HELPER), text],
            timeout=20,
            check=False,
        )
    except Exception as e:
        sys.stdout.write(f"[supervisor] tg.py failed: {e}\n")


class DiscordChild:
    """Supervises one Discord gateway subprocess with auto-restart."""

    def __init__(self, label: str, command_arg: str, log_path: Path, env: dict):
        self.label = label
        self.command_arg = command_arg  # "discord-op" or "discord-train"
        self.log_path = log_path
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.attempt = 0  # how many restarts have happened in the current crash burst
        self.last_start_at: float = 0.0
        self.giving_up: bool = False  # True after exhausted retries until reset
        self.stop_requested: bool = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self.stop_requested = False
            self.giving_up = False
            self.attempt = 0
            self._spawn()

    def _spawn(self) -> None:
        self.proc = spawn([sys.executable, "-u", "count_agent.py", self.command_arg], env=self.env)
        tee(self.proc, self.log_path, self.label)
        self.last_start_at = time.monotonic()
        sys.stdout.write(f"[supervisor] spawned {self.label} (pid={self.proc.pid})\n")

    def stop(self) -> None:
        with self._lock:
            self.stop_requested = True
            terminate(self.proc)
            self.proc = None

    def status(self) -> str:
        if self.proc and self.proc.poll() is None:
            uptime = int(time.monotonic() - self.last_start_at)
            return f"{self.label}: running (pid={self.proc.pid}, uptime={uptime}s)"
        if self.giving_up:
            return f"{self.label}: down (gave up after retries — restart manually)"
        return f"{self.label}: stopped"

    def tick(self) -> None:
        """Called from the supervisor loop. Detects crashes and triggers restart."""
        if self.proc is None or self.stop_requested:
            return
        if self.proc.poll() is None:
            return
        rc = self.proc.returncode
        sys.stdout.write(f"[supervisor] {self.label} exited (rc={rc}); attempt {self.attempt + 1}/{len(DISCORD_BACKOFFS)}\n")
        notify_telegram(f"{self.label} crashed (rc={rc}); attempt {self.attempt + 1}/{len(DISCORD_BACKOFFS)}")
        if self.attempt >= len(DISCORD_BACKOFFS):
            self.giving_up = True
            self.proc = None
            notify_telegram(f"{self.label} down — manual restart needed (try /{self.command_arg.replace('-', '_')}_start from Telegram)")
            return
        delay = DISCORD_BACKOFFS[self.attempt]
        self.attempt += 1
        sys.stdout.write(f"[supervisor] retrying {self.label} in {delay}s\n")
        time.sleep(delay)
        with self._lock:
            if self.stop_requested:
                return
            self._spawn()


def handle_request_file(path: Path, op_child: DiscordChild | None, train_child: DiscordChild | None) -> None:
    """Handle a single sentinel request file. The filename encodes the request:
        start-discord-op, stop-discord-op, status-discord-op
        start-discord-train, stop-discord-train, status-discord-train
    """
    name = path.name
    try:
        path.unlink()
    except FileNotFoundError:
        return

    if name.endswith("-discord-op"):
        child = op_child
    elif name.endswith("-discord-train"):
        child = train_child
    else:
        return
    if child is None:
        notify_telegram(f"[supervisor] {name}: gateway not enabled in this run.")
        return

    if name.startswith("start-"):
        child.stop()  # clean slate
        time.sleep(1)
        child.start()
        notify_telegram(f"{child.label}: started.")
    elif name.startswith("stop-"):
        child.stop()
        notify_telegram(f"{child.label}: stopped.")
    elif name.startswith("status-"):
        notify_telegram(child.status())


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--with-discord-op", action="store_true")
    p.add_argument("--with-discord-train", action="store_true")
    p.add_argument("--no-telegram", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    proxy = spawn([sys.executable, "-u", "count_proxy.py"])
    tee(proxy, LOGS / "proxy.log", "proxy")
    if not wait_for_port(PROXY_HOST, PROXY_PORT, timeout=10.0):
        sys.stderr.write(f"[start_gateway] proxy failed to open {PROXY_URL} within 10s\n")
        terminate(proxy)
        sys.exit(1)
    sys.stdout.write(f"[start_gateway] proxy listening on {PROXY_URL}\n")

    child_env = os.environ.copy()
    child_env["ANTHROPIC_BASE_URL"] = PROXY_URL

    telegram = None
    if not args.no_telegram:
        telegram = spawn([sys.executable, "-u", "count_agent.py", "telegram"], env=child_env)
        tee(telegram, LOGS / "gateway.log", "gateway")

    op_child = train_child = None
    if args.with_discord_op:
        op_child = DiscordChild("discord_op", "discord-op", LOGS / "discord_op.log", child_env)
        op_child.start()
    if args.with_discord_train:
        train_child = DiscordChild("discord_train", "discord-train", LOGS / "discord_train.log", child_env)
        train_child.start()

    def shutdown(*_):
        sys.stdout.write("[start_gateway] shutting down\n")
        if op_child:
            op_child.stop()
        if train_child:
            train_child.stop()
        terminate(telegram)
        terminate(proxy)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    while True:
        # Telegram death = full shutdown (load-bearing surface).
        if telegram and telegram.poll() is not None:
            sys.stdout.write(f"[start_gateway] telegram exited ({telegram.returncode}); tearing down\n")
            if op_child: op_child.stop()
            if train_child: train_child.stop()
            terminate(proxy)
            sys.exit(telegram.returncode or 0)
        # Proxy death = full shutdown
        if proxy.poll() is not None:
            sys.stdout.write(f"[start_gateway] proxy exited ({proxy.returncode}); tearing down\n")
            if op_child: op_child.stop()
            if train_child: train_child.stop()
            terminate(telegram)
            sys.exit(proxy.returncode or 1)

        # Tick Discord children (auto-restart if crashed)
        if op_child: op_child.tick()
        if train_child: train_child.tick()

        # Drain sentinel-file requests
        try:
            for req in REQUESTS_DIR.iterdir():
                if req.is_file():
                    handle_request_file(req, op_child, train_child)
        except FileNotFoundError:
            pass

        time.sleep(1.0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-check the launcher's argparse**

Run: `python start_gateway.py --help`
Expected: usage line showing `--with-discord-op`, `--with-discord-train`, `--no-telegram`.

- [ ] **Step 3: Commit**

```bash
git add start_gateway.py
git commit -m "Supervisor: flags, auto-restart Discord children, sentinel-file control plane"
```

---

## Task 11: Telegram-side slash commands for Discord child management

**Files:**
- Modify: `count_agent.py` (the Telegram slash-command handler at lines ~1304 onward; and the `setMyCommands` list at ~1285)

Six new commands on the Telegram surface: `/discord_op_start`, `/discord_op_stop`, `/discord_op_status`, `/discord_train_start`, `/discord_train_stop`, `/discord_train_status`. Each writes a sentinel file to `~/.count/.requests/` that the supervisor consumes.

- [ ] **Step 1: Find the Telegram command registration block**

Run: `grep -n 'setMyCommands\|"command":' C:/Users/shken/the-count-agent/count_agent.py | head -30`
Expected: shows the `setMyCommands` block listing existing commands.

- [ ] **Step 2: Extend the registered command list**

Find the `await tg_api(http, token, "setMyCommands", {"commands": [...]})` call. Add to that list:

```python
                {"command": "discord_op_start", "description": "Start the operator Discord bot"},
                {"command": "discord_op_stop", "description": "Stop the operator Discord bot"},
                {"command": "discord_op_status", "description": "Status of operator Discord bot"},
                {"command": "discord_train_start", "description": "Start the training Discord bot"},
                {"command": "discord_train_stop", "description": "Stop the training Discord bot"},
                {"command": "discord_train_status", "description": "Status of training Discord bot"},
```

- [ ] **Step 3: Add handler branches inside `handle_command`**

Inside the `handle_command` function (search for `async def handle_command`), add new branches near the bottom (just before its `return False`):

```python
            if c in {
                "/discord_op_start", "/discord_op_stop", "/discord_op_status",
                "/discord_train_start", "/discord_train_stop", "/discord_train_status",
            }:
                verb, _, target = c[1:].partition("_")
                if target.startswith("discord_op"):
                    sentinel_name = f"{verb}-discord-op"
                else:
                    sentinel_name = f"{verb}-discord-train"
                req_dir = COUNT_HOME / ".requests"
                req_dir.mkdir(parents=True, exist_ok=True)
                (req_dir / sentinel_name).write_text(str(datetime.now().isoformat()))
                await tg_send(http, token, chat_id, f"Request queued: {sentinel_name}")
                return True
```

- [ ] **Step 4: Smoke-check the syntax**

Run: `python -c "import count_agent; print('ok')"`
Expected: `ok` (just verifies the file parses).

- [ ] **Step 5: Commit**

```bash
git add count_agent.py
git commit -m "Telegram: slash commands for managing Discord gateways via sentinels"
```

---

## Task 12: Manual integration smoke checklist

**Files:**
- Create: `docs/superpowers/plans/2026-05-14-smoke-checklist.md`

Discord bots don't unit-test well end-to-end. This is the manual verification pass — run through it after the build is done with real bot tokens.

- [ ] **Step 1: Write the smoke checklist file**

Create `docs/superpowers/plans/2026-05-14-smoke-checklist.md`:

```markdown
# Discord Gateways — Smoke Checklist

Run through this once both Discord bots are created in the Discord
developer portal and tokens are in `~/.count/dg_secrets.json`.

## Prep

- [ ] Two Discord applications/bots created in
      https://discord.com/developers/applications
- [ ] Both bots invited to the Disco Gotterdammerung server with these
      scopes/permissions:
      - operator: `bot`, `applications.commands` — needs Read Messages, Send
        Messages, Read Message History in the one operator channel.
      - training: `bot`, `applications.commands` — needs the same DM
        permissions plus View Server Members (for the membership check).
- [ ] `dg_secrets.json` has all five Discord keys filled (operator token,
      operator guild_id, operator channel_id, training token, training
      guild_id).
- [ ] Optional: `HONCHO_SEQUOYAH_DISCORD_ID=<your discord user id>` exported
      so Sequoyah's Discord identity maps to his existing Telegram Honcho
      peer.

## Boot

- [ ] `python start_gateway.py --with-discord-op --with-discord-train`
      starts all four (proxy, telegram, discord_op, discord_train).
- [ ] Each child's log appears under `~/.count/logs/`.
- [ ] Telegram heartbeat still fires (no regression).

## Operator gateway

- [ ] In the configured channel, talk to your buddy without mentioning the
      bot: no dispatch happens.
- [ ] `@TheCount what's up?` triggers a dispatch; the response streams in
      real time as text blocks land.
- [ ] Reply to one of the bot's messages without `@`-mentioning it; that
      also triggers a dispatch.
- [ ] After three non-trigger messages and then a mention, the recent
      context buffer is included in the prompt (verify by asking the bot
      to summarize what was just said).
- [ ] `/status`, `/model`, `/compact`, `/reset`, `/ping`, `/stop` all
      respond.
- [ ] On restart, the operator session resumes (asking "what were we
      just talking about" works across a process restart).
- [ ] Buddy talks for the first time: Honcho creates a new peer (visible
      in `logs/discord_op.log` as a peer-create event).

## Training gateway

- [ ] DM the training bot from your own account: a `~/.count/training/<id>/<uuid>/`
      directory appears; `metadata.json` is populated; `conversation.jsonl`
      and `conversation.md` accumulate after each turn.
- [ ] A response arrives. Tool-use traces (if any) land inline in the
      JSONL `tool_calls` array.
- [ ] `/comment that's wrong` writes to `comments.md` under "Turn N".
- [ ] `/comment global his energy is great` writes to the global section.
- [ ] `/new` closes the current session (`ended_at` written to
      `metadata.json`, line appended to `~/.count/training/INDEX.jsonl`)
      and starts a fresh one on the next DM.
- [ ] The Count can call `Read`, `Glob`, `Grep`. The Count can call
      `mcp__graphiti__search_nodes`. The Count cannot call
      `mcp__graphiti__add_memory` (predicate denies).
- [ ] The Count can run `python ~/.count/tools/kgraph.py similar "foo"`.
      The Count cannot run `python ~/.count/tools/kgraph.py cypher "..."`
      (predicate denies).
- [ ] A friend (different Discord account, also in the server) DMs and
      gets a separate session directory under their own user-id folder.
- [ ] A non-member DM is silently ignored.

## Supervision

- [ ] Manually kill the discord_train process (`taskkill /PID <pid> /F`).
      Within ~5s the supervisor logs the crash and respawns; Telegram
      receives the "discord_train crashed, attempt 1/3" message.
- [ ] Kill it three times in quick succession (let each respawn first).
      Supervisor gives up; Telegram receives "discord_train down — manual
      restart needed".
- [ ] `/discord_train_start` from Telegram revives the bot.
- [ ] `/discord_train_status` reports the new uptime.
- [ ] Telegram death still triggers full shutdown (verify by killing the
      telegram gateway and confirming everything else dies).
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-05-14-smoke-checklist.md
git commit -m "Add Discord gateways smoke checklist"
```

---

## Self-Review Notes

Each spec requirement is covered:

- **Operator gateway behavior** — Task 8 (file, dispatch, slash commands, peer-keyed Honcho).
- **Training gateway behavior** — Task 7 (file, DM listener, slash commands, tool restriction, log writer integration).
- **Bash predicate** — Task 3, with adversarial unit tests.
- **Logging layout** — Task 4, with round-trip tests.
- **Peer-keyed Honcho** — Task 2, with race-safety test.
- **Launcher flags + supervisor + sentinels** — Task 10.
- **Telegram slash commands** — Task 11.
- **System-prompt modes** — Task 5.
- **Secrets loader** — Task 6.
- **CLI subcommands** — Task 9.
- **Smoke verification** — Task 12.

Type/method consistency checked:
- `TrainingLog.append_user_turn`, `.append_assistant_turn`, `.append_comment_attached`, `.append_comment_global`, `.render_markdown`, `.close`, `.open` — same names used in Task 4 (definition) and Task 7 (consumption).
- `HonchoMemory.get_or_create_user_peer`, `.recall(user_id=…)`, `.store(user_id=…)` — same in Task 2 (definition) and Task 8 (consumption).
- `is_safe_kgraph_invocation` — same in Task 3 (definition) and Task 7 (consumption via `can_use_tool`).
- `load_discord_secrets` — same in Task 6 (definition) and Tasks 7/8 (consumption).
- CLI subcommand strings: `"discord-op"`, `"discord-train"` consistent across Task 9, Task 10's supervisor spawn, and Task 11's sentinel naming.
- Sentinel filenames: `<verb>-discord-<op|train>` consistent in Task 10's `handle_request_file` and Task 11's `handle_command`.

No placeholders, TODOs, or "implement later" lines.
