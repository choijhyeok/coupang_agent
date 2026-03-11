# Coupang Cart Agent

Telegram shopping requests flow into product selection, Coupang add-to-cart automation, and Telegram notifications. This workspace keeps the module contracts stable while adding a production-shaped integration path with LangGraph state persistence, Azure OpenAI planning, and PostgreSQL operational storage.

## Project Layout

```text
.
├── coupang_cart_agent/
│   ├── azure_openai.py
│   ├── cart_adapters.py
│   ├── cart_executor.py
│   ├── cart_persistence.py
│   ├── candidate_sources.py
│   ├── cli.py
│   ├── config.py
│   ├── contracts.py
│   ├── http_server.py
│   ├── integration.py
│   ├── live_workflow.py
│   ├── notifications.py
│   ├── postgres_store.py
│   ├── selection.py
│   ├── selection_context.py
│   ├── services.py
│   ├── telegram_intake.py
│   └── telegram_persistence.py
├── docker-compose.yml
├── Dockerfile
├── main.py
├── pyproject.toml
└── tests/
```

## Runtime Modes

### Demo Path

Safe local validation. Uses deterministic candidates, a fake Coupang page, and a local notification sender.

```bash
uv run python -m coupang_cart_agent integration-demo "콜라 제로 2개 담아줘" --scenario success
uv run python -m coupang_cart_agent integration-demo "삼다수 1박스 담아줘" --scenario cart-failure
```

This path is useful for local smoke checks only. It is not sufficient for live completion.

### Live Path

Production-shaped integration. Uses:

- Telegram Bot API intake
- LangGraph workflow checkpoints in PostgreSQL
- Azure OpenAI planning node
- heuristic product selection
- real Coupang cart automation
- real Telegram notifications

Primary live commands:

```bash
uv run python -m coupang_cart_agent integration-live-request \
  "양파 1개 담아줘" \
  --user-id telegram:cli-user \
  --chat-id cli-chat

uv run python -m coupang_cart_agent integration-live-telegram-once \
  --timeout 10 \
  --intake-db-path .artifacts/telegram_intake.sqlite3

uv run python -m coupang_cart_agent integration-live-telegram-worker \
  --timeout 30 \
  --sleep-seconds 1 \
  --intake-db-path .artifacts/telegram_intake.sqlite3
```

`integration-live-request` bypasses Telegram network polling but still runs the LangGraph, Azure OpenAI, PostgreSQL, Coupang, and Telegram-notification path.

`integration-live-telegram-once` is the operator command for the full path:

1. poll Telegram once
2. parse the first valid request
3. run the live LangGraph workflow
4. add the selected product to the Coupang cart
5. send a Telegram notification
6. persist workflow state and cart/session history to PostgreSQL

`integration-live-telegram-worker` is the always-on operator path:

1. restore the previous `next_offset` and any pending envelopes from the Telegram intake SQLite DB
2. long-poll Telegram for new `~~~ 담아줘` messages
3. persist each parsed envelope before workflow execution
4. run the LangGraph live workflow for each pending envelope
5. persist worker cursor and per-message completion state so restarts resume safely

## Environment

Create `.env` from the example:

```bash
cp .env.example .env
```

Required for the live workflow:

- `TELEGRAM_BOT_TOKEN`
- `COUPANG_USERNAME`
- `COUPANG_PASSWORD`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT`
- `POSTGRES_DSN`

Required for real candidate lookup:

- `COUPANG_SEARCH_ENDPOINT`

If `COUPANG_SEARCH_ENDPOINT` is not available, you can pass `--fixture-path` to the live commands for operator preflight only:

```bash
uv run python -m coupang_cart_agent integration-live-request \
  "양파 1개 담아줘" \
  --fixture-path tests/fixtures/coupang_search_onion_fixture.json
