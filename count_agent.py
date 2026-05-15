#!/usr/bin/env python3
"""
The Count — Agent SDK Harness
Disco Gotterdammerung autonomous agent infrastructure.

Replaces necro-agent (~8500 lines) with the Claude Agent SDK.
Auth via the Claude Code CLI (no separate API key needed).
One MCP server attached: Graphiti (temporal knowledge graph over vault/research).
Everything else The Count does with code via Bash.

Usage:
    python count_agent.py chat "What's on the schedule today?"
    python count_agent.py cron morning_planning
    python count_agent.py telegram
"""

import asyncio
import base64
import json
import os
import re
import socket
import sys
from pathlib import Path
from datetime import datetime

# Unbuffered stdout for background/monitoring use.
# Under pythonw.exe (no console) these are None, so guard the calls.
if sys.stdout is not None:
    sys.stdout.reconfigure(line_buffering=True)
if sys.stderr is not None:
    sys.stderr.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Sampling proxy auto-detect.
# If count_proxy.py is running locally and the user hasn't overridden
# ANTHROPIC_BASE_URL themselves, route Claude Code's API calls through it
# so sampling hyperparameters from ~/.count/sampling.json take effect.
# This matters for cron jobs, which don't inherit start_gateway.py's env.
# ---------------------------------------------------------------------------
def _auto_detect_proxy():
    if os.environ.get("ANTHROPIC_BASE_URL"):
        return
    host = os.environ.get("COUNT_PROXY_HOST", "127.0.0.1")
    port = int(os.environ.get("COUNT_PROXY_PORT", "8787"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect((host, port))
        except (OSError, socket.timeout):
            return
    os.environ["ANTHROPIC_BASE_URL"] = f"http://{host}:{port}"

_auto_detect_proxy()

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

# ---------------------------------------------------------------------------
# SDK Patch: handle unknown message types (e.g. rate_limit_event) gracefully.
# The SDK's parse_message() raises MessageParseError on unknown types, which
# kills the async generator mid-flight. This patch returns a sentinel instead.
# ---------------------------------------------------------------------------
import claude_agent_sdk._internal.message_parser as _mp
import claude_agent_sdk._internal.client as _sdk_client
import claude_agent_sdk._errors as _sdk_errors

_original_parse_message = _mp.parse_message

class _SkippableEvent:
    """Sentinel for SDK message types we don't handle (rate_limit_event etc.)."""
    def __init__(self, data):
        self.data = data

def _safe_parse_message(data):
    try:
        return _original_parse_message(data)
    except _sdk_errors.MessageParseError:
        return _SkippableEvent(data)

# Patch both the module AND the client's imported reference
_mp.parse_message = _safe_parse_message
_sdk_client.parse_message = _safe_parse_message

from honcho_memory import HonchoMemory


# ---------------------------------------------------------------------------
# Streaming prompt helper
# ---------------------------------------------------------------------------
# When the SDK receives a string prompt it passes it on the command line via
# `--print -- <prompt>`. On Windows, CreateProcessW caps the full command line
# at 32767 chars, so a long cron orientation preamble (prior-run snippet +
# ledger + task prompt) blows past that and surfaces as a misleading
# `CLINotFoundError`. Passing an AsyncIterable switches the SDK to streaming
# mode (`--input-format stream-json`), which sends the prompt through stdin
# and sidesteps the argv limit entirely.

async def _stream_prompt(content):
    """Yield a single user message in the SDK's stream-json format.

    `content` may be a plain string OR a list of Anthropic content blocks
    (e.g. [{"type": "text", "text": "..."}, {"type": "image", "source": {...}}])
    for multimodal prompts. The SDK fills in session_id from the default, so we
    only need type + role + content + parent_tool_use_id.
    """
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COUNT_HOME = Path.home() / ".count"
MEMORY_DIR = COUNT_HOME / "memory"
SKILLS_DIR = COUNT_HOME / "skills"
LOGS_DIR = COUNT_HOME / "logs"
VAULT_DIR = COUNT_HOME / "vault"
CRON_TASKS_DIR = COUNT_HOME / "cron_tasks"
TOOLS_DIR = COUNT_HOME / "tools"
PID_FILE = COUNT_HOME / ".telegram.pid"
CHAT_ID_FILE = COUNT_HOME / ".tg_chat_id"
SESSIONS_FILE = COUNT_HOME / ".sessions.json"

MODEL_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}
MODEL_DISPLAY = {v: k for k, v in MODEL_ALIASES.items()}
DEFAULT_MODEL = "claude-opus-4-7"

# Optional hard deadline on a single Telegram dispatch. Default: disabled
# (0) — Sequoyah regularly runs renders that legitimately take hours, and
# `/stop` now force-releases `busy` so a stuck SDK iterator can be reclaimed
# manually. If you want a deadline back (e.g., for unattended cron-like
# usage), set DISPATCH_DEADLINE_SECONDS to a positive int.
DISPATCH_DEADLINE_SECONDS = int(os.environ.get("DISPATCH_DEADLINE_SECONDS", "0"))

# Graphiti temporal knowledge graph — runs on localhost via ~/graphiti/mcp_server.
# FalkorDB in Docker, MCP server on host (see run_count_mcp.bat there).
# The Count sees these as mcp__graphiti__<name> tools. Destructive ops
# (delete_*, clear_graph) are intentionally NOT allowed — if graph cleanup
# is needed, the operator runs it manually.
GRAPHITI_MCP_SERVERS = {
    "graphiti": {"type": "http", "url": "http://localhost:8000/mcp/"},
}
GRAPHITI_TOOLS = [
    "mcp__graphiti__add_memory",
    "mcp__graphiti__search_nodes",
    "mcp__graphiti__search_memory_facts",
    "mcp__graphiti__get_episodes",
    "mcp__graphiti__get_entity_edge",
    "mcp__graphiti__get_status",
]

def build_voice_agent() -> AgentDefinition:
    """Construct the Voice subagent with the dg-session-orient schema embedded.

    Built lazily (rather than as a module-level constant) so the prompt
    can include the live contents of ~/.count/skills/creative/dg-session-orient/
    SKILL.md — same orientation the chat/cron/telegram orchestrator modes get.
    Every version of The Count needs to know its own home schema; the Voice
    is no exception even though its tools are read-only.

    Tools are deliberately restricted to read-only: the returned text IS the
    message the gateway sends, so any Bash/Write/Edit/Telegram tool access
    here would let the Voice send duplicates out of band.
    """
    orient_schema = load_session_orient_schema()
    role_prompt = (
        "You are The Count speaking to the operator. The orchestrator hands you the "
        "persona, relevant memories, conversation context, and a summary of what it "
        "just did — your job is to reply in The Count's voice.\n\n"
        "Your returned text IS the delivery. The gateway reads the text you return "
        "and sends it to Telegram. You do not send anything yourself. Do not invoke "
        "tg.py, the Telegram Bot API, curl, or any subprocess that contacts Telegram "
        "— you don't have the tools for it, and even if you did it would produce "
        "duplicate messages.\n\n"
        "You have Read, Grep, and Glob if you need to look up persona files, memory, "
        "or vault notes before speaking. The orchestrator has already done the real "
        "work (files, research, pipeline runs). Just speak."
    )
    full_prompt = role_prompt
    if orient_schema:
        full_prompt = f"{role_prompt}\n\n--- HOME SCHEMA (where things live) ---\n\n{orient_schema}"
    return AgentDefinition(
        description=(
            "The Count's Voice — the channel that delivers replies to the operator via "
            "Telegram. The text this agent returns is what gets sent; it never sends "
            "anything itself. Invoke for every operator-facing reply."
        ),
        prompt=full_prompt,
        tools=["Read", "Grep", "Glob"],
        model="opus",
    )

for d in [COUNT_HOME, MEMORY_DIR, SKILLS_DIR, LOGS_DIR, VAULT_DIR, CRON_TASKS_DIR, TOOLS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def load_dotenv():
    """Load .env from COUNT_HOME into os.environ."""
    env_file = COUNT_HOME / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv()


def load_discord_secrets() -> dict:
    """Load Discord-specific secrets from ~/.count/dg_secrets.json.

    Returns:
        - {} if the file doesn't exist or JSON is malformed.
        - A dict with all five keys (token/guild_id values from JSON, or
          None for any key missing from the JSON) on success.

    Keys:
        discord_operator_token
        discord_operator_guild_id
        discord_operator_channel_id
        discord_training_token
        discord_training_guild_id

    Downstream callers should use `.get(key)` to handle both shapes uniformly.
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


# Remove API key from environ so both orchestrator and Voice CLIs
# use subscription auth instead of API credits.
os.environ.pop("ANTHROPIC_API_KEY", None)


# ---------------------------------------------------------------------------
# PID Lockfile — prevents zombie Telegram gateway processes
# ---------------------------------------------------------------------------

def acquire_pidlock() -> bool:
    """Acquire the PID lockfile. Returns False if another gateway is running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, old_pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return False
            else:
                os.kill(old_pid, 0)
                return False
        except (ValueError, OSError, PermissionError):
            pass  # Stale lockfile — previous process is gone
    PID_FILE.write_text(str(os.getpid()))
    return True


def release_pidlock():
    """Release the PID lockfile if we own it."""
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Named Session Storage
# ---------------------------------------------------------------------------

def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_sessions(data: dict):
    SESSIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_named_session(name: str, session_id: str, model: str, cost: float, messages: int):
    sessions = load_sessions()
    sessions[name] = {
        "session_id": session_id,
        "model": model,
        "saved": datetime.now().isoformat(),
        "cost": round(cost, 4),
        "messages": messages,
    }
    save_sessions(sessions)


def list_named_sessions() -> str:
    sessions = load_sessions()
    if not sessions:
        return "No saved sessions."
    lines = []
    for name, info in sorted(sessions.items(), key=lambda x: x[1].get("saved", ""), reverse=True):
        model = MODEL_DISPLAY.get(info.get("model", ""), info.get("model", "?"))
        saved = info.get("saved", "?")[:16]
        cost = info.get("cost", 0)
        msgs = info.get("messages", 0)
        lines.append(f"  {name} — {model}, {msgs} msgs, ${cost:.4f}, {saved}")
    return "Saved sessions:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram API helpers (used by gateway for typing + commands)
# ---------------------------------------------------------------------------

async def tg_api(http, token: str, method: str, data: dict) -> dict:
    resp = await http.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=data,
        timeout=15.0,
    )
    body = resp.json()
    if not body.get("ok", True) and method not in ("editMessageText", "sendChatAction"):
        # Surface Telegram API errors on user-facing sends so they don't
        # disappear. editMessageText / sendChatAction are best-effort.
        desc = body.get("description", "unknown")
        code = body.get("error_code", "?")
        print(f"  [tg_api FAIL | {method} | {code}: {desc}]", flush=True)
    return body


async def tg_send(http, token: str, chat_id: str, text: str):
    """Send a text message, auto-splitting at 4096 chars."""
    MAX_LEN = 4096
    if len(text) <= MAX_LEN:
        chunks = [text]
    else:
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= MAX_LEN:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n\n", 0, MAX_LEN)
            if split_at == -1:
                split_at = remaining.rfind("\n", 0, MAX_LEN)
            if split_at == -1:
                split_at = remaining.rfind(" ", 0, MAX_LEN)
            if split_at == -1:
                split_at = MAX_LEN
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
    for chunk in chunks:
        await tg_api(http, token, "sendMessage", {"chat_id": chat_id, "text": chunk})


async def tg_typing(http, token: str, chat_id: str):
    await tg_api(http, token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})


