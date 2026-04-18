"""
Honcho vector memory integration for The Count.

Provides the "unbidden resonance" layer — associative recall that surfaces
relevant memories the agent wasn't explicitly looking for. Works alongside
file-based memory (MEMORY.md, USER.md) which handles deliberate recall.

Uses honcho-ai 2.1.0 SDK. All methods gracefully degrade on failure.
"""

import asyncio
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


class HonchoMemory:
    """Async Honcho integration for The Count's vector memory.

    Usage:
        memory = HonchoMemory()
        await memory.init()               # call once at startup
        ctx = await memory.recall(msg)     # before dispatch — get relevant context
        await memory.store(msg, response)  # after dispatch — store the exchange
    """

    def __init__(self):
        self.api_key = os.environ.get("HONCHO_API_KEY", "")
        self.workspace_id = os.environ.get("HONCHO_WORKSPACE", "The-Count-2")
        self.ai_peer_id = os.environ.get("HONCHO_AI_PEER", "The-Count")
        self.user_peer_id = os.environ.get("HONCHO_PEER_NAME", "Sequoyah")
        self.session_id = "telegram-global"

        self._ready = False
        self._aio = None
        self._ai_peer = None
        self._user_peer = None
        self._session = None

        # Stats
        self.recalls = 0
        self.stores = 0
        self.errors = 0

    async def init(self) -> bool:
        """Initialize Honcho client, workspace, peers, and session.

        Returns True if Honcho is operational, False otherwise.
        """
        if not self.api_key:
            print("  [honcho] No API key — running without vector memory")
            return False

        try:
            from honcho import Honcho, HonchoAio
            from honcho.api_types import SessionPeerConfig

            client = Honcho(
                api_key=self.api_key,
                environment="production",
                workspace_id=self.workspace_id,
            )
            self._aio = HonchoAio(client)

            # Get or create peers and session
            self._ai_peer = await self._aio.peer(self.ai_peer_id)
            self._user_peer = await self._aio.peer(self.user_peer_id)
            self._session = await self._aio.session(self.session_id)

            # Configure peer observation
            observe = SessionPeerConfig(observe_me=True, observe_others=True)
            self._session.add_peers([
                (self._ai_peer, observe),
                (self._user_peer, observe),
            ])

            self._ready = True
            print(f"  [honcho] Connected — workspace={self.workspace_id}, "
                  f"peers={self.ai_peer_id}/{self.user_peer_id}, "
                  f"session={self.session_id}")
            return True

        except Exception as e:
            self.errors += 1
            print(f"  [honcho] Init failed: {e}")
            print("  [honcho] Running without vector memory — file-based memory still active")
            return False

    async def recall(self, user_message: str) -> str:
        """Query Honcho for context relevant to the user's message.

        Runs a dialectic query — Honcho's LLM reasons over the full
        peer representation to surface what "reminds" it of the current
        message. This is the "unbidden resonance" layer.

        Returns context string to inject into the prompt, or empty string.
        """
        if not self._ready:
            return ""

        try:
            level = self._reasoning_level(user_message)
            result = await asyncio.to_thread(
                self._user_peer.chat,
                user_message,
                target=self._ai_peer,
                reasoning_level=level,
            )
            self.recalls += 1
            if result:
                # Cap at 600 chars to avoid bloating the prompt
                if len(result) > 600:
                    result = result[:600].rsplit(" ", 1)[0] + " ..."
                return result
            return ""

        except Exception as e:
            self.errors += 1
            logger.debug("Honcho recall failed: %s", e)
            return ""

    async def store(self, user_message: str, assistant_response: str):
        """Store an exchange in Honcho for future recall.

        Fire-and-forget safe — errors are logged but don't propagate.
        """
        if not self._ready or not user_message or not assistant_response:
            return

        try:
            messages = [
                self._user_peer.message(user_message),
                self._ai_peer.message(assistant_response),
            ]
            await asyncio.to_thread(self._session.add_messages, messages)
            self.stores += 1
        except Exception as e:
            self.errors += 1
            logger.debug("Honcho store failed: %s", e)

    def status(self) -> str:
        """Return a status string for the /honcho command."""
        if not self._ready:
            return "Honcho: offline (no API key or init failed)"
        return (
            f"Honcho: online\n"
            f"  Workspace: {self.workspace_id}\n"
            f"  Peers: {self.ai_peer_id} / {self.user_peer_id}\n"
            f"  Session: {self.session_id}\n"
            f"  Recalls: {self.recalls} | Stores: {self.stores} | Errors: {self.errors}"
        )

    @staticmethod
    def _reasoning_level(query: str) -> str:
        """Pick reasoning level based on message complexity.

        Short messages get minimal reasoning (fast, cheap).
        Longer messages get deeper reasoning (slower, more associative).
        """
        n = len(query)
        if n < 120:
            return "low"
        if n < 400:
            return "medium"
        return "high"
