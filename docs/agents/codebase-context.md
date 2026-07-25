# Codebase Context

This document is the authoritative reference for all agents. Read it before starting any task.

---

## Project Summary

A remote control system that lets the user send instructions from a smartphone via Slack to Claude Code running on a home PC. Claude Code processes the instruction and writes the response to a temp file; the Bot polls for the file and posts the content back to the Slack thread.

This project is a migration of the Telegram bot (`02_Telegram-claude-bot`) to Slack Socket Mode, with the additional goal of enabling per-channel parallel execution.

---

## Directory Structure

```
slack-claude-bot/
├── CLAUDE.md                  # Project rules, agent team definition, migration design
├── README.md
├── requirements.txt           # Python deps: slack-bolt, python-dotenv
├── start.sh                   # Startup script (creates tmux session + windows + starts bot)
├── app.py                     # Current: single-file Slack bot (Claude API). To be replaced by src/
├── src/                       # Planned modular structure (mirrors telegram-claude-bot/src/)
│   ├── bot.py                 # Main Slack handler: event listeners, Socket Mode
│   ├── config.py              # Channel → tmux mapping, paths, constants
│   ├── channel_state.py       # ChannelState dataclass + per-channel state management
│   ├── tmux_handler.py        # tmux operations (reusable from telegram-claude-bot)
│   └── file_handler.py        # Slack file upload: send_long_text, files_upload_v2
├── .env                       # Secrets (never committed)
├── .env.example               # Template for required env vars
├── docs/
│   ├── feature-list.md        # One-line summary of every feature branch
│   ├── issues.md              # Active bugs (B-xx), improvements (F-xx)
│   ├── issues-resolved.md     # Closed issues with resolution notes
│   ├── agents/                # Agent role templates (this directory)
│   │   ├── codebase-context.md  ← this file
│   │   ├── planner.md
│   │   ├── design-reviewer.md
│   │   ├── coder.md
│   │   ├── code-reviewer.md
│   │   ├── test-designer.md
│   │   ├── test-executor.md
│   │   └── retrospective-analyst.md
│   ├── featureNNN.md          # Per-feature design + status doc (BD/DD/CD/UT/IT/ST/UAT)
│   ├── proposals/
│   │   ├── human/             # Human-authored proposals
│   │   └── analyst/           # Retrospective Analyst output
│   └── daily-report/
│       └── daily_report_YYYYMMDD.md
├── log/
│   ├── bot.log                # Runtime log
│   └── agent-metrics.jsonl    # SubagentStop hook appends metrics here (JSONL)
└── .claude/
    ├── agents/
    │   └── retrospective-analyst.md  # Deployed agent definition
    ├── hooks/
    │   └── subagent_stop.py          # SubagentStop metrics hook
    ├── commands/
    │   └── review-checklist.md       # /review-checklist skill
    └── settings.json                  # Hook registration
```

---

## Current State vs. Target State

### Current (`app.py` — single-file Claude API bot)

The current `app.py` is a placeholder that calls the Claude API directly. It does NOT use tmux or response files.

| Feature | Status |
|---|---|
| Slack Socket Mode | ✅ Working |
| Claude API responses | ✅ Working (direct API, not tmux) |
| Per-user history | ✅ In memory |
| tmux integration | ❌ Not implemented |
| Channel authorization | ❌ Not implemented |
| Thread replies | ❌ Not implemented |
| ChannelState | ❌ Not implemented |
| Response file relay | ❌ Not implemented |

### Target (per CLAUDE.md design)

| Feature | Status |
|---|---|
| Slack Socket Mode | ✅ Carry over |
| tmux integration | ⬜ Planned |
| Channel = tmux window mapping | ⬜ Planned |
| ChannelState per channel | ⬜ Planned |
| Response file relay | ⬜ Planned |
| Thread replies | ⬜ Planned |
| Long message handling | ⬜ Planned |
| `/reset` command | ⬜ Planned |

---

## Planned Module Map

### `src/config.py`
Channel-to-tmux mapping and constants. No logic.

| Constant | Purpose |
|---|---|
| `CHANNEL_MAP` | Dict mapping Slack channel ID → `ChannelConfig(target, cwd, tmp)` |
| `TMUX_SESSION` | The tmux session name (e.g. `"claude_session"`) |
| `CAPTURE_WAIT` | Default max wait seconds for response file (e.g. `120`) |
| `MAX_MESSAGE_LENGTH` | Slack soft limit for plain text (~3000 chars) |

### `src/channel_state.py`
ChannelState dataclass (per CLAUDE.md § 2).