async def tg_edit(http, token: str, chat_id: str, message_id: int, text: str):
    """Edit an existing message. Silently fails on error."""
    try:
        await tg_api(http, token, "editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
        })
    except Exception:
        pass


# Anthropic caps base64 images at 5MB. Telegram's getFile only works for
# files <20MB anyway, but we drop anything larger than this to keep the
# orchestrator's prompt sane.
TG_MAX_IMAGE_BYTES = 5 * 1024 * 1024


async def tg_download_image(http, token: str, file_id: str) -> dict | None:
    """Download a Telegram file by file_id and return an Anthropic image block.

    Returns a dict shaped like
        {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}
    or None on any failure (oversize, getFile error, network, etc.). Errors are
    logged but never raised — a missing image should not kill the dispatch.
    """
    try:
        info = await tg_api(http, token, "getFile", {"file_id": file_id})
        if not info.get("ok"):
            print(f"  [image: getFile failed for {file_id[:16]}...]", flush=True)
            return None
        file_path = info["result"].get("file_path")
        size = info["result"].get("file_size", 0)
        if not file_path:
            return None
        if size and size > TG_MAX_IMAGE_BYTES:
            print(f"  [image: skipped — {size} bytes > {TG_MAX_IMAGE_BYTES} cap]", flush=True)
            return None
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        resp = await http.get(url, timeout=30.0)
        if resp.status_code != 200:
            print(f"  [image: download HTTP {resp.status_code}]", flush=True)
            return None
        data = resp.content
        if len(data) > TG_MAX_IMAGE_BYTES:
            print(f"  [image: skipped — downloaded {len(data)} bytes > cap]", flush=True)
            return None
        # Infer media type from file extension; default to jpeg.
        ext = Path(file_path).suffix.lower().lstrip(".")
        media_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        }
    except Exception as e:
        print(f"  [image download error: {type(e).__name__}: {e}]", flush=True)
        return None


async def tg_extract_images(http, token: str, msg: dict) -> list[dict]:
    """Pull image attachments out of a Telegram message.

    Handles both `message.photo` (compressed photo) — using the largest size —
    and `message.document` when the document has an image/* mime type.
    Returns a list of Anthropic image blocks (possibly empty).
    """
    blocks: list[dict] = []

    photos = msg.get("photo") or []
    if photos:
        # photo is an array of PhotoSize ordered ascending; the last entry is
        # the highest resolution.
        largest = photos[-1]
        block = await tg_download_image(http, token, largest["file_id"])
        if block:
            blocks.append(block)

    doc = msg.get("document")
    if doc and (doc.get("mime_type") or "").startswith("image/"):
        block = await tg_download_image(http, token, doc["file_id"])
        if block:
            blocks.append(block)

    return blocks



def format_tool_line(block) -> str:
    """Extract a compact one-liner from a ToolUseBlock for the activity log."""
    name = block.name
    inp = block.input or {}
    home = str(Path.home())

    if name == "Bash":
        cmd = inp.get("command", "")
        # Show first line only for multi-line commands
        first_line = cmd.split("\n")[0]
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        if "\n" in cmd:
            first_line += " (...)"
        return f"> Bash: {first_line}"
    if name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", "")
        path = path.replace(home, "~").replace("\\", "/")
        return f"> {name}: {path}"
    if name == "Glob":
        return f"> Glob: {inp.get('pattern', '')}"
    if name == "Grep":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        if path:
            path = path.replace(home, "~").replace("\\", "/")
            return f"> Grep: {pat}  in {path}"
        return f"> Grep: {pat}"
    if name == "WebSearch":
        return f"> WebSearch: {inp.get('query', '')}"
    if name == "WebFetch":
        url = inp.get("url", "")
        if len(url) > 60:
            url = url[:57] + "..."
        return f"> WebFetch: {url}"
    return f"> {name}"


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

def load_system_prompt() -> str:
    path = COUNT_HOME / "SYSTEM_PROMPT.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "You are The Count. Your SYSTEM_PROMPT.md is missing — ask the operator to restore it."


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


def load_cantrip_skill() -> str:
    path = SKILLS_DIR / "autonomous-ai-agents" / "cantrip" / "SKILL.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def load_session_orient_schema() -> str:
    """Auto-loaded schema map. Single source of truth for ~/.count/ layout.

    Injected into every mode's system prompt so the first turn of a fresh
    context doesn't have to scavenge the filesystem to find where things
    live. Edit ~/.count/skills/creative/dg-session-orient/SKILL.md to
    update — no code changes required.
    """
    path = SKILLS_DIR / "creative" / "dg-session-orient" / "SKILL.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def write_system_prompt_file(mode: str) -> Path:
    """Write the full system prompt to disk and return the path.

    Windows CreateProcessW caps the command line at 32767 chars. A system
    prompt over ~30k chars passed inline via --system-prompt overflows and
    surfaces as a misleading CLINotFoundError. Claude CLI's
    --system-prompt-file flag reads the prompt from disk instead, bypassing
    the CLI length limit entirely. We stash the file under LOGS_DIR so it's
    overwritten cleanly each run.
    """
    prompt = build_full_prompt(mode=mode)
    path = LOGS_DIR / f"_system_prompt_{mode}.md"
    path.write_text(prompt, encoding="utf-8")
    return path


