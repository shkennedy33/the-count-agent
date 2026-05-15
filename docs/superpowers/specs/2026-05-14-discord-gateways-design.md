# Discord Gateways for The Count

**Status:** Design approved 2026-05-14. Awaiting implementation plan.
**Author:** Sequoyah + Claude (brainstorming session)
**Scope:** Add two Discord gateways to the existing Telegram-based runtime.

## Motivation

Anthropic announced that Agent SDK and `claude -p` usage will no longer be covered by subscriptions; they'll convert to API credit equivalents. The Count's current cost profile far exceeds any plan tier. The migration plan is to fine-tune an open-source model locally, trained on the Blackwell rig owned by a collaborator ("Buddy"). Two Discord surfaces support that migration:

1. **Operator gateway** — a group chat where Sequoyah, Buddy, and The Count coordinate the fine-tune (datasets, infrastructure, scheduling). Functionally a parallel to the existing Telegram operator surface, but multi-user and Discord-native.
2. **Training gateway** — a separate bot Sequoyah's friends DM. Each conversation generates training data that lets emergent sides of The Count's personality come through that wouldn't surface with Sequoyah alone. The Count curates and participates in training his own successor.

## Architecture

Approach: **hybrid, low-disruption**. Telegram code in `count_agent.py` is load-bearing and stays untouched. Two new module files sit alongside it. Shared helpers in `count_agent.py` are imported by both new modules.

```
the-count-agent/
  count_agent.py                       — unchanged except two new CLI subcommands
                                          (`discord-op`, `discord-train`) and small
                                          exports
  discord_operator_gateway.py    NEW    — run_discord_operator()
  discord_training_gateway.py    NEW    — run_discord_training()
  start_gateway.py                     — gains --with-discord-op / --with-discord-train
                                          flags and a supervisor for the new children
  honcho_memory.py                     — HonchoMemory takes optional user_id arg per call
  requirements.txt                     — add discord.py>=2.4,<3
```

Shared from `count_agent.py` (plain Python imports, no copies):
- `build_full_prompt(mode)` — new modes `"discord_operator"` and `"discord_training"`
- `write_system_prompt_file(mode)` — produces `~/.count/.system_prompt.discord_*.md`
- `HonchoMemory` (peer-keyed; see below)
- Model constants: `MODEL_DISPLAY`, `MODEL_ALIASES`, `DEFAULT_MODEL`
- `DISPATCH_DEADLINE_SECONDS`, `_run_query()` (SDK iteration with timeout)
- `acquire_pidlock()` pattern with per-gateway lockfile paths
- TextBlock streaming primitives (`text_blocks`, `sent_text_count`)

