# DingTalk AI Assistant

An asyncio-based DingTalk AI assistant that will connect DingTalk Stream events,
DingTalk OpenAPI calls, Claude responses, SQLite state, and encrypted user tokens.

## Setup

1. Create a Python 3.11+ virtual environment.
2. Install the project with development dependencies:

   ```bash
   python -m pip install -e ".[dev]"
   ```

3. Copy `.env.example` to `.env` and fill in the DingTalk and Anthropic values. Generate
   `TOKEN_VAULT_FERNET_KEY` with:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

4. Adjust non-secret settings such as the Claude model, DingTalk API base URL, session timeout,
   SQLite database path, document creation defaults, and log level in `config.yaml` when needed.

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

`src.infra.token_vault.TokenVault` stores DingTalk user-level OBO access and refresh tokens in the
`token_vault` table encrypted with the `.env` Fernet key, and marks grants that are expired or within
five minutes of expiry as needing refresh. `TokenVault.get_valid(...)` can silently refresh those
grants through the DingTalk refresh-token grant and clears rejected refresh tokens so the caller can
start a new consent flow.

`src.infra.oauth` provides the DingTalk OAuth2 HTTP endpoints for OBO flows. Applications create a
short-lived `PendingAuthStore` nonce with the requesting actor identity, send the user to
`/oauth/start?nonce=<nonce>`, and the aiohttp service redirects to DingTalk consent with
`state=<nonce>`. `/oauth/callback` validates and consumes that state once, exchanges the authorization
code at `/v1.0/oauth2/userAccessToken`, calls `/v1.0/contact/users/me` with the user token, rejects
the callback if the returned `unionId` does not match the pending actor, then stores the verified
access/refresh token in `TokenVault` before invoking the completion callback used to resume the
pending session. The configured `OAUTH_REDIRECT_URI` must point at `/oauth/callback` on a public HTTPS
URL or local tunnel for browser-based manual validation.

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

The built-in system tier now includes three application-level DingTalk tools that use the app access
token and do not require OBO authorization:

| Tool | Purpose | Notes |
|---|---|---|
| `contact_lookup` | Look up DingTalk contacts by userId or display name. | Uses the contact APIs from the OpenAPI client. |
| `create_doc` | Create a DingTalk document and append text content. | For group use, add `create_doc` to `capabilities.channel_enabled.<openConversationId>`. Configure `dingtalk.document.parent_object_type` and `dingtalk.document.parent_object_id`, or provide those fields as tool input. |
| `create_todo` | Create a DingTalk todo task for the current actor or a specified assignee. | Resolves userId to unionId through the contact API, then calls the todo API with the app token. |

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