```python
@dataclass
class ChannelState:
    target: str              # tmux "session:window"
    cwd: str                 # project working directory
    tmp: str                 # response file output directory
    generation: int = 0
    is_processing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
```

**Critical invariants:**
- `generation` and `is_processing` must be read-modified-written under `state.lock`.
- `is_processing` must be cleared in a `finally` block.
- Watcher thread compares only `state.generation == own_gen` to detect expiry.

### `src/tmux_handler.py`
All subprocess calls to tmux. Reused from telegram-claude-bot with `window` parameter.

| Function | Behavior |
|---|---|
| `session_exists(session)` | `tmux has-session` |
| `send_input(session, text, window)` | Sends text + Enter (0.3s delay) |
| `capture_output(session, wait, window)` | Captures 500 lines of scrollback |
| `window_exists(session, window)` | Checks `tmux list-windows` |
| `create_window(session, name, cwd)` | `new-window` + launch Claude Code |
| `restart_claude(session, cwd, window)` | `respawn-window -k` + restart |

### `src/file_handler.py`
Slack-specific sending. Mirrors telegram-claude-bot's file_handler but uses Slack API.

| Function | Behavior |
|---|---|
| `send_long_text(client, channel, text, thread_ts)` | ≤3000 chars → `chat_postMessage`; longer → `files_upload_v2` as snippet |

### `src/bot.py`
Main process. Runs `SocketModeHandler` and handles all Slack events.

**Global state** (in-memory, lost on restart):

| Variable | Default | Purpose |
|---|---|---|
| `_channel_states` | `{}` | Dict[channel_id, ChannelState] — initialized from `CHANNEL_MAP` |

**Event handlers** (all check channel authorization first):

| Event / Command | Handler | Key behavior |
|---|---|---|
| `app_mention` or DM text | `handle_message` | Core relay — see data flow below |
| `/reset` | `handle_reset` | Increments generation, clears is_processing for own channel |
| `/reset all` | `handle_reset_all` | Same for all channels |

---

## Core Data Flow (message relay)

```
User sends message in Slack channel
        ↓
bot.py: handle_message()
  1. Check channel is in CHANNEL_MAP — reject if not
  2. Acquire state.lock → check is_processing → set is_processing = True, gen = generation
  3. Build response_file = tmp/{channel_id}_{gen}.txt
  4. Delete response_file if it already exists
  5. Append instruction to message:
     "[TOP PRIORITY] {response_file} = sole Slack output channel. Write FINAL response via Write tool AFTER all reasoning/work."
  6. tmux_handler.send_input(session, full_text, window=state.target)
  7. Reply "送信しました... [channel: {name}]" to user (in thread)
  8. Start background watcher thread(response_file, channel_id, thread_ts, gen)
        ↓ polls every 2s
Claude Code (in tmux window)
  - Processes instruction
  - Writes response to tmp/{channel_id}_{gen}.txt  ← MUST happen before anything else
        ↓ file appears (settle check: size stable for 1s)
bot.py: watcher thread
  9. Read response_file content
  10. Delete response_file
  11. file_handler.send_long_text(content) → Slack thread reply
  12. finally: clear is_processing if generation still matches own gen
```

**Critical**: Claude Code MUST write the response file. If it delays, the watcher times out and the user gets a timeout notice.

---

## Slack API Characteristics (Design Notes)

**`chat_postMessage` and `files_upload_v2` are NOT idempotent.**

Sending the same request twice creates two messages. If an HTTP response is lost due to a network error, the Slack server may have already processed the message. Retrying causes duplicates.

**Safe strategy:**
- ❌ Send → fail → wait → resend (duplicate risk)
- ✅ Pre-check connectivity → send once → do not retry after send

Any new send logic must follow this pattern.

---

## Known Issues and Risks

| ID | Severity | Summary |
|---|---|---|
| (none yet) | — | Project is in early migration phase |

Refer to `docs/issues.md` for the current list as the project evolves.

---

## Coding Conventions

- **No comments for WHAT** — function names and variable names are self-documenting.
- **Comments only for WHY** — non-obvious constraints, workarounds, invariants.
- **Error handling only at boundaries** — user input (Slack messages/commands), external calls (tmux subprocess, Slack API). Trust internal module calls.
- **No defensive fallbacks for impossible cases** — don't add `if client is None` inside handlers.
- **No unused abstractions** — implement only what DD specifies.
- **Security**: All handlers must check channel authorization first. Never pass unsanitized user input directly to shell commands.
- **Secrets**: Always load from `.env` via `python-dotenv`. Never hardcode tokens or paths.
