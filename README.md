# Coupang Cart Agent

Shared foundation for a Telegram-driven Coupang cart agent. This issue establishes the package layout, shared contracts, configuration loading, and service interfaces that downstream feature branches must consume without redesigning.

## Project Layout

```text
.
├── coupang_cart_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── contracts.py
│   ├── selection.py
│   └── services.py
├── main.py
├── pyproject.toml
└── tests/
    ├── test_foundation.py
    └── test_selection.py
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

   This runs the check in a clean environment so missing required variables are reported
   deterministically instead of being satisfied by inherited shell variables.

## Commands

- `uv run python -m coupang_cart_agent contracts-example`
  Prints a sample request, selected product, cart result, and notification payload.
- `uv run python -m coupang_cart_agent check-config`
  Validates required environment variables and prints a clear error when config is incomplete.
  For a deterministic missing-config check, prefer:

  ```bash
  env -i PATH="$PATH" HOME="$HOME" UV_CACHE_DIR=.uv-cache uv run python -m coupang_cart_agent check-config
  ```
- `uv run python main.py contracts-example`
  Root entrypoint wrapper for local execution.

## Shared Contracts

The following contracts are fixed by this foundation issue and should be consumed by downstream work:

- `RequestedItem`: one requested product with quantity and optional constraints.
- `ShoppingRequest`: Telegram/user-originated request containing multiple requested items.
- `ProductCandidate`: scraped or retrieved candidate product before final selection.
- `SelectedProduct`: chosen product with the rationale used for selection.
- `CartAddResult`: output of the Coupang cart automation stage.
- `NotificationPayload`: normalized Telegram notification message content.

See [`coupang_cart_agent/contracts.py`](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-7/coupang_cart_agent/contracts.py) for field-level definitions.

## Service Interfaces

Downstream modules should implement the protocols in [`coupang_cart_agent/services.py`](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-7/coupang_cart_agent/services.py):

- `TelegramIntakeService`
- `ProductSelectionService`
- `CoupangCartService`
- `NotificationService`

These protocols intentionally separate intake, selection, cart automation, and notification so parallel branches can implement each module independently.

## Selection Engine

The product selection module lives in [`coupang_cart_agent/selection.py`](/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-9/coupang_cart_agent/selection.py).
It exposes a pure `select_best_product()` helper and a protocol-compatible
`HeuristicProductSelectionService` that scores candidates by rating, review count,
and relative price. The heuristic intentionally penalizes suspiciously cheap,
low-confidence products so the selector does not naively choose the lowest price.

## Example Import

```python
from coupang_cart_agent.contracts import RequestedItem, ShoppingRequest

request = ShoppingRequest(
    user_id="telegram:1234",
    chat_id="1234",
    items=[RequestedItem(name="Coke Zero 355ml", quantity=2)],
    raw_text="콜라 제로 355ml 2개 담아줘",
)
```

## Validation

Run:

```bash
uv run python -m unittest discover -s tests
```

The tests cover the shared contract example and the missing-config error path. For a manual
missing-config proof that ignores inherited shell variables, use:

```bash
env -i PATH="$PATH" HOME="$HOME" UV_CACHE_DIR=.uv-cache uv run python -m coupang_cart_agent check-config
```

Selection-specific validation:

```bash
uv run python -m unittest tests.test_selection
```
