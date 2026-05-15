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