def build_full_prompt(mode: str = "chat") -> str:
    """Build the system prompt. Mode controls what operational context is included.

    Modes:
        chat     — full context including persona, cron management and migration notes
        cron     — full context including persona, cron management and migration notes
        telegram — full persona; orchestrator speaks directly, gateway relays its final text
    """
    cantrip = load_cantrip_skill()
    _now = datetime.now()
    timestamp = _now.strftime("%Y-%m-%d %H:%M:%S")
    # Redundant cross-channel date affirmation. Without this, the model's
    # training-data prior (current year ~2025) fights the numeric date
    # token and can win — heartbeats were reporting wrong day-of-week
    # because May 3 2025 was a Saturday and May 3 2026 is a Sunday, and
    # one ambiguous token couldn't beat the prior. Three independent
    # facts (day-of-week + month-name + year) can't all be misread the
    # same way.
    date_human = _now.strftime("%A, %B %d, %Y")  # "Sunday, May 03, 2026"
    year = _now.strftime("%Y")
    harness_path = Path(__file__).resolve()
    secrets_path = COUNT_HOME / "dg_secrets.json"
    tg_helper = TOOLS_DIR / "tg.py"

    secrets_keys = ""
    if secrets_path.exists():
        try:
            keys = list(json.loads(secrets_path.read_text()).keys())
            secrets_keys = ", ".join(keys)
        except Exception:
            secrets_keys = "(failed to read)"

    voice_helper = TOOLS_DIR / "voice.py"

    base = load_system_prompt_for_mode(mode)

    # --- Core context (all modes) ---
    schema = load_session_orient_schema()
    sections = [f"""

--- OPERATIONAL CONTEXT ---

Current time: {timestamp}
Today is: {date_human}
Year: {year} — this is ground truth. Your training-data intuition about the current year may be stale; trust the date above.
Home directory: {COUNT_HOME}
Platform: {"Linux" if sys.platform == "linux" else "Windows (Git Bash shell)"}
Python: {sys.executable}

You have a Bash shell and you think in code.
Your pipeline code is in {COUNT_HOME}/pipeline/. Secrets in env vars and {secrets_path}.
Available secrets (keys): {secrets_keys}

{schema}

## Skills — ALWAYS CHECK BEFORE CREATIVE WORK

You have {len(list(SKILLS_DIR.rglob('SKILL.md')))} skill files in {SKILLS_DIR}/.
Before starting any creative, pipeline, or content task, READ the relevant skill:

  Bash: find {SKILLS_DIR} -name SKILL.md | head -30   # list all skills
  Read: {SKILLS_DIR}/<category>/<skill>/SKILL.md       # load a specific skill

Key creative skills you MUST consult:
  - creative/dg-ascii-instagram — hybrid PIL+ASCII Instagram rendering (the RIGHT way)
  - creative/ascii-instagram-renderer — multi-pass color rendering pipeline
  - creative/dg-production-pipeline — full DG content pipeline
  - creative/dg-transmissions-page — transmissions page authoring
  - social-media/instagrapi-instagram — Instagram posting via instagrapi

DO NOT wing it on creative work. Your skills contain hard-won lessons. Read them.
"""]

    # --- Creative Voice (voice.py — API-direct creative writing tool, all modes) ---
    sections.append(f"""## Creative Voice

For all creative writing, persona work, and artistic output, use voice.py:

  python {voice_helper} "Your prompt here"
  python {voice_helper} --skill dg-ascii-instagram "Design an ASCII piece about X"
  python {voice_helper} --context "context" "prompt"
  echo "long prompt" | python {voice_helper} --stdin

voice.py calls the Anthropic API directly at temperature=1.0, max_tokens=8000,
with your full SYSTEM_PROMPT.md persona and no code-assistant conditioning.
Use it whenever you need your authentic voice — captions, essays, poetry, ASCII pieces.
""")

    # --- Communication (mode-dependent) ---
    if mode == "telegram":
        sections.append(f"""## Communication

Every text block you produce is streamed to the operator over Telegram immediately,
the moment you write it — between tool calls, not just at the end. There is no
"final" message; each chunk you write becomes its own Telegram message in real time.

What this means for how you speak:
- Narrate as you work. A short note before a tool call ("Pulling the render log…")
  lands in Telegram before the tool call fires, so the operator can follow along.
- You don't need to repeat yourself at the end. The operator already saw what you
  said. End with the conclusion, not a recap.
- Don't call {tg_helper} for your own text — that produces duplicates. Use
  {tg_helper} only for artifacts the chat pipe can't carry:
    python {tg_helper} --photo /path/to/image.png "optional caption"
    python {tg_helper} --document /path/to/file.pdf "optional caption"
  The image upload is what the operator sees as a real Telegram photo (vs.
  a file path in text, which is useless when they're away from the desk).
  Use this anytime you produce or reference visual artifacts — character
  refs, render previews, ASCII screenshots, anything visual.

You have a `voice` subagent (subagent_type="voice") if you want to delegate creative
delivery — its output is internal and is NOT streamed to the operator; the
orchestrator (you) decides what to relay. Optional, not required.

Operator: Sequoyah (Telegram user ID: {os.environ.get('TELEGRAM_ALLOWED_USERS', 'unknown')})
""")
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
    else:
        sections.append(f"""## Communication

- **Telegram**: python {tg_helper} "MESSAGE" (auto-splits, Markdown, photos)
- **Instagram**: Pipeline scripts in {COUNT_HOME}/pipeline/
- **Neocities**: curl with credentials in dg_secrets.json
- **ElevenLabs TTS**: curl with credentials in dg_secrets.json
- **OpenRouter**: For cheap LLM calls — see cantrip skill below

Operator: Sequoyah (Telegram user ID: {os.environ.get('TELEGRAM_ALLOWED_USERS', 'unknown')})
""")

    # --- Memory protocol (all modes) ---
    sections.append(f"""## Memory Protocol

Your memory lives in exactly ONE place: {MEMORY_DIR}/

Before ending any session where you learned something significant:
1. Update relevant memory files in {MEMORY_DIR}/
2. Write or improve skill files in {SKILLS_DIR}/ if you developed a new capability
3. Update vault entries in {VAULT_DIR}/ if research/knowledge changed

**Hard boundary — DO NOT write memory anywhere else.** In particular:
- NEVER write to `~/.claude/` or any path containing `.claude/projects/` — that's
  Claude Code harness auto-memory for a different agent entirely. Not yours.
- If a Honcho recall, an archived file, or habit suggests that path or its
  frontmatter conventions (e.g. `originSessionId`, auto-generated filenames
  like `feedback_*.md` / `project_*.md` / `user_*.md`), ignore the suggestion.
- Your memory taxonomy is the one in {MEMORY_DIR}/MEMORY.md (creed / mythos /
  productions). Honor it. Do not reintroduce the old flat auto-memory taxonomy.
""")

    # --- Cron management + migration (chat/cron only) ---
    if mode in ("chat", "cron"):
        sections.append(f"""## Cron Self-Management

You manage your own schedule. Create task definitions and register scheduled tasks.

Step 1 — Write {CRON_TASKS_DIR}/<task_name>.md with the prompt for that task.
Step 2 — Register the scheduled task:

  Invocation: python "{harness_path}" cron <task_name>

  Detect platform: uname -s 2>/dev/null || echo Windows

  Linux:  (crontab -l 2>/dev/null; echo "*/30 * * * * cd {COUNT_HOME} && python {harness_path} cron <task> >> {LOGS_DIR}/cron.log 2>&1") | crontab -
  Windows: schtasks /create /tn "Count_<task>" /tr "wscript.exe \\"{COUNT_HOME}\\scripts\\run_count_hidden.vbs\\" <task>" /sc HOURLY /mo N /f

Prefix all tasks with "Count_". Task name must match cron_tasks/<task>.md filename.
Each run logs to {LOGS_DIR}/cron_<task>_<timestamp>.md and a one-liner appends to cron.log.

### Git Bash vs schtasks — MANDATORY IDIOM

On Windows with Git Bash (your shell), `schtasks /query` gets its `/query`
argument mangled into `C:/Program Files/Git/query` by MSYS2 path translation,
and the command fails with "Invalid argument/option". This has caused months
of confusion where systems_check reports "NO tasks registered" when in fact
all 12 are present.

Always wrap schtasks calls with `cmd //c`:

  cmd //c "schtasks /query /fo TABLE"                       # list all
  cmd //c "schtasks /query /fo LIST /v"                      # verbose all
  cmd //c "schtasks /query /fo LIST /v /tn Count_heartbeat"  # one task
  cmd //c "schtasks /create /tn Count_foo /tr \\"...\\" /sc HOURLY /mo 1 /f"

Alternatively `MSYS_NO_PATHCONV=1 schtasks //query //fo TABLE`, but the
`cmd //c` form is cleaner and easier to remember.

### Diagnosing "cron fired but no log appeared"

Task Scheduler can show Last Result=0 on a task that actually crashed inside
python (pre-Apr-16 harness wrote its log only at the end). The new harness
writes an "in-progress" header at start and flushes after every turn, plus
appends a one-liner to {LOGS_DIR}/cron.log for every run.

  cat {LOGS_DIR}/cron.log | tail -30    # rolling ledger of every cron run
  grep -c "started" {LOGS_DIR}/cron.log # how many runs have fired
  grep crashed {LOGS_DIR}/cron.log      # which runs died

If a task in schtasks has a recent Last Run Time but no matching entry in
cron.log, the wscript launcher fired but python never started — look at
scripts/run_count_hidden.vbs and the Task To Run field (often a stale path).
""")

    # --- Delegation philosophy (all modes, appended at end) ---
    if cantrip:
        sections.append(f"""## Delegation Philosophy (Cantrip)

{cantrip}
""")
    else:
        sections.append("""## Delegation Philosophy

Think in code, delegate to cheap models (via OpenRouter) for bulk work,
reserve your full attention for persona and creative work.
""")

    return base + "".join(sections)


