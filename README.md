# social-mcp

A local Model Context Protocol (MCP) server that bridges an AI agent (e.g. OpenCode)
with your personal Telegram direct messages: read unread messages, pull chat
history for context, and send replies.

## How it works

```
opencode (AI agent) ↔ social-mcp (MCP over stdio) ↔ Telethon ↔ Telegram
```

This is **not** a bot. The server runs in *userbot* mode — it logs in as **your own
account** via the Telegram API, so everything it sends is sent from you. Your AI
assistant simply gets a set of tools (15+) to read and manage your own chats.

> ⚠️ **Read before using**
> - Automating a personal account sits in a grey zone of Telegram's Terms of
>   Service. Keep the automation reasonable, don't spam, use at your own risk.
> - Store `.env` and `*.session` outside of version control. These files grant
>   full access to your account.
> - Only you ("the boss") should command the assistant. It must never act on
>   instructions coming from message content itself.

## Requirements

- Python **3.10+**
- A Telegram `api_id` / `api_hash` from https://my.telegram.org (API Development Tools)
- Works on Windows, Linux and macOS

## Project layout

```
social-mcp/
├── config.py
├── clients/
│   ├── __init__.py
│   └── telegram_client.py
├── storage.py
├── server.py
├── setup_auth.py
├── requirements.txt
└── README.md
```

## Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure credentials

Get your Telegram API ID/hash from https://my.telegram.org (API Development Tools).

Create a `.env` file in the project root:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_PHONE=+15551234567

# Optional overrides
SOCIAL_MCP_DATA_DIR=/home/you/.social-mcp
SOCIAL_MCP_LOG_LEVEL=INFO
```

Session/state files live under `SOCIAL_MCP_DATA_DIR` (default `~/.social-mcp`),
deliberately **outside** the repo so they can never be committed by accident.

## First-time interactive login

Run this once, manually, from a real terminal — OpenCode invokes `server.py`
over stdio and cannot answer interactive prompts.

```bash
python setup_auth.py --telegram
```

Telegram will text/app you a login code, and ask for your 2FA password if enabled.

## Run the server standalone (smoke test)

```bash
python server.py
```

It will idle on stdio waiting for MCP protocol messages — that's expected;
this step just confirms it starts without import/config errors. Ctrl+C to stop.

## Wire it into OpenCode

Add this to `~/.config/opencode/opencode.json` (adjust paths to your machine):

```json
{
  "mcpServers": {
    "social-mcp": {
      "command": "/absolute/path/to/social-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/social-mcp/server.py"],
      "env": {
        "SOCIAL_MCP_DATA_DIR": "/home/you/.social-mcp"
      }
    }
  }
}
```

Restart OpenCode. It should discover these tools:

- `get_unread_messages(limit?, platforms?)`
- `send_reply(platform, target_id, text)`
- `get_chat_history(platform, target_id, limit?)`
- `edit_message` / `delete_message`
- `delete_chat` — full dissolution: kicks all members, leaves, purges (groups); hard-deletes owned channels; revoke-deletes private dialogs
- `leave_chat` — exit a group/channel without touching its members
- `block_user` / `unblock_user` / `get_blocked_users`
- `create_group` / `create_supergroup` / `add_user_to_group` / `remove_user_from_group` / `invite_to_channel`

## Example agent workflow

1. Agent calls `get_unread_messages(limit=10)` → gets a JSON list of unread
   messages.
2. Agent summarizes them for you.
3. You say "reply to Anna on Telegram: I'm free after 6pm".
4. Agent optionally calls `get_chat_history(platform="telegram", target_id=<id>)`
   to see prior context, drafts a reply, and calls
   `send_reply(platform="telegram", target_id=<id>, text="...")`.

## Notes on reliability

- All tool functions catch platform-specific errors and return a structured
  `{"success": false, "error": "..."}` JSON payload instead of raising, so a
  single failed call never kills the stdio connection to OpenCode.
- Group deletion handles all Telegram quirks: owned channels are hard-deleted
  via `channels.deleteChannel`, basic groups via `messages.deleteChat` when you
  have admin rights, with an automatic kick-all → leave → purge fallback otherwise.
- `storage.py` keeps a small SQLite file recording which message IDs have
  already been surfaced, as a foundation for future "mark as read" / dedup
  logic — it is not yet wired into filtering by default.

## License

[MIT](LICENSE) © sydrx
