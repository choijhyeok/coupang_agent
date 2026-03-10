from __future__ import annotations

import json
import sys

from .config import ConfigError, load_config
from .contracts import demo_contract_payload


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    command = args[0] if args else "help"

    if command == "contracts-example":
        print(json.dumps(demo_contract_payload(), ensure_ascii=False, indent=2, default=str))
        return 0

    if command == "check-config":
        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        print(
            json.dumps(
                {
                    "telegram_bot_token_set": bool(config.telegram_bot_token),
                    "coupang_username": config.coupang_username,
                    "coupang_login_url": config.coupang_login_url,
                    "coupang_cart_url": config.coupang_cart_url,
                    "default_currency": config.default_currency,
                },
                indent=2,
            )
        )
        return 0

    print(
        "Usage: python -m coupang_cart_agent [contracts-example|check-config]",
        file=sys.stderr,
    )
    return 1
