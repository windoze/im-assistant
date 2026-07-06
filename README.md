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

Run tests:

```bash
pytest
```