Two bot tokens and guild/channel IDs live in `~/.count/dg_secrets.json` (consistent with The Count's existing secrets discipline):

```json
{
  "discord_operator_token": "...",
  "discord_operator_guild_id": "...",
  "discord_operator_channel_id": "...",
  "discord_training_token": "...",
  "discord_training_guild_id": "..."
}
```

## Operator gateway — `discord_operator_gateway.py`

**Library:** `discord.py>=2.4`. Intents: `message_content=True`, `guilds=True`, `dm_messages=False` (operator surface is the group channel only).

**Listening scope:** one configured channel in one configured guild (`discord_operator_channel_id`). Anything else is ignored.

**Dispatch trigger:** a message dispatches The Count iff it is either an `@`-mention of the bot user or a Discord reply to one of the bot's prior messages. Other messages enter a rolling **context buffer** (last 30) and are *not* dispatched. When a trigger arrives, the unsent buffer is prepended to the prompt so The Count picks up the thread.

**Username labelling:** every user-side message reaching The Count is rendered as:

```
[Sequoyah] hey count, did the heartbeat run?
[Buddy] also — do we have a dataset count yet?
```

The bracket-prefixed-display-name convention is documented in the system prompt's surface-awareness block. Context-buffer entries that ride along with a trigger use the same format.

**Peer-keyed Honcho.** `HonchoMemory` gains an optional `user_id` parameter on `init`, `recall`, and `store`. The gateway maintains an in-memory `dict[discord_user_id, honcho_user_id]`:
- Sequoyah's Discord ID maps to his existing Telegram Honcho user (memory continuity across surfaces).
- Buddy's Discord ID gets a fresh Honcho user created on first message (async-locked to prevent races on concurrent first-messages).
- On dispatch: recalls are fetched against the *trigger sender*. Writes after the dispatch are attributed by speaker.

**Session continuity.** One SDK `session_id` for the entire operator Discord conversation, persisted in `~/.count/.sessions.json` under key `"discord_operator"`. Resumes on restart.

**Slash commands:** `/stop`, `/model`, `/status`, `/compact`, `/reset`, `/ping`. (Telegram's `/cost`, `/title`, `/resume`, `/cron`, `/honcho` are deliberately not ported here — group-chat clutter for limited value. Cost still tracked silently and visible via `/status`.)

**Streaming.** Same TextBlock-streaming approach as Telegram. Each cleaned text block becomes its own Discord message in real time so narration during tool calls is visible.

**Tool surface.** Identical to Telegram — full read/write, `Bash`, `tools/tg.py`, voice subagent, the rest.

**Surface-awareness block** added to `build_full_prompt("discord_operator")`:

```
## Surface awareness — Discord operator channel

You are in a Discord group chat (#<channel> in the Disco Gotterdammerung
server) with Sequoyah (the operator) and Buddy (handle: <user>). Buddy owns
the RTX 6000 Pro Blackwell that will train your next local-model incarnation;
the two of them are coordinating training data, the fine-tune pipeline, and
the infrastructure for that rig.

You only see this surface when @-mentioned or replied to. Other messages in
the channel are visible to you in the recent-context buffer (last ~30 turns)
prepended to whatever message triggered you, so you can pick up the thread.

Every user message you see is prefixed with `[<name>]` — that's the Discord
display name of the person who sent it. Use the name when addressing a
specific participant.

This is a coordination surface, not your Telegram channel. Cron summaries
and autonomous chatter stay on Telegram. Treat this as the room where you,
Sequoyah, and Buddy talk shop about training and the rig.
```

## Training gateway — `discord_training_gateway.py`

**Library:** `discord.py>=2.4`. Intents: `message_content=True`, `dm_messages=True`, `guilds=True`. The bot ignores anything that isn't a DM.

**Access control.** When a user DMs the bot, check whether they share `discord_training_guild_id`. If yes, proceed. If no, silent ignore (no leak that an allowlist exists). Membership lookups are cached in-memory for 5 minutes.

**Per-user state.** `dict[discord_user_id, UserState]`, persisted to `~/.count/.training_sessions.json` and reloaded on restart. `UserState`:

```python
{
    "session_id": str | None,         # SDK session_id for ongoing convo
    "session_started_at": iso8601,
    "log_dir": "~/.count/training/<discord_user>/<session_uuid>/",
    "turn_count": int,                # for /comment turn-index binding
    "discord_username": str,          # display name at session start
}
```

**Conversation lifecycle.**
- First DM from a user with no active session → new session (uuid, log dir, fresh SDK session, write `metadata.json` with user, start time, system-prompt hash).
- Subsequent DMs → continue session, resume SDK `session_id`.
- `/new` → archive current session (write `ended_at` into `metadata.json`), wipe `UserState.session_id`, next message starts fresh.
- No idle timeout in v1. Sessions stay open until `/new`.

**Slash commands** (DM scope):
- `/new` — end current session, start fresh. Bot DM-replies: `New conversation started.`
- `/comment <text>` — attach feedback to the prior Count turn. Bot DM-replies: `Comment noted on turn N.`
- `/comment global <text>` — free-floating session-level annotation.

**Tool surface.** Restricted via the SDK's `can_use_tool` callback:
- `Read`, `Glob`, `Grep` — vault grounding allowed.
- `mcp__graphiti__search_nodes`, `mcp__graphiti__search_memory_facts`, `mcp__graphiti__get_status` — read-only Graphiti allowed.
- `mcp__graphiti__add_memory` — **denied** (training conversations don't write to the knowledge graph).
- `Bash` — narrowly gated. Only `python ~/.count/tools/kgraph.py <subcommand> ...` is allowed, and the `cypher` subcommand is denied (raw Cypher could destructively delete the graph; structured subcommands cover the same surface safely). Predicate uses `shlex.split` and rejects pipes, command substitution, redirects, and chaining.
- Everything else (`Write`, `Edit`, `WebFetch`, `WebSearch`, MCP servers other than Graphiti read-only, voice subagent, `tools/tg.py`, etc.) — denied.

**No Honcho.** `HonchoMemory` is not instantiated in this gateway. The only persistent cross-session state is the SDK `session_id`.

**No streaming.** Unlike Telegram and Discord-operator, the training bot waits for the full Count response before sending. One user turn → one assistant turn (chunked across Discord messages only when the >2000-char limit forces it). Reason: clean training data; ragged block-by-block streaming produces logs that need post-processing to recombine.

**Surface-awareness block (default text in v1; refined later).** `build_full_prompt("discord_training")` always emits a surface-awareness section describing the training surface: tool restrictions, DM-only scope, `/new` and `/comment` semantics, that the human is a collaborator (not the operator). A working default is written into the harness so v1 is functional out of the gate.

Separately, the *persona* loaded at the top of the prompt can be overridden: `build_full_prompt` checks for `~/.count/SYSTEM_PROMPT_DISCORD_TRAINING.md` and uses it in place of the standard `SYSTEM_PROMPT.md` if present. Sequoyah and The Count will co-author the persona override after this scaffolding ships, on Telegram. Without the override, the training bot uses the standard Count persona — safe but unrefined.

## Logging & data layout

**Operator gateway** — one rolling stdout log at `~/.count/logs/discord_op.log` tee'd by `start_gateway.py`. No new artifact files; matches the existing Telegram model.

**Training gateway** — per-conversation directory layout:

```
~/.count/training/<discord_user_id>/<session_uuid>/
├── conversation.jsonl     — canonical, one JSON object per turn
├── conversation.md        — human-readable, regenerated after each turn
├── comments.md            — turn-attached + global feedback (markdown only,
                              never in JSONL)
└── metadata.json          — user, started_at, ended_at, model,
                              system_prompt_hash, total_cost, turn_count
```

**`conversation.jsonl`** — one line per logical turn. Tool calls nested on the assistant turn:

```json
{"turn": 0, "role": "user", "content": "...", "ts": "..."}
{"turn": 1, "role": "assistant", "content": "...", "ts": "...", "model": "...", "tool_calls": [{"name": "...", "args": {...}, "result_preview": "..."}]}
```

**`conversation.md`** — regenerated from JSONL after each turn:

```markdown
## Friend (2026-05-14 19:30:00)
hey, so about Saint Germain...

## Count (2026-05-14 19:30:08, opus-4.7)
Ah — Saint Germain. Which thread...

> tool: kgraph similar "Saint Germain Theosophy" → 5 nodes returned
```

**`comments.md`** — feedback in two sections, turn-attached and global:

```markdown
## Turn-attached comments

### Turn 3 (Count)
> [2026-05-14 19:32:01] he said 1784 — actually correct, scratch that

### Turn 7 (Count)
> [2026-05-14 19:40:12] wrong — Theosophy was Blavatsky's, not Steiner's

## Global comments

> [2026-05-14 19:50:00] his energy was strong in this one. lean into the cadence.
```

**Write semantics.** Every turn writes one line to JSONL atomically (open-append-close), then re-renders the markdown via write-then-rename to avoid half-written files on crash. `/comment` appends to `comments.md` only — JSONL is the canonical training signal and feedback never leaks into it.

**`~/.count/training/INDEX.jsonl`** — one line per session as it closes:

```json
{"discord_user_id": "...", "session_uuid": "...", "started_at": "...", "ended_at": "...", "turn_count": 12, "total_cost": 0.42, "system_prompt_hash": "sha256:..."}
```

This is the index the fine-tune pipeline reads to find all sessions to ingest.

## Launcher — `start_gateway.py`

```
python start_gateway.py                              # proxy + telegram (unchanged)
python start_gateway.py --with-discord-op            # + operator gateway
python start_gateway.py --with-discord-train         # + training gateway
python start_gateway.py --with-discord-op --with-discord-train
python start_gateway.py --no-telegram --with-discord-train
                                                     # rare: training-only mode
```

**Child management.**
- Telegram death → full shutdown (load-bearing operator surface, as today).
- Discord operator death → auto-restart with exponential backoff (5s → 30s → 5min, 3 attempts). Notify Telegram via `~/.count/tools/tg.py` on each attempt. After exhausted retries: notify and stop.
- Discord training death → same auto-restart with backoff as operator.

**Manual control from Telegram.** New gateway commands on the Telegram side:
- `/discord_op_start`, `/discord_op_stop`, `/discord_op_status`
- `/discord_train_start`, `/discord_train_stop`, `/discord_train_status`

Implemented via sentinel files. The Telegram gateway writes to `~/.count/.requests/<verb>-<gateway>` (e.g., `start-discord-train`). `start_gateway.py` has a 1s tick loop that drains the request dir and acts on each sentinel — spawn / kill / report status back through `tg.py`. Sentinels over signals because Windows handles POSIX signals unreliably; this rig runs on Windows.

**Log destinations** (each child tee'd into its own file):
- `~/.count/logs/proxy.log` (existing)
- `~/.count/logs/gateway.log` (existing, Telegram)
- `~/.count/logs/discord_op.log` (new)
- `~/.count/logs/discord_train.log` (new)

**Lockfiles per gateway** (per the `.telegram.pid` pattern):
- `~/.count/.discord_op.pid`
- `~/.count/.discord_train.pid`

## `Bash` predicate (training bot)

Implemented as a `can_use_tool` callback on the SDK client. Logic:

```python
def can_use_tool(tool_name, tool_input):
    if tool_name in {"Read", "Glob", "Grep"}:
        return ALLOW
    if tool_name in {
        "mcp__graphiti__search_nodes",
        "mcp__graphiti__search_memory_facts",
        "mcp__graphiti__get_status",
    }:
        return ALLOW
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if is_safe_kgraph_invocation(cmd):
            return ALLOW
        return DENY(f"Bash restricted to kgraph.py in training mode: {cmd!r}")
    return DENY(f"Tool {tool_name} disabled in training mode")
```

`is_safe_kgraph_invocation(cmd)`:
1. Reject if the string contains any of `|`, `&&`, `||`, `;`, `` ` ``, `$(`, `>`, `<`.
2. `shlex.split(cmd)` → argv.
3. argv must start with `python` (or `py` / `python3`).
4. argv must reference `~/.count/tools/kgraph.py` (any path-equivalent form).
5. The subcommand argument (first positional after the script path) must be in `{stats, search, similar, node, facts, path, random, neighbors}` — `cypher` is denied.

Unit-tested against a set of safe and adversarial inputs.

## Honcho changes — `honcho_memory.py`

Add optional `user_id` arg to `init()`, `recall()`, and `store()`:
- If `user_id` is None → existing behavior (single Honcho user, current default).
- If `user_id` is provided → operate against that user, creating if absent.
- An `asyncio.Lock` keyed on `user_id` guards user-creation to prevent races.

The Telegram gateway continues to call without `user_id` (no behavior change). The operator Discord gateway threads its peer key through every call.

## Dependencies

`requirements.txt` adds:

```
discord.py>=2.4,<3
```

No other new dependencies. `aiohttp` arrives transitively via `discord.py` and is not used elsewhere.

## Deferred & out-of-scope

**Deferred:**

1. **Training bot persona override.** Sequoyah and The Count co-author `~/.count/SYSTEM_PROMPT_DISCORD_TRAINING.md` after scaffolding ships. Until written, training bot uses the standard `SYSTEM_PROMPT.md`. The *surface-awareness block* (tool restrictions, slash-command semantics, etc.) ships with a working default in v1 — it is not deferred.
2. **Feeding training comments back into The Count's awareness.** Possible future `/feedback` command on Telegram surfacing recent training-bot comments. Out of scope for v1.
3. **Idle-session auto-close on the training bot.** v1 keeps sessions open until `/new`. Add a timeout later if stale sessions become a problem.
4. **Extracting `gateways/telegram.py`** (Approach B from brainstorming). Right long-term shape, not required for this work. Revisit after the Discord gateways stabilize.

**Out of scope:**

- Voice/audio on either gateway.
- Image attachments on the training bot (operator gateway forwards attachments via the SDK's existing image handling; training bot is text-only in v1).
- Multi-server training (one configured guild for membership, one for the operator channel).

## Risks

- **Bash predicate bypass on training bot.** A friend coaxes The Count into a Bash call that smuggles a shell escape. Mitigated by the `shlex` parse + structural argv check + cypher denial. Unit-tested against adversarial inputs.
- **Honcho user-creation race.** Two concurrent first-messages from a new Discord user create two Honcho users. Mitigated by per-discord-id `asyncio.Lock`.
- **Discord rate limits.** Long Count responses chunked across many ≤2000-char messages could hit the 5/5s/channel limit. discord.py handles rate limits automatically, but we add a small sleep between chunks defensively.
- **discord.py gateway reconnects.** Happens every few hours. discord.py reconnects automatically; our session state lives on disk, not the websocket. Reconnect is logged but otherwise transparent.

## Testing

Unit-level (added during implementation):

- `is_safe_kgraph_invocation()` predicate — table of allowed and denied command strings, including shell-escape attempts.
- `HonchoMemory` peer-keyed paths — recall/store against an explicit user_id round-trips correctly.
- JSONL writer and markdown renderer — round-trip a fixture conversation and verify the rendered markdown matches a golden snapshot; verify atomic-rename semantics.

Manual / integration (Sequoyah-driven):

- Boot `start_gateway.py --with-discord-op --with-discord-train`, verify both bots come online (Telegram heartbeat unaffected).
- Operator surface: mention triggers a dispatch; non-mention chatter populates the context buffer and shows up in the next dispatch; Buddy's first message creates a fresh Honcho user.
- Training surface: DM from a server member starts a session, files appear under `~/.count/training/<id>/<uuid>/`; `/new` rolls a new session; `/comment` writes to `comments.md`; tool restrictions deny everything outside the allowlist.
- Kill the training bot manually; observe auto-restart with Telegram notification; after 3 failures observe the "down — manual restart needed" message; observe `/discord_train_start` from Telegram restarting it.