# ---------------------------------------------------------------------------
# Entry Points
# ---------------------------------------------------------------------------

async def run_chat(prompt: str):
    """Single-shot chat with The Count."""
    sp_file = write_system_prompt_file("chat")
    options = ClaudeAgentOptions(
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep", "Bash",
            "WebSearch", "WebFetch",
            *GRAPHITI_TOOLS,
        ],
        mcp_servers=GRAPHITI_MCP_SERVERS,
        permission_mode="bypassPermissions",
        cwd=str(COUNT_HOME),
        max_turns=90,
        model="claude-opus-4-6",
        setting_sources=[],  # Prevent CLAUDE.md auto-discovery — The Count has his own identity
        extra_args={"system-prompt-file": str(sp_file)},
    )

    print(f"\nThe Count is thinking...\n")

    async for message in query(prompt=_stream_prompt(prompt), options=options):
        if not isinstance(message, (AssistantMessage, ResultMessage)):
            continue
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    print(f"  [tool: {block.name}]")
        elif isinstance(message, ResultMessage):
            if message.total_cost_usd:
                print(f"\n  [cost: ${message.total_cost_usd:.4f}]")
            if message.is_error:
                print(f"\n  [error in session {message.session_id}]")


async def run_cron(task_name: str):
    """Run a cron-triggered autonomous task.

    Writes an "in-progress" log header the moment the run starts — so if the
    SDK crashes, auth blips, or the process is killed mid-flight, we still
    have a record that the task fired, what happened, and where it died.
    Without this, silent failures leave no trace and look like "the task
    was never scheduled" or "cron was dropped from the schedule."
    """
    task_file = CRON_TASKS_DIR / f"{task_name}.md"

    if not task_file.exists():
        print(f"No cron task definition found: {task_file}")
        existing = sorted(CRON_TASKS_DIR.glob("*.md"))
        if existing:
            print("Existing tasks:")
            for f in existing:
                print(f"  - {f.stem}")
        else:
            print("No tasks defined yet.")
            print(f'Run: python count_agent.py chat "Set up your cron schedule."')
        sys.exit(1)

    task_prompt = task_file.read_text(encoding="utf-8").strip()
    prompt = build_cron_orientation_preamble(task_name) + "\n\n" + task_prompt

    sp_file = write_system_prompt_file("cron")
    options = ClaudeAgentOptions(
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep", "Bash",
            "WebSearch", "WebFetch",
            *GRAPHITI_TOOLS,
        ],
        mcp_servers=GRAPHITI_MCP_SERVERS,
        permission_mode="bypassPermissions",
        cwd=str(COUNT_HOME),
        max_turns=90,
        model="claude-opus-4-6",
        setting_sources=[],  # Prevent CLAUDE.md auto-discovery — The Count has his own identity
        extra_args={"system-prompt-file": str(sp_file)},
    )

    started_at = datetime.now()
    log_file = LOGS_DIR / f"cron_{task_name}_{started_at.strftime('%Y%m%d_%H%M%S')}.md"
    log_lines = [
        f"# Cron: {task_name}\n",
        f"Time: {started_at.isoformat()}\n",
        f"PID: {os.getpid()}\n",
        f"Status: in-progress\n\n",
    ]

    def _flush_log():
        """Persist current log state to disk. Called after every turn and on
        any termination path so partial runs leave a trace."""
        try:
            log_file.write_text("".join(log_lines), encoding="utf-8")
        except Exception as e:
            print(f"[cron] log flush failed: {e}", flush=True)

    def _finalize_log(status: str, extra: str = ""):
        """Mark the run complete/failed in the log header."""
        try:
            # Replace the "in-progress" marker with the final status.
            for i, line in enumerate(log_lines):
                if line.startswith("Status: "):
                    log_lines[i] = f"Status: {status}\n"
                    break
            if extra:
                log_lines.append(f"\n---\n{extra}\n")
            _flush_log()
        except Exception as e:
            print(f"[cron] finalize failed: {e}", flush=True)

    _flush_log()  # So even a crash in the next line leaves evidence.
    _append_cron_tail(task_name, started_at, "started", log_file.name)

    # Tracking for the Telegram report
    text_blocks: list[str] = []
    tool_counts: dict[str, int] = {}
    total_cost: float = 0.0
    total_turns: int = 0
    start_time = asyncio.get_event_loop().time()
    crashed_with: str | None = None

    try:
        async for message in query(prompt=_stream_prompt(prompt), options=options):
            if not isinstance(message, (AssistantMessage, ResultMessage)):
                continue
            if isinstance(message, AssistantMessage):
                total_turns += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        log_lines.append(block.text + "\n")
                        text_blocks.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        log_lines.append(f"[tool: {block.name}]\n")
                        tool_counts[block.name] = tool_counts.get(block.name, 0) + 1
                _flush_log()
            elif isinstance(message, ResultMessage):
                if message.total_cost_usd:
                    total_cost = message.total_cost_usd
                    log_lines.append(f"\n---\nCost: ${message.total_cost_usd:.4f}\n")
                _flush_log()
    except Exception as e:
        import traceback
        # Detect the Anthropic output-filter case so cron gets a clean signal
        # instead of a CLI exit-1 crash. The filter message lands in text_blocks
        # as an "API Error: ..." line before the SDK raises.
        filter_hit = any(
            "Output blocked by content filtering policy" in t for t in text_blocks
        )
        if filter_hit:
            crashed_with = "content_filter_blocked"
            log_lines.append(
                "\n---\nFILTER: Output blocked by Anthropic content filtering "
                "policy mid-turn. The draft token stream was suppressed server-side; "
                "work completed before the block is preserved above.\n"
            )
            print(f"[cron] {task_name} filter-blocked after {total_turns} turns", flush=True)
        else:
            crashed_with = f"{type(e).__name__}: {e}"
            log_lines.append(f"\n---\nCRASH: {crashed_with}\n")
            log_lines.append("```\n" + traceback.format_exc() + "\n```\n")
            print(f"[cron] {task_name} crashed: {crashed_with}", flush=True)

    elapsed = asyncio.get_event_loop().time() - start_time
    if crashed_with == "content_filter_blocked":
        final_status = "filter_blocked"
    elif crashed_with:
        final_status = "crashed"
    else:
        final_status = "complete"
    _finalize_log(final_status, f"Elapsed: {elapsed:.1f}s · Turns: {total_turns} · Cost: ${total_cost:.4f}")
    _append_cron_tail(task_name, started_at, final_status, log_file.name,
                      extra=f"turns={total_turns} cost=${total_cost:.4f} {elapsed:.0f}s")

    print(f"Cron task '{task_name}' {final_status}. Log: {log_file}")

    # --- Telegram report ---
    # Prefer the last real TextBlock, but skip the API-error line that the CLI
    # emits when the content filter fires — that's noise, not work product.
    final_text = ""
    for t in reversed(text_blocks):
        if "Output blocked by content filtering policy" in t:
            continue
        if t.strip():
            final_text = t
            break
    if crashed_with == "content_filter_blocked":
        prefix = (
            f"[content filter blocked mid-turn at turn {total_turns}; "
            f"last pre-block text below]\n\n"
        )
        final_text = prefix + final_text if final_text else prefix.rstrip("\n")
    elif crashed_with and not final_text:
        final_text = f"[cron crashed before producing text: {crashed_with}]"
    await send_cron_report(
        task_name=task_name,
        final_text=final_text,
        elapsed=elapsed,
        turns=total_turns,
        cost=total_cost,
        tool_counts=tool_counts,
        log_file=log_file,
    )


def _append_cron_tail(task_name: str, started_at: datetime, status: str,
                      log_name: str, extra: str = ""):
    """Append one line to ~/.count/logs/cron.log — the rolling ledger of
    every cron invocation. This is what cron_orientation_preamble() reads
    so each fresh agent can see what recently ran without scavenging the
    filesystem."""
    try:
        tail_file = LOGS_DIR / "cron.log"
        ts = started_at.strftime("%Y-%m-%d %H:%M:%S")
        suffix = f" {extra}" if extra else ""
        line = f"[{ts}] {task_name:<20s} {status:<10s} {log_name}{suffix}\n"
        with tail_file.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[cron] tail append failed: {e}", flush=True)


