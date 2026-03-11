# Coupang Cart Agent

Shared modules for a Telegram-driven Coupang cart agent. The repository is organized so intake, selection, cart automation, notifications, and integration can evolve on separate branches without redesigning shared contracts.

## Project Layout

```text
.
├── coupang_cart_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cart_executor.py
│   ├── candidate_sources.py
│   ├── cli.py
│   ├── config.py
│   ├── contracts.py
│   ├── integration.py
│   ├── notifications.py
│   ├── selection.py
│   ├── selection_context.py
│   ├── services.py
│   └── telegram_intake.py
├── main.py
├── pyproject.toml
└── tests/
    ├── test_foundation.py
    ├── test_integration.py
    ├── test_selection.py
    └── test_telegram_intake.py
```

## Quick Start

1. Install dependencies:

   ```bash
   uv sync
   ```

2. Copy the example environment file and fill in required values:

   ```bash
   cp .env.example .env
   ```

   `load_config()` reads `.env` first and lets explicit process environment variables override those values.
   Use placeholders only for local validation. Do not commit real credentials.

3. Run the shared contracts example:

   ```bash
   uv run python -m coupang_cart_agent contracts-example
   ```

4. Validate config loading:

   ```bash
   env -i PATH="$PATH" HOME="$HOME" UV_CACHE_DIR=.uv-cache uv run python -m coupang_cart_agent check-config
   ```

## Commands

- `uv run python -m coupang_cart_agent contracts-example`
  Prints a sample request, selected product, cart result, and notification payload.
- `uv run python -m coupang_cart_agent check-config`
  Validates required environment variables and prints a clear error when config is incomplete.
- `uv run python -m coupang_cart_agent parse-telegram-message "콜라 제로 2개 담아줘"`
  Parses a Telegram-style shopping request into the shared `ShoppingRequest` contract.
- `uv run python -m coupang_cart_agent poll-telegram-once --timeout 1`
  Uses Telegram Bot API long polling once, persists inbound/session records, and prints live intake envelopes or user-facing errors.
- `uv run python -m coupang_cart_agent capture-telegram-live-request --timeout 30 --max-attempts 10`
  Repeats live polling until the first real Telegram update is captured or attempts are exhausted, printing a validation-friendly evidence payload.
- `uv run python -m coupang_cart_agent integration-demo "콜라 제로 2개 담아줘" --scenario success`
  Runs a local end-to-end proof across intake, selection, cart execution, and notification with deterministic demo doubles.
- `uv run python -m coupang_cart_agent integration-demo "삼다수 1박스 담아줘" --scenario cart-failure`
  Exercises a failure path that stops before checkout and emits a failure notification.
- `uv run python -m coupang_cart_agent show-captured-candidates --item-name 양파`
  Loads a captured production-shaped candidate fixture and prints normalized candidates.
- `uv run python -m coupang_cart_agent send-telegram-notification --chat-id <chat_id> --scenario success --database-path <sqlite_db>`
  Sends a real Telegram success or failure message using the live `sendMessage` path. When `--database-path` is provided, the success payload reads `current_cart_snapshot_items` and `prior_purchases` from SQLite to compose the reply.
- `uv run python main.py contracts-example`
  Root entrypoint wrapper for local execution.

## Shared Contracts

The following contracts are shared across modules:

- `RequestedItem`
- `ShoppingRequest`
- `RequestSession`
- `ShoppingRequestEnvelope`
- `ProductCandidate`
- `SelectedProduct`
- `CartAddResult`
- `NotificationPayload`

See [coupang_cart_agent/contracts.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-13/coupang_cart_agent/contracts.py) for field-level definitions.

## Service Interfaces

Downstream modules should implement the protocols in [coupang_cart_agent/services.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-13/coupang_cart_agent/services.py):

- `TelegramIntakeService`
- `ProductSelectionService`
- `CoupangCartService`
- `NotificationService`

## Telegram Intake

[coupang_cart_agent/telegram_intake.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-13/coupang_cart_agent/telegram_intake.py) provides a production-shaped intake implementation for HOW-8.

- `TelegramBotApiClient`: minimal Telegram Bot API client using long polling.
- `TelegramPollingIntakeService`: keeps the demo parser path separate from the production long-polling path, parses `... 담아줘` messages, persists inbound/session records, and returns a LangGraph-ready `ShoppingRequestEnvelope`.
- `TelegramIntakeRepository`: SQLite-backed storage for Telegram session and inbound request records.

