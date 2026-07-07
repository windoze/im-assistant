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
`Idle → RunningAgent → Idle` state transitions, or `RunningAgent → AwaitingInteraction` when an
out-of-band consent/confirm interrupt suspends the turn.

On Stream startup the service idempotently initializes the SQLite database configured by
`storage.database_path` with tables for sessions, message history, identity bindings, audit logs,
pending interactions, and encrypted token material.

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

`src.capabilities.Authorizer` is the execution-time authorization gate for capability requirements.
For OBO requirements it checks `TokenVault.get_valid(...)`, uses DingTalk silent refresh when needed,
returns `Granted` with a user credential when a usable token exists, returns `NeedsConsent` with a
pending `/oauth/start?nonce=...` link when consent is missing, and returns `Denied` for OBO tools
outside DMs. When an agent tool call needs consent, the Session is persisted as `AwaitingInteraction`
and the bot replies with the authorization link.

`src.core.interrupt.SessionInterruptManager` persists out-of-band `consent` and `confirm`
interactions in `pending_interactions` with a `correlation_id`, expected responder, payload, and
expiry. Resolving or cancelling an interrupt validates the responder, marks the pending row terminal,
clears the Session's `pending_interaction` context, and restores the Session to `Idle`; `AgentLoop`
exposes this through `resume_interaction(...)` for OAuth/callback routing.

Capability handlers can call `await context.confirm(action, details)` before sensitive side effects.
The agent loop stores a `confirm` interrupt, sends a DingTalk interactive card with confirm/cancel
buttons, and returns without running the tool. DingTalk card callbacks are registered on the Stream
card callback topic, normalized by `normalize_card_callback(...)`, and routed by
`InteractionCallbackRouter` using the callback `correlation_id` plus responder. Confirm executes the
deferred tool directly without another LLM roundtrip; cancel resolves the pending interaction without
running the tool.

If a Session is still `AwaitingInteraction`, a later inbound message cancels the pending interaction as
`superseded_by_new_message`, sends a runtime system notice such as `已取消:未确认，[发送钉钉通知] 未执行。`,
then processes the new message normally. Pending interactions also schedule a timeout cancellation at
their persisted `expires_at`, and Stream startup restores those timers from SQLite so restart-recovered
interactions still expire; timeout sends the system notice directly and records a silent `Cancelled`
tool result in SQLite history without asking Claude to narrate the cancellation again.

Before any text message enters the agent loop, `src.core.router.classify_inbound_message(...)`
deterministically checks for an active pending interaction, then slash-command text beginning with `/`,
and only sends ordinary messages to Claude. Slash commands are dispatched through the independent
`src.core.commands.CommandRegistry`, which checks command availability, actor role requirements, and
argument specs before calling a deterministic handler. Commands can affect later AI turns only by calling
the command injection API, which appends explicit command-originated history to the Session; concrete
built-in commands are added by the next M6 task.

Capabilities are declared with `src.capabilities.Capability` and optional `Requirement` metadata.
The registry loads Python modules from `src/capabilities/system/`, `src/capabilities/base/`, and
`src/capabilities/user/<userId>/` in that order, so later tiers override earlier capabilities with
the same name. Visibility is filtered by `src.capabilities.can_use(...)`: capabilities requiring
on-behalf-of user authority are hidden outside DMs, globally available capabilities are visible
everywhere, DM-capable system/base capabilities are visible in DMs, user capabilities are visible
only to their owner in DM, and group capabilities must be listed under
`capabilities.channel_enabled.<channel_id>` in `config.yaml`.

Visible executable capabilities are exposed to Claude as tools. A capability may provide
`description` and JSON-object `input_schema` metadata; its handler is called as
`handler(context, **arguments)` with a `CapabilityExecutionContext`. Handlers can use `context.user`,
`context.group`, and `context.require_user_token(service)` from the injected `CredentialContext`.
Handler failures are returned to Claude as error `tool_result` blocks so the agent loop can continue
to a normal text reply.

The built-in system tier includes application-level DingTalk tools that use the app access token and
one DM-only calendar tool that uses OBO authorization:

| Tool | Purpose | Notes |
|---|---|---|
| `contact_lookup` | Look up DingTalk contacts by userId or display name. | Uses the contact APIs from the OpenAPI client. |
| `create_doc` | Create a DingTalk document and append text content. | For group use, add `create_doc` to `capabilities.channel_enabled.<openConversationId>`. Configure `dingtalk.document.parent_object_type` and `dingtalk.document.parent_object_id`, or provide those fields as tool input. |
| `create_todo` | Create a DingTalk todo task for the current actor or a specified assignee. | Resolves userId to unionId through the contact API, then calls the todo API with the app token. |
| `send_notification` | Send a DingTalk notification after an explicit confirm-card approval. | Calls `context.confirm("发送钉钉通知", details)` before sending; cancel callbacks do not send. For group use, add `send_notification` to `capabilities.channel_enabled.<openConversationId>`. |
| `schedule_summary` | Summarize the current DM actor's DingTalk calendar for today. | Requires `calendar:read` OBO consent, reads `/v1.0/calendar/primary` and `/v1.0/calendar/users/me/calendars/<calendarId>/events` with the user token, then asks Claude to summarize the events. |

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
