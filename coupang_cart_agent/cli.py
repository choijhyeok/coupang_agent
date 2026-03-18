from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.postgres import PostgresSaver

from .azure_openai import AzureOpenAIPlanner
from .candidate_sources import (
    CapturedCoupangFixtureCandidateSource,
    DemoCandidateSource,
    LiveBrowserDiscoveryCandidateSource,
)
from .cart_verification import AzureOpenAICartVerifier
from .cart_adapters import (
    BrowserUseCoupangCartPage,
    BrowserUseSettings,
    ChromeCdpCoupangCartPage,
    ChromeCdpSettings,
    DemoCoupangCartPage,
    ExistingChromeCdpCoupangCartPage,
    PlaywrightCoupangCartPage,
    PlaywrightCoupangSettings,
)
from .cart_executor import CoupangCartExecutor, SessionCredentials
from .cart_persistence import SqliteCartResultStore
from .config import ConfigError, load_config, load_telegram_bot_token
from .contracts import (
    CartAddResult,
    CartAddStage,
    IntakeMode,
    RequestSession,
    ProductCandidate,
    RequestedItem,
    SelectedProduct,
    ShoppingRequest,
    ShoppingRequestEnvelope,
    demo_contract_payload,
)
from .http_server import CoupangCartAgentHttpServer
from .integration import CoupangCartAgentFlow
from .live_browser_agent import AzureOpenAIBrowserAgent, CoupangLiveBrowserShoppingAgent
from .live_workflow import CoupangCartAgentLiveWorkflow
from .notifications import (
    RetryingNotificationService,
    SQLiteNotificationContextStore,
    TelegramSendMessageSender,
    build_failure_notification_payload,
    build_success_notification_payload,
)
from .postgres_store import PostgresOperationalStore
from .selection import HeuristicProductSelectionService
from .telegram_intake import TelegramBotApiClient, TelegramPollingIntakeService
from .telegram_persistence import TelegramIntakeRepository
from .telegram_worker import TelegramLiveWorker


def _build_live_intake_service(*, token: str, db_path: str) -> TelegramPollingIntakeService:
    return TelegramPollingIntakeService(
        TelegramBotApiClient(token=token),
        TelegramIntakeRepository(db_path),
    )


def _build_live_notification_service(*, token: str) -> RetryingNotificationService:
    return RetryingNotificationService(
        sender=TelegramSendMessageSender(client=TelegramBotApiClient(token=token)),
        max_attempts=3,
    )


def _build_live_cart_verifier(config):
    return AzureOpenAICartVerifier(
        endpoint=config.azure_openai_endpoint,
        api_key=config.azure_openai_api_key,
        deployment=config.azure_openai_deployment,
        api_version=config.azure_openai_api_version,
    )


def _build_live_candidate_source(*, config, fixture_path: str | None, page):
    if fixture_path:
        return CapturedCoupangFixtureCandidateSource(fixture_path=fixture_path)
    if config.coupang_search_endpoint:
        from .candidate_sources import LiveCoupangSearchCandidateSource

        return LiveCoupangSearchCandidateSource(search_endpoint=config.coupang_search_endpoint)
    return LiveBrowserDiscoveryCandidateSource(driver=page)


def _build_synthetic_live_envelope(request: ShoppingRequest) -> ShoppingRequestEnvelope:
    session_id = f"telegram-session:{request.chat_id}:{request.user_id}"
    occurred_at = request.received_at if request.received_at.tzinfo is not None else request.received_at.replace(tzinfo=UTC)
    follow_up_reply = TelegramPollingIntakeService.classify_follow_up_message(request.raw_text)
    return ShoppingRequestEnvelope(
        source="cli-live",
        mode=IntakeMode.LIVE,
        request=request,
        session=RequestSession(
            session_id=session_id,
            channel="telegram",
            user_id=request.user_id,
            chat_id=request.chat_id,
            created_at=occurred_at,
            last_message_at=occurred_at,
        ),
        inbound_message_id=request.request_id,
        update_id=None,
        message_id=None,
        raw_text=request.raw_text,
        raw_update={},
        metadata={
            "created_by": "integration-live-request",
            "follow_up_reply": follow_up_reply,
        },
    )


