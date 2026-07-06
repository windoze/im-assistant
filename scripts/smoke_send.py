"""Send a DingTalk robot smoke-test message and print contact users."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.infra.config import DEFAULT_ENV_PATH, load_config  # noqa: E402
from src.infra.dingtalk_client import DingTalkClient  # noqa: E402
from src.infra.log import configure_logging, get_logger  # noqa: E402

SMOKE_USER_ID_ENV = "DINGTALK_SMOKE_USER_ID"

logger = get_logger("scripts.smoke_send")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse smoke-test command-line arguments."""

    parser = argparse.ArgumentParser(
        description="Send a DingTalk robot one-to-one message and print contact users.",
    )
    parser.add_argument(
        "user_id",
        nargs="?",
        help=f"DingTalk userId to receive the smoke-test message. Defaults to {SMOKE_USER_ID_ENV}.",
    )
    parser.add_argument(
        "--text",
        default="DingTalk AI assistant smoke test",
        help="Text content to send to the target userId.",
    )
    parser.add_argument(
        "--department-id",
        default="1",
        help="Department id used when printing the contact list. Defaults to DingTalk root dept 1.",
    )
    args = parser.parse_args(argv)
    args.user_id = _resolve_smoke_user_id(args.user_id)
    if args.user_id is None:
        parser.error(
            f"provide a user_id argument or set {SMOKE_USER_ID_ENV} in the environment or .env"
        )
    return args


def _resolve_smoke_user_id(arg_user_id: str | None) -> str | None:
    if arg_user_id is not None and arg_user_id.strip() != "":
        return arg_user_id.strip()

    env_user_id = os.environ.get(SMOKE_USER_ID_ENV)
    if env_user_id is not None and env_user_id.strip() != "":
        return env_user_id.strip()

    dotenv_user_id = dotenv_values(DEFAULT_ENV_PATH).get(SMOKE_USER_ID_ENV)
    if dotenv_user_id is not None and dotenv_user_id.strip() != "":
        return dotenv_user_id.strip()

    return None


async def async_main() -> None:
    """Load configuration, send the smoke message, and print contact users."""

    args = parse_args()
    config = load_config()
    configure_logging(config.logging.level)

    async with DingTalkClient(config.dingtalk) as client:
        users = await client.get_user_list(department_id=args.department_id)
        await client.send_oto([args.user_id], args.text)

    print("Contacts:")
    for user_id, name in sorted(users.items()):
        print(f"{user_id}\t{name}")

    logger.info(
        "dingtalk_smoke_message_sent",
        extra={"user_id": args.user_id, "contact_count": len(users)},
    )


if __name__ == "__main__":
    asyncio.run(async_main())
