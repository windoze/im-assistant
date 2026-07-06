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
   and log level in `config.yaml` when needed.

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

When a user privately messages the bot or @mentions it in a group, the service sends the text to
Claude with a short enterprise-assistant system prompt and replies through `sessionWebhook` when it
is still valid, otherwise through DingTalk OpenAPI. This M1 path is intentionally stateless: no
history, tools, or Session runtime are used yet. Non-text messages receive `暂只支持文本`.

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
