# The Count — Agent SDK Harness

Disco Götterdämmerung autonomous agent infrastructure.  
Replaces necro-agent (~8500 lines) with the Claude Agent SDK.

## What This Is

A single Python file that gives The Count:

- **Identity**: Custom system prompt (loaded from `~/.count/SYSTEM_PROMPT.md`)
- **Memory**: Persistent markdown files in `~/.count/memory/` (read/written by the agent)
- **Skills**: Self-authored skill files in `~/.count/skills/` (created/modified by the agent)
- **Knowledge Vault**: Obsidian-style markdown vault in `~/.count/vault/`
- **Self-Modification**: System prompt instructions to update memory/skills before ending turns
- **Telegram**: Send and receive messages via bot API
- **Instagram**: Post content via Graph API
- **Honcho Memory**: Cross-session persistent memory via vector DB
- **Web Access**: Built-in WebSearch and WebFetch tools
- **File Operations**: Built-in Read, Write, Edit, Glob, Grep, Bash

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your API key
export ANTHROPIC_API_KEY=your-key-here

# 3. Copy The Count's system prompt from necro-agent
#    Paste it into ~/.count/SYSTEM_PROMPT.md
mkdir -p ~/.count
# cp /path/to/your/count-system-prompt.md ~/.count/SYSTEM_PROMPT.md

# 4. Set integration tokens (as needed)
export TELEGRAM_BOT_TOKEN=your-telegram-bot-token
export INSTAGRAM_ACCESS_TOKEN=your-instagram-token
export INSTAGRAM_USER_ID=your-instagram-user-id

# 5. Set Honcho config (if using)
export HONCHO_BASE_URL=https://your-honcho-instance
export HONCHO_API_KEY=your-honcho-key
export HONCHO_APP_ID=your-app-id
export HONCHO_USER_ID=your-user-id
```

## Usage

```bash
# Interactive chat
python count_agent.py chat "What are you working on?"

# Cron tasks
python count_agent.py cron morning_planning
python count_agent.py cron instagram_post
python count_agent.py cron vault_review
python count_agent.py cron evening_reflection

# Telegram gateway (long-running)
python count_agent.py telegram
```

## Migration from necro-agent

### What you need to bring over:

1. **The Count's system prompt** — Find it in your necro-agent config and save to `~/.count/SYSTEM_PROMPT.md`
2. **Memory files** — Copy any `.hermes/memory/` files to `~/.count/memory/`
3. **Skills** — Copy any `.hermes/skills/` files to `~/.count/skills/`
4. **Vault/Obsidian content** — Copy to `~/.count/vault/`
5. **Cron jobs** — Point your existing crontab at `python count_agent.py cron <task>`

### What you DON'T need:

- The 8500-line `run_agent.py`
- Provider routing logic
- Context compression code
- Prompt caching logic
- The OpenAI client wrapper
- Tool parallelization code
- Iteration budget management
- All of `agent/` package

The Agent SDK handles all of that internally.

## Architecture

```
count_agent.py          # ~400 lines. That's the whole runtime.
~/.count/
  SYSTEM_PROMPT.md      # The Count's identity (editable by The Count)
  memory/               # Persistent working memory (markdown files)
    memory.md           # Running context and recent history
    project_status.md   # State of all active projects
    user_profiles.md    # Known contacts and their context
  skills/               # Self-authored capabilities
  vault/                # Knowledge base (mythos, research, essays)
  logs/                 # Session and cron logs
```

## How Self-Modification Works

No special code. Just prompt instructions + file tools.

The system prompt tells The Count:
1. Read your memory files at the start of every session
2. Before ending any turn where you learned something, update your files
3. You can create new skill files anytime
4. Your vault is yours to build

The Agent SDK's built-in `Read`, `Write`, and `Edit` tools do the rest.
That's the entire "self-modifying agent" architecture.