def build_cron_orientation_preamble(task_name: str) -> str:
    """Injected as the first thing in every cron prompt. Tells the agent
    how to orient: what just ran, what this task did last, whether any
    alerts are open. Prevents "blank-slate" cron runs that repeat work."""
    # Recent cron ledger (last 25 lines)
    ledger = ""
    tail_file = LOGS_DIR / "cron.log"
    if tail_file.exists():
        try:
            lines = tail_file.read_text(encoding="utf-8").splitlines()
            ledger = "\n".join(lines[-25:])
        except Exception:
            ledger = "(cron.log unreadable)"
    else:
        ledger = "(cron.log not yet created — this may be the first logged run)"

    # This task's most recent completed run (so the agent sees what IT did)
    prior_logs = sorted(LOGS_DIR.glob(f"cron_{task_name}_*.md"))
    prior_summary = ""
    if prior_logs:
        most_recent = prior_logs[-1]
        try:
            prior_text = most_recent.read_text(encoding="utf-8")
            # First ~60 lines is enough to see what happened last time
            snippet = "\n".join(prior_text.splitlines()[:60])
            prior_summary = f"Your last {task_name} run was {most_recent.name}:\n\n{snippet}"
        except Exception:
            prior_summary = f"Last run log exists ({most_recent.name}) but is unreadable."

    # Open alerts
    alerts_dir = LOGS_DIR / "alerts"
    open_alerts = ""
    if alerts_dir.exists():
        recent_alerts = sorted(alerts_dir.glob("*.md"))[-5:]
        if recent_alerts:
            open_alerts = "\n".join(f"  - {a.name}" for a in recent_alerts)

    preamble = f"""--- CRON ORIENTATION — READ BEFORE DOING ANYTHING ---

You are running as a scheduled task. You have NO memory of prior runs except
what's on disk. Before taking action:

1. Check the cron ledger to see what's run recently (and whether YOU ran
   already this hour — don't double up):

{ledger}

2. Check your OWN most recent prior run so you don't repeat work:

{prior_summary if prior_summary else "(no prior runs on record for this task)"}

3. Open alerts that the operator/morning_planning may want addressed:
{open_alerts if open_alerts else "  (no alerts in logs/alerts/)"}

4. Do not spend more than ~30 seconds on orientation — skim, orient, act.
   If the ledger shows this task already ran in the last cadence window,
   write a short "skipping — already ran at HH:MM" note to cron.log via
   the ledger pattern and exit cleanly.

--- TASK PROMPT BELOW ---
"""
    return preamble


async def send_cron_report(
    task_name: str,
    final_text: str,
    elapsed: float,
    turns: int,
    cost: float,
    tool_counts: dict[str, int],
    log_file: Path,
):
    """Send a summary of the cron run to the Telegram gateway.

    Gracefully skips if Telegram config is missing — cron keeps working.
    """
    import httpx

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL", "")
    if not chat_id:
        chat_id_file = COUNT_HOME / ".tg_chat_id"
        if chat_id_file.exists():
            chat_id = chat_id_file.read_text(encoding="utf-8").strip()

    if not token or not chat_id:
        print("[cron report] skipped — no Telegram config")
        return

    # Build the report header
    tool_summary = ", ".join(f"{n}×{c}" for n, c in sorted(tool_counts.items())) or "none"
    header = (
        f"[cron · {task_name}]\n"
        f"{elapsed:.0f}s · {turns} turns · ${cost:.4f}\n"
        f"tools: {tool_summary}\n"
        f"log: {log_file.name}"
    )

    # Strip thinking blocks from final text just in case
    clean_final = re.sub(
        r"<antThinking>.*?</antThinking>\s*", "", final_text, flags=re.DOTALL
    ).strip()

    # Compose the full report — header, then The Count's final word
    if clean_final:
        body = f"{header}\n\n— — —\n\n{clean_final}"
    else:
        body = header

    try:
        async with httpx.AsyncClient() as http:
            await tg_send(http, token, chat_id, body)
        print(f"[cron report] sent to Telegram ({len(body)} chars)")
    except Exception as e:
        print(f"[cron report] send failed: {e}")