```

That preflight path still uses LangGraph, PostgreSQL, Azure OpenAI, Coupang cart automation, and Telegram notifications, but it does not satisfy a real live candidate-fetch validation by itself.

Validated live browser path on March 11, 2026:

```bash
COUPANG_BROWSER_LAUNCH_MODE=browser_use
COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome"
COUPANG_CHROME_PROFILE_DIRECTORY="Profile 1"
```

The `browser_use` mode is the preferred live path in this repository. It uses a copied real Chrome profile over CDP so the worker runs with an operator-approved session instead of a fresh Playwright context.

`Default` returned `Access Denied` in this workspace. `Profile 1` restored the authenticated Coupang session and completed add-to-cart successfully. Keep `playwright` only as a debugging fallback for selector work, not as the primary live path.

## Docker Compose

`docker compose up` starts a PostgreSQL container and an app container exposing health and demo smoke endpoints.

```bash
docker compose up -d --build
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/smoke/demo
```

The compose app runs:

```bash
uv run python -m coupang_cart_agent serve-http --host 0.0.0.0 --port 8080
```

The HTTP server is for health and smoke validation. Operators should run the live integration commands explicitly when real credentials and access are available.

Draft live worker service:

```bash
COUPANG_CHROME_USER_DATA_DIR_HOST="$HOME/Library/Application Support/Google/Chrome" \
docker compose --profile live up -d worker
```

The `worker` service expects a trusted Chrome profile bind-mounted from the host into `/operator-chrome`, and it reuses `.artifacts/telegram_intake.sqlite3` plus PostgreSQL state so restarts can resume from the last processed Telegram offset.

## Other Commands

```bash
uv run python -m coupang_cart_agent check-config
uv run python -m coupang_cart_agent parse-telegram-message "콜라 제로 2개 담아줘"
uv run python -m coupang_cart_agent poll-telegram-once --timeout 1
uv run python -m coupang_cart_agent capture-telegram-live-request --timeout 30 --max-attempts 10
uv run python -m coupang_cart_agent integration-live-telegram-worker --timeout 30 --sleep-seconds 1
uv run python -m coupang_cart_agent cart-live-add --headed --product-url "https://www.coupang.com/vp/products/..." --product-id "..." --name "..."
uv run python -m coupang_cart_agent send-telegram-notification --chat-id <chat_id> --scenario success
```

## LangGraph Live Workflow

The live workflow is defined in `coupang_cart_agent/live_workflow.py`.

Node order:

1. `load_context`
2. `agent_plan`
3. `load_candidates`
4. `select_products`
5. `add_to_cart`
6. `notify`
7. `persist`

State is checkpointed through LangGraph's PostgreSQL checkpointer using `thread_id = envelope.session.session_id`.

Operational data is stored separately in PostgreSQL:

- `workflow_threads`
- `workflow_runs`
- `prior_purchases`
- `recent_session_signals`
- `current_cart_snapshot_items`

That lets the workflow restore both prior purchase history and current-thread session signals on subsequent runs.

## Validation

Unit and integration tests:

```bash
uv run python -m unittest discover -s tests
```

Focused live-workflow tests:

```bash
uv run python -m unittest tests.test_live_workflow
```

Docker smoke:

```bash
docker compose up -d --build
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/smoke/demo
docker compose down -v
```

Suggested live operator validation order:

```bash
uv run python -m coupang_cart_agent check-config
uv run python -m coupang_cart_agent integration-live-request "양파 1개 담아줘" --fixture-path tests/fixtures/coupang_search_onion_fixture.json
uv run python -m coupang_cart_agent integration-live-telegram-once --timeout 10
uv run python -m coupang_cart_agent integration-live-telegram-worker --timeout 30 --sleep-seconds 1
```

Recorded live validation evidence on March 11, 2026:

- Real Telegram intake capture succeeded for `telegram-update-286968896` with text `콜라 제로 2개 담아줘`
- Real Telegram worker execution succeeded with:
  `POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/coupang_cart_agent COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-telegram-worker --timeout 1 --sleep-seconds 0 --max-cycles 1 --intake-db-path .artifacts/how22_telegram_intake.sqlite3 --fixture-path tests/fixtures/coupang_search_onion_fixture.json --skip-error-response`
- Worker restart restored offset `286968897` from `.artifacts/how22_telegram_intake.sqlite3` and resumed with no duplicate processing on a second `integration-live-telegram-worker` run.
- Telegram success notification payload recorded `총 1종, 2개, 17,960원 장바구니 담기 완료`.
- The remaining gap is a production candidate source for arbitrary live requests, tracked separately in `HOW-21`

## Notes

- The workflow stops at verified add-to-cart. It must not continue into checkout or payment.
- `cart-live-add` remains useful for isolated Coupang selector debugging.
- `integration-demo` and `/smoke/demo` are safe local validation paths.
- `integration-live-*` commands are the production-shaped integration paths.