@contextmanager
def _open_live_workflow(
    *,
    config,
    fixture_path: str | None,
):
    if not config.postgres_dsn:
        raise ConfigError("POSTGRES_DSN or DATABASE_URL is required for live integration commands.")

    operational_store = PostgresOperationalStore(config.postgres_dsn)
    operational_store.setup()
    page = build_live_cart_page(config)
    candidate_source = _build_live_candidate_source(config=config, fixture_path=fixture_path, page=page)
    cart_service = CoupangCartExecutor(
        page=page,
        credentials=None,
        result_store=SqliteCartResultStore(config.cart_db_path),
        verifier=_build_live_cart_verifier(config),
    )
    shopping_agent = CoupangLiveBrowserShoppingAgent(
        driver=page,
        model=AzureOpenAIBrowserAgent(
            endpoint=config.azure_openai_endpoint,
            api_key=config.azure_openai_api_key,
            deployment=config.azure_openai_deployment,
            api_version=config.azure_openai_api_version,
        ),
        cart_verifier=_build_live_cart_verifier(config),
    )
    with PostgresSaver.from_conn_string(config.postgres_dsn) as checkpointer:
        checkpointer.setup()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=cart_service,
            notification_service=_build_live_notification_service(token=config.telegram_bot_token),
            operational_store=operational_store,
            agent_planner=AzureOpenAIPlanner(
                endpoint=config.azure_openai_endpoint,
                api_key=config.azure_openai_api_key,
                deployment=config.azure_openai_deployment,
                api_version=config.azure_openai_api_version,
            ),
            shopping_agent=shopping_agent,
            checkpointer=checkpointer,
        )
        try:
            yield workflow, operational_store
        finally:
            page.close()