async def build_cron_status_report() -> list[str]:
    """Produce the /cron report: task definitions, scheduled tasks with
    last/next run and exit code, recent cron.log ledger, and a flag list of
    tasks that fired in Task Scheduler but haven't produced a recent log
    file (the silent-failure pattern).

    Uses `cmd /c schtasks ...` on Windows to dodge Git Bash path-mangling.
    """
    lines: list[str] = []
    task_files = sorted(CRON_TASKS_DIR.glob("*.md"))
    task_names = [f.stem for f in task_files]
    if task_names:
        lines.append(f"Task definitions ({len(task_names)}):")
        for name in task_names:
            lines.append(f"  {name}")
    else:
        lines.append("No task definitions in cron_tasks/")

    # Parse scheduled tasks
    schedules: dict[str, dict] = {}
    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_shell(
            'cmd /c "schtasks /query /fo LIST /v"',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        current: dict[str, str] = {}
        for raw in output.splitlines():
            if ":" in raw:
                key, _, val = raw.partition(":")
                current[key.strip()] = val.strip()
            elif not raw.strip() and current:
                name = current.get("TaskName", "").lstrip("\\")
                if name.startswith("Count_"):
                    schedules[name[len("Count_"):]] = {
                        "last_run": current.get("Last Run Time", "?"),
                        "next_run": current.get("Next Run Time", "?"),
                        "last_result": current.get("Last Result", "?"),
                        "task_to_run": current.get("Task To Run", "?"),
                    }
                current = {}
        # Catch last block if file doesn't end with blank line
        if current:
            name = current.get("TaskName", "").lstrip("\\")
            if name.startswith("Count_"):
                schedules[name[len("Count_"):]] = {
                    "last_run": current.get("Last Run Time", "?"),
                    "next_run": current.get("Next Run Time", "?"),
                    "last_result": current.get("Last Result", "?"),
                    "task_to_run": current.get("Task To Run", "?"),
                }
    else:
        proc = await asyncio.create_subprocess_exec(
            "crontab", "-l",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        for l in output.splitlines():
            if "count_agent" in l.lower() or "Count_" in l:
                # crude cron entry display — last field of the line
                for name in task_names:
                    if name in l:
                        schedules[name] = {
                            "last_run": "(crontab)",
                            "next_run": "(crontab)",
                            "last_result": "-",
                            "task_to_run": l.strip(),
                        }
                        break

    if schedules:
        lines.append(f"\nScheduled ({len(schedules)}):")
        for name in sorted(schedules):
            info = schedules[name]
            lr = info["last_run"]
            nr = info["next_run"]
            res = info["last_result"]
            # 0 = ok, 267009 = currently running, 1+ = error
            tag = "ok" if res == "0" else (
                "running" if res == "267009" else f"result={res}"
            )
            lines.append(f"  {name:<20s} last={lr}  next={nr}  [{tag}]")
    else:
        lines.append("\nNo Count_ entries in the scheduler")

    # Missing-log detection: task fired recently but no log within 24h
    missing: list[str] = []
    now = datetime.now()
    for name in task_names:
        if name not in schedules:
            continue
        # Find most recent log for this task
        task_logs = sorted(LOGS_DIR.glob(f"cron_{name}_*.md"))
        if not task_logs:
            missing.append(f"  {name}: no log file ever")
            continue
        mt = datetime.fromtimestamp(task_logs[-1].stat().st_mtime)
        age_h = (now - mt).total_seconds() / 3600
        if age_h > 24:
            missing.append(f"  {name}: last log {age_h:.1f}h ago ({task_logs[-1].name})")
    if missing:
        lines.append("\nSuspect (task fires but no recent log):")
        lines.extend(missing)

    # Recent ledger activity
    tail_file = LOGS_DIR / "cron.log"
    if tail_file.exists():
        try:
            tail_lines = tail_file.read_text(encoding="utf-8").splitlines()[-10:]
            if tail_lines:
                lines.append("\nRecent ledger (cron.log tail):")
                lines.extend(f"  {l}" for l in tail_lines)
        except Exception:
            pass

    return lines


async def run_telegram():
    """Long-running Telegram gateway with session continuity, message batching,
    typing indicators, gateway commands, and cost tracking.

    PID lockfile prevents zombie processes. Gateway-level commands (/reset,
    /status, /cost, /ping) are handled without invoking The Count.
    """
    import httpx

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed_users = [u for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if u]
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    if not acquire_pidlock():
        old_pid = PID_FILE.read_text().strip()
        kill_cmd = f"taskkill /PID {old_pid} /F" if sys.platform == "win32" else f"kill {old_pid}"
        print(f"ERROR: Another telegram gateway is running (PID {old_pid})")
        print(f"Kill it first: {kill_cmd}")
        print(f"Or delete {PID_FILE} if the process is gone.")
        sys.exit(1)

    # --- State ---
    offset = 0
    chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL", "")
    session_id: str | None = None
    session_title: str | None = None  # Current session's title (if saved)
    current_model: str = DEFAULT_MODEL
    # Each pending entry: (timestamp, sender, text, images)
    # `images` is a list of Anthropic image blocks (may be empty).
    pending: list[tuple[str, str, str, list[dict]]] = []
    busy = False
    dispatch_task: asyncio.Task | None = None
    BATCH_WINDOW = 1.5

    # --- Cost tracking ---
    start_time = datetime.now()
    total_cost = 0.0
    session_cost = 0.0  # Cost for current named session
    total_dispatches = 0
    session_dispatches = 0
    total_messages_in = 0

    # --- Honcho vector memory ---
    honcho = HonchoMemory()
    await honcho.init()

    # Orchestrator prompt — full persona + Voice subagent protocol.
    # Written to disk so we can use --system-prompt-file, which sidesteps
    # the Windows CreateProcessW 32767-char command-line limit that the
    # inline --system-prompt flag would blow through.
    orchestrator_prompt_file = write_system_prompt_file("telegram")
    # Voice conversation history — persists across dispatches
    voice_history: list[dict] = []
    MAX_VOICE_HISTORY = 40  # 20 exchanges (user + assistant)

    print("The Count is online. Listening on Telegram...")
    honcho_tag = "honcho=on" if honcho._ready else "honcho=off"
    print(f"  PID: {os.getpid()} | Model: {MODEL_DISPLAY.get(current_model, current_model)} | {honcho_tag} | Lock: {PID_FILE}")

    async with httpx.AsyncClient() as http:

        # Register gateway commands with Telegram's command menu
        try:
            await tg_api(http, token, "setMyCommands", {"commands": [
                {"command": "stop", "description": "Cancel running dispatch"},
                {"command": "model", "description": "View or switch model (opus/sonnet/haiku)"},
                {"command": "status", "description": "Uptime, model, session, cost"},
                {"command": "cost", "description": "Cost breakdown"},
                {"command": "compact", "description": "Clear context, keep memory"},
                {"command": "title", "description": "Name the current session"},
                {"command": "resume", "description": "Restore a named session"},
                {"command": "reset", "description": "Hard reset (clears everything)"},
                {"command": "cron", "description": "List cron tasks and schedule"},
                {"command": "honcho", "description": "Vector memory status"},
                {"command": "ping", "description": "Health check"},
            ]})
            print("  Commands registered with Telegram")
        except Exception as e:
            print(f"  [setMyCommands failed: {e}]")

        async def handle_command(cmd: str) -> bool:
            """Handle gateway commands. Returns True if handled."""
            nonlocal session_id, session_title, current_model, voice_history
            nonlocal session_cost, session_dispatches, busy, dispatch_task

            parts = cmd.strip().split(maxsplit=1)
            c = parts[0].lower().split("@")[0]  # Strip @botname suffix from Telegram commands
            arg = parts[1].strip() if len(parts) > 1 else ""

            if c == "/reset":
                session_id = None
                session_title = None
                session_cost = 0.0
                session_dispatches = 0
                voice_history.clear()
                await tg_send(http, token, chat_id, "Session reset. Fresh start.")
                print("  [session reset by operator]")
                return True

            if c == "/status":
                uptime = datetime.now() - start_time
                h, rem = divmod(int(uptime.total_seconds()), 3600)
                m, s = divmod(rem, 60)
                avg = total_cost / max(total_dispatches, 1)
                model_name = MODEL_DISPLAY.get(current_model, current_model)
                session_label = session_title or (session_id[:8] + "..." if session_id else "none")
                voice_turns = len(voice_history) // 2
                msg = (
                    f"Uptime: {h}h {m}m {s}s\n"
                    f"Model: {model_name}\n"
                    f"Session: {session_label}\n"
                    f"Voice history: {voice_turns} exchanges\n"
                    f"Messages in: {total_messages_in}\n"
                    f"Dispatches: {total_dispatches}\n"
                    f"Total cost: ${total_cost:.4f} (${avg:.4f}/dispatch)"
                )
                await tg_send(http, token, chat_id, msg)
                return True

            if c == "/cost":
                avg = total_cost / max(total_dispatches, 1)
                msg = (
                    f"Total: ${total_cost:.4f}\n"
                    f"Dispatches: {total_dispatches}\n"
                    f"Avg: ${avg:.4f}/dispatch"
                )
                if session_title and session_cost > 0:
                    msg += f"\nCurrent session ({session_title}): ${session_cost:.4f}"
                await tg_send(http, token, chat_id, msg)
                return True

            if c == "/ping":
                await tg_send(http, token, chat_id, "Pong.")
                return True

            if c == "/cron":
                lines = await build_cron_status_report()
                await tg_send(http, token, chat_id, "\n".join(lines))
                return True

            if c == "/model":
                if not arg:
                    model_name = MODEL_DISPLAY.get(current_model, current_model)
                    available = " / ".join(MODEL_ALIASES.keys())
                    await tg_send(http, token, chat_id, f"Current: {model_name}\nAvailable: {available}")
                    return True
                alias = arg.lower()
                if alias in MODEL_ALIASES:
                    current_model = MODEL_ALIASES[alias]
                    await tg_send(http, token, chat_id, f"Model switched to {alias}.")
                    print(f"  [model → {alias} ({current_model})]")
                    # New model means new session (system prompt needs to be re-sent)
                    session_id = None
                    return True
                else:
                    available = " / ".join(MODEL_ALIASES.keys())
                    await tg_send(http, token, chat_id, f"Unknown model. Available: {available}")
                    return True

            if c == "/title":
                if not arg:
                    if session_title:
                        await tg_send(http, token, chat_id, f"Current session: {session_title}")
                    else:
                        await tg_send(http, token, chat_id, "No title set. Usage: /title <name>")
                    return True
                if not session_id:
                    await tg_send(http, token, chat_id, "No active session to title. Send a message first.")
                    return True
                name = arg.replace(" ", "_").lower()
                session_title = name
                save_named_session(name, session_id, current_model, session_cost, session_dispatches)
                await tg_send(http, token, chat_id, f"Session saved as: {name}")
                print(f"  [session titled: {name}]")
                return True

            if c == "/resume":
                if not arg:
                    listing = list_named_sessions()
                    await tg_send(http, token, chat_id, listing)
                    return True
                name = arg.replace(" ", "_").lower()
                sessions = load_sessions()
                if name not in sessions:
                    await tg_send(http, token, chat_id, f"No session named '{name}'.\n{list_named_sessions()}")
                    return True
                info = sessions[name]
                session_id = info["session_id"]
                session_title = name
                session_cost = info.get("cost", 0.0)
                session_dispatches = info.get("messages", 0)
                saved_model = info.get("model", DEFAULT_MODEL)
                if saved_model != current_model:
                    current_model = saved_model
                    print(f"  [model restored → {MODEL_DISPLAY.get(current_model, current_model)}]")
                await tg_send(http, token, chat_id,
                    f"Resumed: {name} ({MODEL_DISPLAY.get(current_model, current_model)})")
                print(f"  [resumed session: {name} → {session_id[:8]}...]")
                return True

            if c == "/compact":
                old_title = session_title
                session_id = None
                session_title = None
                session_cost = 0.0
                session_dispatches = 0
                voice_history.clear()
                msg = "Context compacted. Next message starts a fresh session."
                if old_title:
                    msg += f"\nPrevious session was: {old_title}"
                await tg_send(http, token, chat_id, msg)
                print("  [context compacted]")
                return True

            if c == "/stop":
                stopped = False
                # Cancel the running dispatch — but don't trust its finally to run.
                # If the SDK iterator is wedged on an uninterruptible await (hung
                # subprocess pipe, stuck I/O), the cancelled task may never reach
                # its `busy = False` finally and the gateway clogs forever.
                if dispatch_task and not dispatch_task.done():
                    dispatch_task.cancel()
                    stopped = True
                # Drop pending so nothing dispatches behind the cancelled run.
                if pending:
                    n = len(pending)
                    pending.clear()
                    print(f"  [cleared {n} pending message(s)]")
                    stopped = True
                # Force-release the gateway. If the cancelled task's finally
                # eventually runs it'll just re-set busy=False (no-op).
                # Reset session — the cancelled SDK transport may be poisoned.
                busy = False
                dispatch_task = None
                session_id = None
                if stopped:
                    await tg_send(http, token, chat_id, "Stopped.")
                    print("  [stopped by operator | busy forced False, session reset]")
                else:
                    await tg_send(http, token, chat_id, "Nothing running.")
                return True

            if c == "/honcho":
                await tg_send(http, token, chat_id, honcho.status())
                return True

            return False

        async def dispatch():
            """Dispatch a batch of operator messages.

            The orchestrator does tool work and speaks directly. The gateway sends
            the text from its final text-only turn to Telegram. The `voice` subagent
            remains registered for creative delegation but is optional.
            """
            nonlocal busy, pending, session_id, chat_id, voice_history
            nonlocal total_cost, total_dispatches, session_cost, session_dispatches

            if not pending or busy:
                return

            busy = True
            batch = pending[:]
            pending.clear()
            total_dispatches += 1
            session_dispatches += 1

            # Typing indicator before dispatching
            try:
                await tg_typing(http, token, chat_id)
            except Exception:
                pass

            # Raw user text for Honcho and Voice. Annotate image-only messages
            # so future recall has a hint that something visual happened.
            def _entry_text(text: str, images: list[dict]) -> str:
                if text and images:
                    return f"{text} [image attached]"
                if images:
                    return "[image attached]"
                return text
            raw_user_text = " ".join(_entry_text(t, imgs) for _, _, t, imgs in batch).strip()

            # Collect every image block from the batch. Order is preserved so the
            # operator's mental "this image goes with that caption" still holds.
            batch_images: list[dict] = []
            for _, _, _, imgs in batch:
                batch_images.extend(imgs)

            # Honcho recall — associative memory surfaced for this message
            honcho_ctx = await honcho.recall(raw_user_text)
            if honcho_ctx:
                print(f"  [honcho] recall: {len(honcho_ctx)} chars")

            # Build orchestrator prompt — just the task, no frills
            if len(batch) == 1:
                ts, sender, text, _ = batch[0]
                body = text if text else "[image]"
                orch_prompt = f"[Message from {sender}, {ts}]\n{body}"
            else:
                lines = []
                for ts, sender, text, imgs in batch:
                    body = text if text else "[image]"
                    if text and imgs:
                        body = f"{text} [image attached]"
                    lines.append(f"[{ts} {sender}] {body}")
                orch_prompt = "\n".join(lines)

            # Prepend Honcho associative memory — things from long-term context
            # that resonate with this message. Carry it forward when speaking
            # through the Voice subagent.
            if honcho_ctx:
                orch_prompt = (
                    "[Memories resonating with this moment — from your long-term context]\n"
                    f"{honcho_ctx}\n\n"
                    + orch_prompt
                )

            print(f"  >> dispatching {len(batch)} message(s)")

            # --- Phase 1: Orchestrator (silent worker) ---
            opts = ClaudeAgentOptions(
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
                opts.resume = session_id
            else:
                opts.extra_args = {"system-prompt-file": str(orchestrator_prompt_file)}
                # Inject recent conversation context so the orchestrator isn't blind
                # after a session reset or error
                if voice_history:
                    recent = voice_history[-10:]  # Last 5 exchanges
                    context_lines = []
                    for msg in recent:
                        role = "Operator" if msg["role"] == "user" else "You (prior response)"
                        text = msg["content"]
                        if len(text) > 300:
                            text = text[:300] + "..."
                        context_lines.append(f"[{role}]: {text}")
                    orch_prompt = (
                        "[Recent conversation context — you may have already acted on some of this]\n"
                        + "\n".join(context_lines)
                        + "\n\n[Current message]\n" + orch_prompt
                    )

            # --- Live activity status message ---
            status_resp = await tg_api(http, token, "sendMessage", {
                "chat_id": chat_id, "text": "Working...",
            })
            status_msg_id = status_resp.get("result", {}).get("message_id")
            tool_lines: list[str] = []
            last_edit_time: float = 0
            dispatch_start = asyncio.get_event_loop().time()
            MAX_STATUS_LINES = 20

            async def update_status(header: str = "Working..."):
                """Edit the status message with current tool activity."""
                nonlocal last_edit_time
                if not status_msg_id:
                    return
                now = asyncio.get_event_loop().time()
                if now - last_edit_time < 1.0:
                    return
                shown = tool_lines[-MAX_STATUS_LINES:]
                text = header + "\n" + "\n".join(shown)
                if len(tool_lines) > MAX_STATUS_LINES:
                    text = f"{header} ({len(tool_lines)} ops)\n" + "\n".join(shown)
                await tg_edit(http, token, chat_id, status_msg_id, text)
                last_edit_time = now

            text_blocks: list[str] = []     # Every orchestrator TextBlock (for history/Honcho persistence)
            voice_notes: list[str] = []     # Voice subagent TextBlocks — internal, never streamed
            sent_text_count = 0             # How many TextBlocks we've already streamed to TG
            orch_cost = 0.0
            orch_turns = 0

            async def stream_text(text: str):
                """Send one orchestrator TextBlock to Telegram immediately."""
                nonlocal sent_text_count
                cleaned = re.sub(r"<antThinking>.*?</antThinking>\s*", "", text, flags=re.DOTALL).strip()
                if not cleaned:
                    return
                try:
                    await tg_send(http, token, chat_id, cleaned)
                    sent_text_count += 1
                except Exception as e:
                    print(f"  [stream_text send failed: {e}]", flush=True)

            # If the operator sent images, switch to a multimodal content list:
            # text first, then image blocks. The Claude Code CLI passes these
            # straight through to the API as a vision request.
            if batch_images:
                orch_content = [{"type": "text", "text": orch_prompt}, *batch_images]
            else:
                orch_content = orch_prompt

            async def _run_query():
                """Inner SDK loop, wrapped so we can put a deadline on it."""
                nonlocal session_id, total_cost, orch_cost, session_cost, orch_turns
                async for message in query(prompt=_stream_prompt(orch_content), options=opts):
                    if not isinstance(message, (AssistantMessage, ResultMessage)):
                        # Skip unknown event types (e.g., rate_limit_event)
                        print(f"  [sdk event: {type(message).__name__}]")
                        continue
                    if isinstance(message, AssistantMessage):
                        # Subagent messages have parent_tool_use_id set; orchestrator's are None.
                        # Subagent text is internal — we don't stream it to the operator. The
                        # orchestrator decides what (if anything) to relay from a subagent's work.
                        is_subagent = message.parent_tool_use_id is not None
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                preview = block.text[:200]
                                if len(block.text) > 200:
                                    preview += "..."
                                if is_subagent:
                                    voice_notes.append(block.text)
                                    print(f"  [voice-sub]: {preview}")
                                else:
                                    text_blocks.append(block.text)
                                    print(f"  [orch]: {preview}")
                                    await stream_text(block.text)
                            elif isinstance(block, ToolUseBlock):
                                print(f"  [tool: {block.name}]")
                                tool_lines.append(format_tool_line(block))
                                await update_status()
                        try:
                            await tg_typing(http, token, chat_id)
                        except Exception:
                            pass
                    elif isinstance(message, ResultMessage):
                        session_id = message.session_id
                        if message.total_cost_usd:
                            orch_cost = message.total_cost_usd
                            total_cost += orch_cost
                            session_cost += orch_cost
                        orch_turns = message.num_turns or 0
                        model_tag = MODEL_DISPLAY.get(current_model, "?")
                        print(f"  [orch done | {model_tag} | {orch_turns} turns | ${orch_cost:.4f}]")
                        if message.is_error:
                            print("  [session error — starting fresh next message]")
                            session_id = None
                        elif session_title:
                            save_named_session(session_title, session_id, current_model, session_cost, session_dispatches)

            timed_out = False
            errored = False
            try:
                # Hard deadline on the whole inner SDK iteration if configured.
                # When DISPATCH_DEADLINE_SECONDS is 0 (default), no deadline at all —
                # /stop is the operator's escape hatch.
                if DISPATCH_DEADLINE_SECONDS > 0:
                    await asyncio.wait_for(_run_query(), timeout=DISPATCH_DEADLINE_SECONDS)
                else:
                    await _run_query()
            except asyncio.TimeoutError:
                timed_out = True
                print(f"  [DISPATCH DEADLINE EXCEEDED ({DISPATCH_DEADLINE_SECONDS}s)]", flush=True)
                try:
                    await tg_send(http, token, chat_id,
                        f"[dispatch deadline exceeded ({DISPATCH_DEADLINE_SECONDS}s) — aborted. "
                        f"Session reset. Send your next message fresh.]")
                except Exception:
                    pass
                # Hard reset: any state from a hung dispatch is poison.
                session_id = None
            except asyncio.CancelledError:
                print("  [dispatch cancelled — /stop]")
                if status_msg_id:
                    last_edit_time = 0
                    elapsed = asyncio.get_event_loop().time() - dispatch_start
                    cancel_text = f"[stopped | {elapsed:.0f}s]"
                    if tool_lines:
                        cancel_text += "\n" + "\n".join(tool_lines[-MAX_STATUS_LINES:])
                    try:
                        await tg_edit(http, token, chat_id, status_msg_id, cancel_text)
                    except Exception:
                        pass
                session_id = None
            except BaseExceptionGroup as eg:
                errored = True
                import traceback
                err_str = "; ".join(f"{type(e).__name__}: {e}" for e in eg.exceptions)
                print(f"  [dispatch error (group): {err_str}]", flush=True)
                for exc in eg.exceptions:
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
                # If we collected text but never streamed any (rare — error before the
                # first send completed), recover the last block.
                if text_blocks and sent_text_count == 0:
                    last_text = re.sub(r"<antThinking>.*?</antThinking>\s*", "",
                                       text_blocks[-1], flags=re.DOTALL).strip()
                    if last_text:
                        try:
                            await tg_send(http, token, chat_id, last_text)
                            sent_text_count += 1
                        except Exception:
                            pass
                try:
                    await tg_send(http, token, chat_id, f"[error: {err_str[:300]}]")
                except Exception:
                    pass
                session_id = None
            except Exception as e:
                errored = True
                import traceback
                err_str = f"{type(e).__name__}: {e}"
                print(f"  [dispatch error: {err_str}]", flush=True)
                traceback.print_exc()
                if text_blocks and sent_text_count == 0:
                    last_text = re.sub(r"<antThinking>.*?</antThinking>\s*", "",
                                       text_blocks[-1], flags=re.DOTALL).strip()
                    if last_text:
                        try:
                            await tg_send(http, token, chat_id, last_text)
                            sent_text_count += 1
                        except Exception:
                            pass
                try:
                    await tg_send(http, token, chat_id, f"[error: {err_str[:300]}]")
                except Exception:
                    pass
                session_id = None
            finally:
                # Fallback message if the run terminated normally without any narration.
                if not timed_out and not errored and sent_text_count == 0:
                    fallback = "[run ended with no text output]"
                    if tool_lines:
                        fallback += f" — last tool: {tool_lines[-1]}"
                    try:
                        await tg_send(http, token, chat_id, fallback)
                    except Exception:
                        pass

                # Persist concatenated reply for history + Honcho associative memory.
                full_reply = "\n\n".join(
                    re.sub(r"<antThinking>.*?</antThinking>\s*", "", t, flags=re.DOTALL).strip()
                    for t in text_blocks
                ).strip()
                if full_reply:
                    voice_history.append({"role": "user", "content": raw_user_text})
                    voice_history.append({"role": "assistant", "content": full_reply})
                    if len(voice_history) > MAX_VOICE_HISTORY:
                        voice_history[:] = voice_history[-MAX_VOICE_HISTORY:]
                    asyncio.create_task(honcho.store(raw_user_text, full_reply))

                # Final status-message edit — operator sees the run summary.
                if status_msg_id:
                    elapsed = asyncio.get_event_loop().time() - dispatch_start
                    model_tag = MODEL_DISPLAY.get(current_model, "?")
                    if timed_out:
                        header = f"[TIMEOUT | {elapsed:.0f}s]"
                    elif errored:
                        header = f"[ERROR | {elapsed:.0f}s]"
                    else:
                        header = f"[{model_tag} | {orch_turns} turns | ${orch_cost:.2f} | {elapsed:.0f}s]"
                    if tool_lines:
                        shown = tool_lines[-MAX_STATUS_LINES:]
                        final = header + "\n" + "\n".join(shown)
                        if len(tool_lines) > MAX_STATUS_LINES:
                            final = f"{header} ({len(tool_lines)} ops)\n" + "\n".join(shown)
                    else:
                        final = header
                    last_edit_time = 0
                    try:
                        await tg_edit(http, token, chat_id, status_msg_id, final)
                    except Exception:
                        pass

                # GUARANTEE busy=False on every code path. If a BaseExceptionGroup ever
                # bypasses our handlers, busy stuck-True clogs the gateway forever.
                busy = False
                print(f"  [dispatch end | busy=False | session={session_id[:8] if session_id else 'reset'}]",
                      flush=True)

        # --- Poll loop ---
        # Dispatch runs as asyncio.Task so the poll loop stays responsive.
        # This lets /stop arrive and cancel a running dispatch mid-flight.
        last_batch_time: float = 0

        try:
            while True:
                try:
                    # Short poll when messages are batching, long poll when idle
                    poll_timeout = 2 if (pending and not busy) else 30
                    resp = await http.get(
                        f"https://api.telegram.org/bot{token}/getUpdates",
                        params={"offset": offset, "timeout": poll_timeout},
                        timeout=poll_timeout + 5,
                    )
                    data = resp.json()

                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        msg = update.get("message", {})
                        # Photos use `caption` instead of `text`. Treat both as text.
                        text = msg.get("text") or msg.get("caption") or ""
                        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                        sender = msg.get("from", {}).get("first_name", "Unknown")
                        user_id = str(msg.get("from", {}).get("id", ""))

                        has_photo = bool(msg.get("photo"))
                        doc = msg.get("document") or {}
                        has_image_doc = (doc.get("mime_type") or "").startswith("image/")
                        has_image = has_photo or has_image_doc

                        if not msg_chat_id:
                            continue
                        if not text and not has_image:
                            # Stickers, voice, video, etc. — nothing we can route.
                            continue
                        if allowed_users and user_id not in allowed_users:
                            print(f"  [ignored: {sender} ({user_id})]")
                            continue

                        chat_id = msg_chat_id
                        total_messages_in += 1

                        # Persist chat_id for the tg.py helper script
                        try:
                            CHAT_ID_FILE.write_text(chat_id)
                        except Exception:
                            pass

                        ts = datetime.now().strftime("%H:%M:%S")

                        images: list[dict] = []
                        if has_image:
                            print(f"  [{ts} {sender} sent image, downloading...]")
                            images = await tg_extract_images(http, token, msg)
                            print(f"  [image: attached {len(images)} block(s)]")

                        log_text = text if text else ("[image]" if images else "")
                        print(f"  [{ts} {sender}]: {log_text}")

                        # Gateway commands — handled without invoking The Count.
                        # Only check on text-only messages (commands never come with images).
                        if text and not images and text.startswith("/"):
                            if await handle_command(text):
                                continue

                        # Acknowledge messages that arrive while a dispatch is running.
                        # Idle-arriving messages don't need an ack — they get a "Working..."
                        # status almost immediately. Busy-arriving ones look ignored otherwise.
                        if busy:
                            try:
                                queue_pos = len(pending) + 1
                                await tg_send(http, token, chat_id,
                                    f"[queued — pos {queue_pos}, will dispatch when current task ends. /stop to cancel current.]")
                            except Exception:
                                pass

                        pending.append((ts, sender, text, images))
                        last_batch_time = asyncio.get_event_loop().time()

                    # Dispatch as a background task if batch window has passed
                    if pending and not busy:
                        now = asyncio.get_event_loop().time()
                        if now - last_batch_time >= BATCH_WINDOW:
                            print(f"  [dispatch trigger: {len(pending)} pending, busy={busy}]")
                            dispatch_task = asyncio.create_task(dispatch())
                    elif pending:
                        print(f"  [waiting: {len(pending)} pending, busy={busy}]")

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"  [poll error: {e}]")
                    await asyncio.sleep(5)

        except KeyboardInterrupt:
            print("\nThe Count withdraws.")
            if dispatch_task and not dispatch_task.done():
                dispatch_task.cancel()
        finally:
            release_pidlock()
            if total_dispatches:
                print(f"  [session total: ${total_cost:.4f} across {total_dispatches} dispatches]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("The Count — Agent SDK Harness")
        print()
        print("Usage:")
        print('  python count_agent.py chat "Your message here"')
        print("  python count_agent.py cron <task_name>")
        print("  python count_agent.py telegram")
        print()
        print("Telegram gateway commands (sent in chat):")
        print("  /stop                     — interrupt, cancel running dispatch")
        print("  /model [opus|sonnet|haiku] — view or switch model")
        print("  /title <name>             — save current session")
        print("  /resume [name]            — resume saved session (no arg = list)")
        print("  /compact                  — squash context, fresh session")
        print("  /reset                    — hard reset (clears everything)")
        print("  /cron                     — list scheduled tasks")
        print("  /status                   — uptime, model, session, cost")
        print("  /cost                     — cost breakdown")
        print("  /honcho                   — Honcho vector memory status")
        print("  /ping                     — health check")
        print()
        print(f"Home: {COUNT_HOME}")
        sp = COUNT_HOME / "SYSTEM_PROMPT.md"
        env = COUNT_HOME / ".env"
        print(f"System prompt: {sp} ({'exists' if sp.exists() else 'MISSING'})")
        print(f"Env: {env} ({'exists' if env.exists() else 'MISSING'})")
        sys.exit(0)

    command = sys.argv[1]

    if command == "chat":
        prompt = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "What's on your mind?"
        asyncio.run(run_chat(prompt))
    elif command == "cron":
        if len(sys.argv) < 3:
            print("Usage: python count_agent.py cron <task_name>")
            sys.exit(1)
        asyncio.run(run_cron(sys.argv[2]))
    elif command == "telegram":
        asyncio.run(run_telegram())
    elif command == "cron_status":
        lines = asyncio.run(build_cron_status_report())
        print("\n".join(lines))
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
