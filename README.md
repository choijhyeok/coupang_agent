# Coupang Cart Agent

Shared modules for a Telegram-driven Coupang cart agent. The repository is organized so intake, selection, cart automation, notifications, and integration can evolve on separate branches without redesigning shared contracts.

## Project Layout

```text
.
├── coupang_cart_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cart_executor.py
│   ├── cli.py
│   ├── config.py
│   ├── contracts.py
│   ├── selection.py
│   ├── services.py
│   └── telegram_intake.py
├── main.py
├── pyproject.toml
└── tests/
    ├── test_foundation.py
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

See [`coupang_cart_agent/contracts.py`](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-8/coupang_cart_agent/contracts.py) for field-level definitions.

## Service Interfaces

Downstream modules should implement the protocols in [`coupang_cart_agent/services.py`](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-8/coupang_cart_agent/services.py):

- `TelegramIntakeService`
- `ProductSelectionService`
- `CoupangCartService`
- `NotificationService`

## Telegram Intake

[`coupang_cart_agent/telegram_intake.py`](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-8/coupang_cart_agent/telegram_intake.py) provides a production-shaped intake implementation for HOW-8.

- `TelegramBotApiClient`: minimal Telegram Bot API client using long polling.
- `TelegramPollingIntakeService`: extracts Telegram updates, parses `... 담아줘` messages, and returns `ShoppingRequest` or a concise error response.

Supported parsing rules:

- quantity units such as `개`, `병`, `팩`, `박스`
- optional constraints through parentheses or `옵션:` / `조건:`
- optional price cap such as `20000원 이하`
- multiple requested items separated by newlines, `;`, or `그리고`

## Selection Engine

[`coupang_cart_agent/selection.py`](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-8/coupang_cart_agent/selection.py) exposes a pure `select_best_product()` helper and a protocol-compatible `HeuristicProductSelectionService` that scores candidates by rating, review count, and relative price.

## Cart Automation Module

[`coupang_cart_agent/cart_executor.py`](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-8/coupang_cart_agent/cart_executor.py) consumes `SelectedProduct` inputs and returns `CartAddResult` while stopping at add-to-cart.

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