def build_live_cart_page(config):
    playwright_settings = PlaywrightCoupangSettings(
        login_url=config.coupang_login_url,
        cart_url=config.coupang_cart_url,
        headless=config.coupang_browser_headless,
        storage_state_path=config.coupang_storage_state_path,
    )
    if config.coupang_browser_launch_mode == "browser_use":
        if not config.coupang_chrome_user_data_dir:
            raise ConfigError(
                "COUPANG_CHROME_USER_DATA_DIR is required when COUPANG_BROWSER_LAUNCH_MODE=browser_use."
            )
        return BrowserUseCoupangCartPage(
            settings=playwright_settings,
            browser_use_settings=BrowserUseSettings(
                chrome_user_data_dir=config.coupang_chrome_user_data_dir,
                chrome_profile_directory=config.coupang_chrome_profile_directory,
                remote_debugging_port=config.coupang_chrome_remote_debugging_port,
                copied_user_data_dir=".data/browser-use-profile",
            ),
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
    if config.coupang_browser_launch_mode == "existing_cdp":
        return ExistingChromeCdpCoupangCartPage(
            settings=playwright_settings,
            remote_debugging_port=config.coupang_chrome_remote_debugging_port,
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
                    "coupang_username_set": bool(config.coupang_username),
                    "coupang_password_set": bool(config.coupang_password),
                    "coupang_login_url": config.coupang_login_url,
                    "coupang_cart_url": config.coupang_cart_url,
                    "coupang_browser_headless": config.coupang_browser_headless,
                    "coupang_browser_launch_mode": config.coupang_browser_launch_mode,
                    "coupang_chrome_user_data_dir": config.coupang_chrome_user_data_dir,
                    "coupang_chrome_profile_directory": config.coupang_chrome_profile_directory,
                    "coupang_chrome_remote_debugging_port": config.coupang_chrome_remote_debugging_port,
                    "coupang_storage_state_path": config.coupang_storage_state_path,
                    "coupang_attach_mode_requires_operator_login": True,
                    "cart_db_path": config.cart_db_path,
                    "default_currency": config.default_currency,
                    "azure_openai_endpoint": config.azure_openai_endpoint,
                    "azure_openai_deployment": config.azure_openai_deployment,
                    "azure_openai_api_version": config.azure_openai_api_version,
                    "postgres_dsn_set": bool(config.postgres_dsn),
                    "coupang_search_endpoint": config.coupang_search_endpoint,
                    "app_host": config.app_host,
                    "app_port": config.app_port,
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
        request = service.parse_demo_message(
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
        parser.add_argument("--db-path", default=".artifacts/telegram_intake.sqlite3")
        parser.add_argument("--skip-error-response", action="store_true")
        parsed = parser.parse_args(args[1:])
        try:
            token = load_telegram_bot_token()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        service = _build_live_intake_service(token=token, db_path=parsed.db_path)
        results = service.poll_once(
            offset=parsed.offset,
            timeout=parsed.timeout,
            mode=IntakeMode.LIVE,
            send_error_response=not parsed.skip_error_response,
        )
        print(
            json.dumps(
                {
                    "mode": "live",
                    "db_path": parsed.db_path,
                    "results": [result.as_dict() for result in results],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if command == "capture-telegram-live-request":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent capture-telegram-live-request")
        parser.add_argument("--offset", type=int, default=None)
        parser.add_argument("--timeout", type=int, default=30)
        parser.add_argument("--max-attempts", type=int, default=10)
        parser.add_argument("--sleep-seconds", type=float, default=0.0)
        parser.add_argument("--db-path", default=".artifacts/telegram_intake.sqlite3")
        parser.add_argument("--skip-error-response", action="store_true")
        parsed = parser.parse_args(args[1:])
        try:
            token = load_telegram_bot_token()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        service = _build_live_intake_service(token=token, db_path=parsed.db_path)
        next_offset = parsed.offset
        attempts: list[dict[str, object]] = []
        captured_result = None
        for attempt_number in range(1, parsed.max_attempts + 1):
            results = service.poll_once(
                offset=next_offset,
                timeout=parsed.timeout,
                mode=IntakeMode.LIVE,
                send_error_response=not parsed.skip_error_response,
            )
            highest_update_id = max((result.update_id for result in results), default=None)
            if highest_update_id is not None:
                next_offset = highest_update_id + 1
            attempts.append(
                {
                    "attempt": attempt_number,
                    "offset": next_offset,
                    "result_count": len(results),
                }
            )
            if results:
                captured_result = results[0]
                break
            if parsed.sleep_seconds > 0 and attempt_number < parsed.max_attempts:
                time.sleep(parsed.sleep_seconds)

        print(
            json.dumps(
                {
                    "mode": "live-capture",
                    "db_path": parsed.db_path,
                    "attempts": attempts,
                    "captured": captured_result.as_dict() if captured_result is not None else None,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0 if captured_result is not None else 2

    if command == "integration-live-telegram-worker":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent integration-live-telegram-worker")
        parser.add_argument("--offset", type=int, default=None)
        parser.add_argument("--timeout", type=int, default=30)
        parser.add_argument("--sleep-seconds", type=float, default=1.0)
        parser.add_argument("--max-cycles", type=int, default=None)
        parser.add_argument("--intake-db-path", default=".artifacts/telegram_intake.sqlite3")
        parser.add_argument("--fixture-path", default=None)
        parser.add_argument("--worker-name", default="telegram-live-worker")
        parser.add_argument("--skip-error-response", action="store_true")
        parsed = parser.parse_args(args[1:])

        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        intake_repository = TelegramIntakeRepository(parsed.intake_db_path)
        intake_service = TelegramPollingIntakeService(
            client=TelegramBotApiClient(token=config.telegram_bot_token),
            repository=intake_repository,
        )

        try:
            with _open_live_workflow(config=config, fixture_path=parsed.fixture_path) as (workflow, operational_store):
                worker = TelegramLiveWorker(
                    worker_name=parsed.worker_name,
                    intake_service=intake_service,
                    intake_repository=intake_repository,
                    workflow_runner=workflow,
                    poll_timeout=parsed.timeout,
                    sleep_seconds=parsed.sleep_seconds,
                    send_error_response=not parsed.skip_error_response,
                )
                reports = worker.run(offset=parsed.offset, max_cycles=parsed.max_cycles)
        except KeyboardInterrupt:
            print(
                json.dumps(
                    {
                        "mode": "live-telegram-worker",
                        "worker_name": parsed.worker_name,
                        "message": "Worker stopped by operator.",
                        "worker_state": intake_repository.list_worker_state(),
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0

        print(
            json.dumps(
                {
                    "mode": "live-telegram-worker",
                    "worker_name": parsed.worker_name,
                    "reports": [report.as_dict() for report in reports],
                    "worker_state": intake_repository.list_worker_state(),
                    "pending_messages": [
                        envelope.inbound_message_id
                        for envelope in intake_repository.load_pending_envelopes(limit=100)
                    ],
                    "workflow_threads": operational_store.fetch_workflow_runs(),
                },
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

        flow = CoupangCartAgentFlow(
            intake_service=TelegramPollingIntakeService(),
            candidate_source=DemoCandidateSource(),
            selection_service=HeuristicProductSelectionService(),
            cart_service=CoupangCartExecutor(
                page=DemoCoupangCartPage(should_fail=parsed.scenario == "cart-failure"),
                credentials=SessionCredentials(),
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
            credentials=None,
            result_store=SqliteCartResultStore(parsed.db_path or config.cart_db_path),
            verifier=_build_live_cart_verifier(config),
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
                    "chrome_profile_directory": config.coupang_chrome_profile_directory,
                    "attach_mode_requires_operator_login": True,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if command == "cart-live-inspect-session":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent cart-live-inspect-session")
        parser.add_argument("--headed", action="store_true")
        parsed = parser.parse_args(args[1:])

        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        runtime_config = replace(
            config,
            coupang_browser_headless=False if parsed.headed else config.coupang_browser_headless,
        )
        page = build_live_cart_page(runtime_config)
        try:
            try:
                session_mode = page.attach_to_logged_in_session(None)
            except Exception as exc:
                session_mode = None
                observation = page.observe(step_index=1)
                payload = {
                    "launch_mode": config.coupang_browser_launch_mode,
                    "chrome_profile_directory": config.coupang_chrome_profile_directory,
                    "attach_mode_requires_operator_login": True,
                    "session_mode": session_mode,
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                    "url": observation.url,
                    "title": observation.title,
                    "page_kind": observation.page_kind,
                    "blocker_hint": observation.blocker_hint,
                    "body_text_excerpt": observation.body_text_excerpt,
                    "interactive_elements": observation.interactive_elements,
                    "available_options": observation.available_options,
                    "add_to_cart_visible": observation.add_to_cart_visible,
                    "cart_count": observation.cart_count,
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
                return 2
            observation = page.observe(step_index=1)
            payload = {
                "launch_mode": config.coupang_browser_launch_mode,
                "chrome_profile_directory": config.coupang_chrome_profile_directory,
                "attach_mode_requires_operator_login": True,
                "session_mode": session_mode,
                "url": observation.url,
                "title": observation.title,
                "page_kind": observation.page_kind,
                "blocker_hint": observation.blocker_hint,
                "body_text_excerpt": observation.body_text_excerpt,
                "interactive_elements": observation.interactive_elements,
                "available_options": observation.available_options,
                "add_to_cart_visible": observation.add_to_cart_visible,
                "cart_count": observation.cart_count,
            }
        except Exception as exc:
            payload = {
                "launch_mode": config.coupang_browser_launch_mode,
                "chrome_profile_directory": config.coupang_chrome_profile_directory,
                "attach_mode_requires_operator_login": True,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 2
        finally:
            page.close()

        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if payload["blocker_hint"] is None else 2

    if command == "show-captured-candidates":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent show-captured-candidates")
        parser.add_argument(
            "--fixture-path",
            default=str(Path("tests/fixtures/coupang_search_onion_fixture.json")),
        )
        parser.add_argument("--item-name", default="양파")
        parsed = parser.parse_args(args[1:])
        source = CapturedCoupangFixtureCandidateSource(fixture_path=parsed.fixture_path)
        request = ShoppingRequest(
            user_id="telegram:cli-user",
            chat_id="cli-chat",
            items=[RequestedItem(name=parsed.item_name)],
            raw_text=f"{parsed.item_name} 담아줘",
            request_id="captured-fixture-demo",
        )
        result = source(request)
        print(
            json.dumps(
                {key: [asdict(candidate) for candidate in value] for key, value in result.items()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if command == "send-telegram-notification":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent send-telegram-notification")
        parser.add_argument("--chat-id", required=True)
        parser.add_argument("--scenario", choices=("success", "failure"), default="success")
        parser.add_argument("--user-id", default="telegram:cli-user")
        parser.add_argument("--database-path")
        parser.add_argument("--failure-stage", default="cart_add")
        parser.add_argument("--failure-reason", default="장바구니 담기 중 예기치 못한 오류가 발생했습니다.")
        parser.add_argument("--failure-detail", default=None)
        parsed = parser.parse_args(args[1:])

        try:
            telegram_bot_token = load_telegram_bot_token()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        notification_service = RetryingNotificationService(
            sender=TelegramSendMessageSender(
                client=TelegramBotApiClient(token=telegram_bot_token)
            ),
            max_attempts=3,
        )

        if parsed.scenario == "failure":
            payload = build_failure_notification_payload(
                chat_id=parsed.chat_id,
                stage=parsed.failure_stage,
                reason=parsed.failure_reason,
                detail=parsed.failure_detail,
            )
        else:
            contract_demo = demo_contract_payload()
            cart_result_data = contract_demo["cart_add_result"]
            selected = cart_result_data["selected_product"]
            candidate = selected["candidate"]
            demo_results = [
                build_demo_cart_result(
                    product_id=str(candidate["product_id"]),
                    name=str(candidate["name"]),
                    price_krw=int(candidate["price_krw"]),
                    quantity=int(selected["quantity"]),
                )
            ]
            cart_snapshot_items = None
            prior_purchases = None
            if parsed.database_path:
                context_store = SQLiteNotificationContextStore(database_path=parsed.database_path)
                notification_context = context_store.load(user_id=parsed.user_id)
                cart_snapshot_items = notification_context["cart_snapshot_items"]
                prior_purchases = notification_context["prior_purchases"]
            payload = build_success_notification_payload(
                chat_id=parsed.chat_id,
                cart_results=demo_results,
                cart_snapshot_items=cart_snapshot_items,
                prior_purchases=prior_purchases,
            )

        notification_service.send(payload)
        print(json.dumps(asdict(payload), ensure_ascii=False, indent=2, default=str))
        return 0

    if command == "integration-live-request":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent integration-live-request")
        parser.add_argument("text")
        parser.add_argument("--user-id", default="telegram:cli-user")
        parser.add_argument("--chat-id", default="cli-chat")
        parser.add_argument("--fixture-path", default=None)
        parser.add_argument("--thread-id", default=None)
        parsed = parser.parse_args(args[1:])

        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        intake_service = TelegramPollingIntakeService()
        request = intake_service.parse_demo_message(
            user_id=parsed.user_id,
            chat_id=parsed.chat_id,
            text=parsed.text,
        )
        envelope = _build_synthetic_live_envelope(request)
        with _open_live_workflow(config=config, fixture_path=parsed.fixture_path) as (workflow, operational_store):
            result = workflow.run_envelope(envelope, thread_id=parsed.thread_id)
            thread_id = parsed.thread_id or envelope.session.session_id
            persisted_state = workflow.get_persisted_state(thread_id=thread_id)

        print(
            json.dumps(
                {
                    "mode": "live-request",
                    "thread_id": thread_id,
                    "candidate_source_mode": persisted_state.get("candidate_source_mode"),
                    "result": result.as_dict(),
                    "persisted_state_keys": sorted(persisted_state.keys()),
                    "thread_context": operational_store.load_thread_context(thread_id=thread_id),
                    "workflow_runs": operational_store.fetch_workflow_runs(),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0 if result.success else 2

    if command == "integration-live-telegram-once":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent integration-live-telegram-once")
        parser.add_argument("--offset", type=int, default=None)
        parser.add_argument("--timeout", type=int, default=10)
        parser.add_argument("--intake-db-path", default=".artifacts/telegram_intake.sqlite3")
        parser.add_argument("--fixture-path", default=None)
        parser.add_argument("--skip-error-response", action="store_true")
        parsed = parser.parse_args(args[1:])

        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        intake_service = _build_live_intake_service(
            token=config.telegram_bot_token,
            db_path=parsed.intake_db_path,
        )
        intake_results = intake_service.poll_once(
            offset=parsed.offset,
            timeout=parsed.timeout,
            mode=IntakeMode.LIVE,
            send_error_response=not parsed.skip_error_response,
        )
        first_parsed = next((result for result in intake_results if result.envelope is not None), None)
        if first_parsed is None or first_parsed.envelope is None:
            print(
                json.dumps(
                    {
                        "mode": "live-telegram-once",
                        "intake_results": [result.as_dict() for result in intake_results],
                        "message": "No parseable Telegram requests were captured.",
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 2

        envelope = first_parsed.envelope
        with _open_live_workflow(config=config, fixture_path=parsed.fixture_path) as (workflow, operational_store):
            result = workflow.run_envelope(envelope)
            persisted_state = workflow.get_persisted_state(thread_id=envelope.session.session_id)

        print(
            json.dumps(
                {
                    "mode": "live-telegram-once",
                    "candidate_source_mode": persisted_state.get("candidate_source_mode"),
                    "intake": first_parsed.as_dict(),
                    "result": result.as_dict(),
                    "persisted_state_keys": sorted(persisted_state.keys()),
                    "thread_context": operational_store.load_thread_context(thread_id=envelope.session.session_id),
                    "workflow_runs": operational_store.fetch_workflow_runs(),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0 if result.success else 2

    if command == "serve-http":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent serve-http")
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=8080)
        parser.add_argument("--postgres-dsn", default=None)
        parsed = parser.parse_args(args[1:])

        db_healthcheck = None
        if parsed.postgres_dsn:
            store = PostgresOperationalStore(parsed.postgres_dsn)
            store.setup()
            db_healthcheck = store.ping
        server = CoupangCartAgentHttpServer(
            host=parsed.host,
            port=parsed.port,
            db_healthcheck=db_healthcheck,
        )
        server.serve_forever()
        return 0

    print(
        "Usage: python -m coupang_cart_agent "
        "[contracts-example|check-config|parse-telegram-message|poll-telegram-once|capture-telegram-live-request|integration-demo|cart-live-add|cart-live-inspect-session|show-captured-candidates|send-telegram-notification|integration-live-request|integration-live-telegram-once|integration-live-telegram-worker|serve-http]",
        file=sys.stderr,
    )
    return 1


def build_demo_cart_result(
    *,
    product_id: str,
    name: str,
    price_krw: int,
    quantity: int,
) -> CartAddResult:
    selected_product = SelectedProduct(
        request_item_name=name,
        candidate=ProductCandidate(
            product_id=product_id,
            name=name,
            price_krw=price_krw,
            rating=4.8,
            review_count=1200,
            product_url=f"https://www.coupang.com/vp/products/{product_id}",
        ),
        quantity=quantity,
        selection_reason="Balanced rating, reviews, and price.",
        score=8.4,
    )
    return CartAddResult(
        success=True,
        cart_item_id=f"cart-{product_id}",
        selected_product=selected_product,
        stage=CartAddStage.ADD_TO_CART,
        message="Item added to cart.",
    )
