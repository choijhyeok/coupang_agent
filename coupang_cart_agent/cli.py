from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace

from .cart_adapters import (
    ChromeCdpCoupangCartPage,
    ChromeCdpSettings,
    DemoCoupangCartPage,
    PlaywrightCoupangCartPage,
    PlaywrightCoupangSettings,
)
from .cart_persistence import SqliteCartResultStore
from .config import ConfigError, load_config
from .contracts import ProductCandidate, SelectedProduct
from .cart_executor import CoupangCartExecutor, SessionCredentials
from .integration import CoupangCartAgentFlow
from .notifications import RetryingNotificationService
from .selection import HeuristicProductSelectionService
from .contracts import demo_contract_payload
from .telegram_intake import TelegramBotApiClient, TelegramPollingIntakeService


def build_live_cart_page(config):
    playwright_settings = PlaywrightCoupangSettings(
        login_url=config.coupang_login_url,
        cart_url=config.coupang_cart_url,
        headless=config.coupang_browser_headless,
        storage_state_path=config.coupang_storage_state_path,
    )
    if config.coupang_browser_launch_mode == "cdp_chrome":
        if not config.coupang_chrome_user_data_dir:
            raise ConfigError(
                "COUPANG_CHROME_USER_DATA_DIR is required when COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome."
            )
        return ChromeCdpCoupangCartPage(
            settings=playwright_settings,
            cdp_settings=ChromeCdpSettings(
                chrome_user_data_dir=config.coupang_chrome_user_data_dir,
                chrome_profile_directory=config.coupang_chrome_profile_directory,
                remote_debugging_port=config.coupang_chrome_remote_debugging_port,
                copied_user_data_dir=".data/chrome-userdata-cdp",
            ),
        )
    return PlaywrightCoupangCartPage(playwright_settings)


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
                    "coupang_browser_headless": config.coupang_browser_headless,
                    "coupang_browser_launch_mode": config.coupang_browser_launch_mode,
                    "coupang_chrome_user_data_dir": config.coupang_chrome_user_data_dir,
                    "coupang_chrome_profile_directory": config.coupang_chrome_profile_directory,
                    "coupang_chrome_remote_debugging_port": config.coupang_chrome_remote_debugging_port,
                    "coupang_storage_state_path": config.coupang_storage_state_path,
                    "cart_db_path": config.cart_db_path,
                    "default_currency": config.default_currency,
                },
                indent=2,
            )
        )
        return 0

    if command == "parse-telegram-message":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent parse-telegram-message")
        parser.add_argument("text")
        parser.add_argument("--user-id", default="telegram:cli-user")
        parser.add_argument("--chat-id", default="cli-chat")
        parsed = parser.parse_args(args[1:])
        service = TelegramPollingIntakeService()
        request = service.parse_message(
            user_id=parsed.user_id,
            chat_id=parsed.chat_id,
            text=parsed.text,
        )
        print(json.dumps(asdict(request), ensure_ascii=False, indent=2, default=str))
        return 0

    if command == "poll-telegram-once":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent poll-telegram-once")
        parser.add_argument("--offset", type=int, default=None)
        parser.add_argument("--timeout", type=int, default=1)
        parsed = parser.parse_args(args[1:])
        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        service = TelegramPollingIntakeService(
            TelegramBotApiClient(token=config.telegram_bot_token)
        )
        results = service.poll_once(offset=parsed.offset, timeout=parsed.timeout)
        print(
            json.dumps(
                [result.as_dict() for result in results],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if command == "integration-demo":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent integration-demo")
        parser.add_argument("text")
        parser.add_argument("--scenario", choices=("success", "cart-failure"), default="success")
        parser.add_argument("--user-id", default="telegram:cli-user")
        parser.add_argument("--chat-id", default="cli-chat")
        parsed = parser.parse_args(args[1:])

        delivered_messages: list[dict[str, str]] = []

        def sender(chat_id: str, text: str) -> None:
            delivered_messages.append({"chat_id": chat_id, "text": text})

        def candidate_source(request):
            candidates_by_item = {}
            for index, item in enumerate(request.items, start=1):
                candidates_by_item[item.name] = [
                    ProductCandidate(
                        product_id=f"{index}-cheap",
                        name=f"{item.name} 보급형",
                        price_krw=5900,
                        rating=3.8,
                        review_count=19,
                        product_url=f"https://www.coupang.com/vp/products/{index}-cheap",
                    ),
                    ProductCandidate(
                        product_id=f"{index}-balanced",
                        name=f"{item.name} 추천",
                        price_krw=8900,
                        rating=4.8,
                        review_count=1800,
                        product_url=f"https://www.coupang.com/vp/products/{index}-balanced",
                    ),
                    ProductCandidate(
                        product_id=f"{index}-premium",
                        name=f"{item.name} 프리미엄",
                        price_krw=11900,
                        rating=4.9,
                        review_count=900,
                        product_url=f"https://www.coupang.com/vp/products/{index}-premium",
                    ),
                ]
            return candidates_by_item

        flow = CoupangCartAgentFlow(
            intake_service=TelegramPollingIntakeService(),
            candidate_source=candidate_source,
            selection_service=HeuristicProductSelectionService(),
            cart_service=CoupangCartExecutor(
                page=DemoCoupangCartPage(should_fail=parsed.scenario == "cart-failure"),
                credentials=SessionCredentials(
                    username="demo-user",
                    password="demo-password",
                ),
            ),
            notification_service=RetryingNotificationService(sender=sender, max_attempts=1),
        )
        result = flow.run_text_request(
            user_id=parsed.user_id,
            chat_id=parsed.chat_id,
            text=parsed.text,
        )
        print(
            json.dumps(
                {
                    **result.as_dict(),
                    "delivered_messages": delivered_messages,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if command == "cart-live-add":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent cart-live-add")
        parser.add_argument("--product-url", required=True)
        parser.add_argument("--product-id", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--request-item-name", default=None)
        parser.add_argument("--quantity", type=int, default=1)
        parser.add_argument("--price-krw", type=int, default=0)
        parser.add_argument("--rating", type=float, default=0.0)
        parser.add_argument("--review-count", type=int, default=0)
        parser.add_argument("--vendor", default=None)
        parser.add_argument("--option", action="append", default=[])
        parser.add_argument("--db-path", default=None)
        parser.add_argument("--headed", action="store_true")
        parsed = parser.parse_args(args[1:])

        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        option_hints: dict[str, str] = {}
        for raw in parsed.option:
            if "=" not in raw:
                print(f"Invalid --option value: {raw}. Expected key=value.", file=sys.stderr)
                return 1
            key, value = raw.split("=", 1)
            option_hints[key.strip()] = value.strip()

        selected = SelectedProduct(
            request_item_name=parsed.request_item_name or parsed.name,
            candidate=ProductCandidate(
                product_id=parsed.product_id,
                name=parsed.name,
                price_krw=parsed.price_krw,
                rating=parsed.rating,
                review_count=parsed.review_count,
                product_url=parsed.product_url,
                vendor=parsed.vendor,
            ),
            quantity=parsed.quantity,
            selection_reason="Manual live cart validation target.",
            score=0.0,
            option_hints=option_hints,
        )
        runtime_config = replace(
            config,
            coupang_browser_headless=False if parsed.headed else config.coupang_browser_headless,
        )
        page = build_live_cart_page(runtime_config)
        executor = CoupangCartExecutor(
            page=page,
            credentials=SessionCredentials(
                username=config.coupang_username,
                password=config.coupang_password,
            ),
            result_store=SqliteCartResultStore(parsed.db_path or config.cart_db_path),
        )

        try:
            result = executor.add_products([selected])[0]
        finally:
            page.close()

        print(
            json.dumps(
                {
                    "result": asdict(result),
                    "audit_log": [asdict(entry) for entry in executor.audit_log()],
                    "db_path": parsed.db_path or config.cart_db_path,
                    "launch_mode": config.coupang_browser_launch_mode,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    print(
        "Usage: python -m coupang_cart_agent "
        "[contracts-example|check-config|parse-telegram-message|poll-telegram-once|integration-demo|cart-live-add]",
        file=sys.stderr,
    )
    return 1
