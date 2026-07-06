# DingTalk AI Assistant

An asyncio-based DingTalk AI assistant that will connect DingTalk Stream events,
DingTalk OpenAPI calls, Claude responses, SQLite state, and encrypted user tokens.

## Setup

1. Create a Python 3.11+ virtual environment.
2. Install the project with development dependencies:

   ```bash
   python -m pip install -e ".[dev]"
   ```

3. Copy `.env.example` to `.env` and fill in the DingTalk and Anthropic values.
4. Adjust non-secret settings such as the Claude model, DingTalk API base URL, session timeout,
   SQLite database path, and log level in `config.yaml` when needed.

## Usage

Run the current skeleton entry point:

```bash
python -m src.main
```

Run the DingTalk Stream receiver and log normalized inbound chatbot messages. This requires a
populated `.env` with DingTalk app credentials and an app configured for Stream mode:

```bash
python -m src.main --stream
```

When a user privately messages the bot or @mentions it in a group, the service resolves one
persistent Session for that DingTalk conversation, loads recent chat history from SQLite, sends the
multi-turn context to Claude with a short enterprise-assistant system prompt, persists the completed
user/assistant turn, and replies through `sessionWebhook` when it is still valid, otherwise through
DingTalk OpenAPI. The first group @mention activates that group Session and sends a welcome message
before normal handling continues. Non-text messages receive `暂只支持文本`. Inbound Stream events are
queued by Session, so messages in the same DingTalk conversation are processed strictly in order
while different conversations can continue in parallel; each agent turn persists
`Idle → RunningAgent → Idle` state transitions.

On Stream startup the service idempotently initializes the SQLite database configured by
`storage.database_path` with tables for sessions, message history, identity bindings, audit logs,
and encrypted token material.

Capabilities are declared with `src.capabilities.Capability` and optional `Requirement` metadata.
The registry loads Python modules from `src/capabilities/system/`, `src/capabilities/base/`, and
`src/capabilities/user/<userId>/` in that order, so later tiers override earlier capabilities with
the same name. Visibility is filtered by `src.capabilities.can_use(...)`: capabilities requiring
on-behalf-of user authority are hidden outside DMs, globally available capabilities are visible
everywhere, user capabilities are visible only to their owner in DM, and group capabilities must be
listed under `capabilities.channel_enabled.<channel_id>` in `config.yaml`.

Visible executable capabilities are exposed to Claude as tools. A capability may provide
`description` and JSON-object `input_schema` metadata; its handler is called as
`handler(context, **arguments)` with a `CapabilityExecutionContext`. Handler failures are returned to
Claude as error `tool_result` blocks so the agent loop can continue to a normal text reply.

Run tests:

```bash
pytest
```

Send a DingTalk robot smoke-test message and print the contact mapping. This requires a populated
`.env` with DingTalk credentials, robot messaging permission, and contact-read permission:

```bash
python scripts/smoke_send.py <userId>
```

For repeatable local smoke runs, set `DINGTALK_SMOKE_USER_ID` in `.env` and omit the positional
argument:

```bash
python scripts/smoke_send.py
```
