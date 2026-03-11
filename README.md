# Coupang Cart Agent

Shared modules for a Telegram-driven Coupang cart agent. The repository is organized so intake, selection, cart automation, notifications, and integration can evolve on separate branches without redesigning shared contracts.

## Project Layout

```text
.
├── coupang_cart_agent/
│   ├── __init__.py
│   ├── cart_adapters.py
│   ├── __main__.py
│   ├── cart_executor.py
│   ├── cart_persistence.py
│   ├── cli.py
│   ├── config.py
│   ├── contracts.py
│   ├── integration.py
│   ├── notifications.py
│   ├── selection.py
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
  Uses Telegram Bot API long polling once and prints parsed requests or user-facing errors.
- `uv run python -m coupang_cart_agent integration-demo "콜라 제로 2개 담아줘" --scenario success`
  Runs a local end-to-end proof across intake, selection, cart execution, and notification with deterministic demo doubles.
- `uv run python -m coupang_cart_agent integration-demo "삼다수 1박스 담아줘" --scenario cart-failure`
  Exercises a failure path that stops before checkout and emits a failure notification.
- `uv run python -m coupang_cart_agent cart-live-add --headed --product-url "https://www.coupang.com/vp/products/..." --product-id "..." --name "..."`
  Runs the production-shaped live cart adapter against a real Coupang product page and persists the resulting `CartAddResult` into SQLite.
- `uv run python main.py contracts-example`
  Root entrypoint wrapper for local execution.

## Shared Contracts

The following contracts are shared across modules:

- `RequestedItem`
- `ShoppingRequest`
- `ProductCandidate`
- `SelectedProduct`
- `CartAddResult`
- `NotificationPayload`

See [coupang_cart_agent/contracts.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-17/coupang_cart_agent/contracts.py) for field-level definitions.

## Service Interfaces

Downstream modules should implement the protocols in [coupang_cart_agent/services.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-17/coupang_cart_agent/services.py):

- `TelegramIntakeService`
- `ProductSelectionService`
- `CoupangCartService`
- `NotificationService`

## Telegram Intake

[coupang_cart_agent/telegram_intake.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-17/coupang_cart_agent/telegram_intake.py) provides a production-shaped intake implementation for HOW-8.

- `TelegramBotApiClient`: minimal Telegram Bot API client using long polling.
- `TelegramPollingIntakeService`: extracts Telegram updates, parses `... 담아줘` messages, and returns `ShoppingRequest` or a concise error response.

Supported parsing rules:

- quantity units such as `개`, `병`, `팩`, `박스`
- optional constraints through parentheses or `옵션:` / `조건:`
- optional price cap such as `20000원 이하`
- multiple requested items separated by newlines, `;`, or `그리고`

## Selection Engine

[coupang_cart_agent/selection.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-17/coupang_cart_agent/selection.py) exposes a pure `select_best_product()` helper and a protocol-compatible `HeuristicProductSelectionService` that scores candidates by rating, review count, and relative price.

## Notifications

The telegram notification module lives in [coupang_cart_agent/notifications.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-17/coupang_cart_agent/notifications.py).
It exposes payload builders aligned with `NotificationPayload`, a bounded
formatter for concise success and failure messages, and a retrying sender adapter
for transient delivery failures.

## Cart Automation Module

[coupang_cart_agent/cart_executor.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-17/coupang_cart_agent/cart_executor.py) consumes `SelectedProduct` inputs and returns `CartAddResult` while stopping at add-to-cart.

- `coupang_cart_agent/cart_adapters.py` contains the split between `DemoCoupangCartPage`, the direct `PlaywrightCoupangCartPage`, and the copied-profile `ChromeCdpCoupangCartPage`.
- `coupang_cart_agent/cart_persistence.py` persists cart add results and before/after snapshots into SQLite.

## Integration Flow

[coupang_cart_agent/integration.py](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-17/coupang_cart_agent/integration.py) connects the existing modules without changing their shared interfaces.

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

For live cart validation, use `cart-live-add` with a real Coupang product URL and inspect the JSON output plus the SQLite record at `CART_DB_PATH`.

- `COUPANG_BROWSER_LAUNCH_MODE=playwright` uses the direct Playwright-managed browser path.
- `COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome` copies an existing local Chrome profile, launches Chrome separately, and attaches over CDP. This is the validated live path for March 11, 2026 because it restored a real authenticated Coupang session where direct Playwright launches were blocked.

## Validation

Run:

```bash
uv run python -m unittest discover -s tests
```

Additional module proofs:

```bash
uv run python -m coupang_cart_agent parse-telegram-message "삼다수 2L 1박스 옵션: 무라벨 담아줘"
uv run python -m unittest tests.test_selection
```

Notification-specific validation:

```bash
uv run python -m unittest tests.test_notifications
```

Integration-specific validation:

```bash
uv run python -m unittest tests.test_integration
```

Live cart validation example:

```bash
COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome \
COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" \
COUPANG_CHROME_PROFILE_DIRECTORY="Profile 1" \
uv run python -m coupang_cart_agent cart-live-add \
  --product-url "https://www.coupang.com/vp/products/7566747125?itemId=24967111280&vendorItemId=91892104543" \
  --product-id "7566747125" \
  --name "코카콜라 제로제로, 350ml, 24개" \
  --price-krw 19940 \
  --rating 5.0 \
  --review-count 19723
```