Supported parsing rules:

- quantity units such as `개`, `병`, `팩`, `박스`
- optional constraints through parentheses or `옵션:` / `조건:`
- optional price cap such as `20000원 이하`
- multiple requested items separated by newlines, `;`, or `그리고`

Live intake persistence stores:

- stable `session_id` linked to `user_id` and `chat_id`
- inbound message rows with parse status, raw update payload, and normalized `request_id`
- enough envelope metadata to hand the request into LangGraph state without reshaping

## Selection Engine

[coupang_cart_agent/selection.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-16/coupang_cart_agent/selection.py) exposes a pure `select_best_product()` helper and a protocol-compatible `HeuristicProductSelectionService` that scores candidates by rating, review count, relative price, prior purchase history, and recent session context when a store is provided.

[coupang_cart_agent/candidate_sources.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-16/coupang_cart_agent/candidate_sources.py) separates deterministic demo candidates from production-shaped sources:

- `DemoCandidateSource` for safe local end-to-end demos
- `CapturedCoupangFixtureCandidateSource` for captured repo fixtures
- `LiveCoupangSearchCandidateSource` for live collector or Scrapling-equivalent JSON output

[coupang_cart_agent/selection_context.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-16/coupang_cart_agent/selection_context.py) defines the DB read path for prior purchases and recent session signals, including an SQLite-backed store used by tests.

## Notifications

The telegram notification module lives in [coupang_cart_agent/notifications.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-13/coupang_cart_agent/notifications.py).
It exposes payload builders aligned with `NotificationPayload`, a bounded formatter for concise success and failure messages, a Telegram `sendMessage` sender adapter, a retrying delivery service, and a SQLite-backed context reader for `current_cart_snapshot_items` plus `prior_purchases`.

## Cart Automation Module

[coupang_cart_agent/cart_executor.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-13/coupang_cart_agent/cart_executor.py) consumes `SelectedProduct` inputs and returns `CartAddResult` while stopping at add-to-cart.

## Integration Flow

[coupang_cart_agent/integration.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-13/coupang_cart_agent/integration.py) connects the existing modules without changing their shared interfaces.

- Input: Telegram-style request text
- Pipeline: parse request -> load candidates -> score/select -> add to cart -> send notification
- Boundary: any failure stops the flow and sends a concise failure notification
- Safety: cart automation stops at add-to-cart and treats checkout as a failure condition

## Operator Run Guide

1. Install dependencies and prepare `.env`.
2. Validate configuration:

   ```bash
   env -i PATH="$PATH" HOME="$HOME" UV_CACHE_DIR=.uv-cache uv run python -m coupang_cart_agent check-config
   ```

3. Run the deterministic end-to-end success proof:

   ```bash
   uv run python -m coupang_cart_agent integration-demo "콜라 제로 2개 담아줘" --scenario success
   ```

4. Run the deterministic failure proof:

   ```bash
   uv run python -m coupang_cart_agent integration-demo "삼다수 1박스 담아줘" --scenario cart-failure
   ```

5. Run the automated validation suite:

   ```bash
   uv run python -m unittest discover -s tests
   ```

The demo command is safe for local verification because it uses fake candidate lookup, fake cart page interactions, and a local notification sender. It never reaches real checkout or payment.

## Validation

Run:

```bash
uv run python -m unittest discover -s tests
```

Additional module proofs:

```bash
uv run python -m coupang_cart_agent parse-telegram-message "삼다수 2L 1박스 옵션: 무라벨 담아줘"
uv run python -m coupang_cart_agent poll-telegram-once --timeout 1 --db-path .artifacts/telegram_intake.sqlite3
uv run python -m coupang_cart_agent capture-telegram-live-request --timeout 30 --max-attempts 10 --db-path .artifacts/telegram_intake.sqlite3
uv run python -m unittest tests.test_selection
uv run python -m coupang_cart_agent show-captured-candidates --item-name 양파
```

Notification-specific validation:

```bash
uv run python -m unittest tests.test_notifications
uv run python -m coupang_cart_agent send-telegram-notification --chat-id <chat_id> --scenario failure
```

Integration-specific validation:

```bash
uv run python -m unittest tests.test_integration
```
