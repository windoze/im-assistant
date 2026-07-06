"""Send a DingTalk robot smoke-test message and print contact users."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.infra.config import load_config  # noqa: E402
from src.infra.dingtalk_client import DingTalkClient  # noqa: E402
from src.infra.log import configure_logging, get_logger  # noqa: E402

logger = get_logger("scripts.smoke_send")


def parse_args() -> argparse.Namespace:
    """Parse smoke-test command-line arguments."""

    parser = argparse.ArgumentParser(
        description="Send a DingTalk robot one-to-one message and print contact users.",
    )
    parser.add_argument("user_id", help="DingTalk userId to receive the smoke-test message.")
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
    return parser.parse_args()


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
