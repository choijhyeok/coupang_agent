# Coupang Cart Agent

Telegram shopping requests flow into an AOAI-guided live browser shopping agent, Coupang add-to-cart automation, and Telegram notifications. This workspace keeps the module contracts stable while adding a production-shaped integration path with LangGraph state persistence, Azure OpenAI planning/decision nodes, and PostgreSQL operational storage.

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
│   ├── live_browser_agent.py
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
- Azure OpenAI planning node plus constrained browser-action decisions
- observation-driven browser agent for search -> result selection -> option handling -> add-to-cart
- candidate-source fallback only for preflight/debugging
- real Coupang cart automation on an attached logged-in Chrome session
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
4. let the AOAI browser agent search Coupang live, choose a product, and add it to the cart
5. send a Telegram notification
6. persist workflow state, agent reasoning summary, last observation, and cart/session history to PostgreSQL

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
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT`
- `POSTGRES_DSN`

Optional fallback-only preflight input:

- `COUPANG_SEARCH_ENDPOINT`

If you want to force the old candidate-source fallback for operator preflight or debugging, you can pass `--fixture-path` to the live commands:

```bash
uv run python -m coupang_cart_agent integration-live-request \
  "양파 1개 담아줘" \
  --fixture-path tests/fixtures/coupang_search_onion_fixture.json
```

That fallback path still uses LangGraph, PostgreSQL, Azure OpenAI, Coupang cart automation, and Telegram notifications, but the primary live path no longer depends on a prepared product URL, product ID, or candidate fixture.

Validated live browser path on March 11, 2026:

```bash
COUPANG_BROWSER_LAUNCH_MODE=browser_use
COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome"
COUPANG_CHROME_PROFILE_DIRECTORY="Profile 1"
```

The `browser_use` mode is the preferred live path in this repository. It uses a copied real Chrome profile over CDP so the worker runs with an operator-approved session instead of a fresh Playwright context.

`Default` returned `Access Denied` in this workspace. `Profile 1` restored the authenticated Coupang session and completed add-to-cart successfully. Keep `playwright` only as a debugging fallback for selector work, not as the primary live path.

Supported attach launch modes:

- `browser_use`: copy a trusted local Chrome profile and run the agent against that attached profile
- `cdp_chrome`: launch a copied Chrome profile under CDP from the agent process
- `existing_cdp`: connect to an already running operator Chrome started with `--remote-debugging-port`

### Attach Mode Operating Rules

The live cart automation runs in attach mode only:

1. A human operator must open Chrome and complete Coupang login manually before starting the agent.
2. The agent attaches to the already logged-in Chrome profile or another explicitly allowed session state.
3. The agent does not fill the Coupang login form, handle OTP, or bypass security checks.
4. If Coupang redirects to login, shows `Access Denied`, or presents a security challenge, the run stops immediately and records a blocker-classified cart result.
5. If a product is sold out or the visible option state is ambiguous, the agent stops safely and records a classified failure instead of guessing.
6. The workflow stops after verified add-to-cart and must not continue into checkout or payment.

Recommended operator preflight:

```bash
uv run python -m coupang_cart_agent check-config
uv run python -m coupang_cart_agent cart-live-inspect-session
uv run python -m coupang_cart_agent cart-live-add \
  --headed \
  --product-url "https://www.coupang.com/vp/products/..." \
  --product-id "..." \
  --name "..." \
  --quantity 1
```

`cart-live-add` prints `attach_mode_requires_operator_login: true` and the active Chrome profile directory so operators can confirm the run started from an already prepared session.

Interpret `cart-live-inspect-session` before attempting a live run:

- `error_type=LoginFailedError`: no reachable CDP endpoint or attachable browser process for the selected mode
- `error_type=LoginRequiredError` with `page_kind=session_blocked`: the browser session is reachable, but Coupang cart still shows an unauthenticated state such as `로그인하기`
- `error_type=AccessDeniedError` or `SecurityChallengeError`: stop and re-prepare the session; do not retry the shopping run blindly
- no error and a non-blocked observation: the browser looks attachable enough to attempt a live search-to-cart run

The current workspace produced both of the first two cases:

- `existing_cdp` against an unused port returned a structured `LoginFailedError`
- `existing_cdp` against a reachable local Chrome session on port `9226` attached successfully, reached `https://cart.coupang.com/cartView.pang`, and still returned `LoginRequiredError` / `session_blocked`

That distinction matters operationally: CDP reachability alone does not prove the attached Chrome session is actually logged in to Coupang for cart actions.

If the operator wants to attach to the exact running Chrome session instead of a copied profile, start Chrome manually with remote debugging enabled and switch the launch mode:

```bash
/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \
  --remote-debugging-port=9223

COUPANG_BROWSER_LAUNCH_MODE=existing_cdp \
COUPANG_CHROME_REMOTE_DEBUGGING_PORT=9223 \
uv run python -m coupang_cart_agent cart-live-add \
  --product-url "https://www.coupang.com/vp/products/..." \
  --product-id "..." \
  --name "..."
```

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
uv run python -m coupang_cart_agent cart-live-inspect-session
uv run python -m coupang_cart_agent cart-live-add --headed --product-url "https://www.coupang.com/vp/products/..." --product-id "..." --name "..."
uv run python -m coupang_cart_agent send-telegram-notification --chat-id <chat_id> --scenario success
```

## LangGraph Live Workflow

The live workflow is defined in `coupang_cart_agent/live_workflow.py`.

Node order:

1. `load_context`
2. `agent_plan`
3. `browser_shop`
4. `load_candidates` fallback only
5. `select_products` fallback only
6. `add_to_cart` fallback only
7. `notify`
8. `persist`

State is checkpointed through LangGraph's PostgreSQL checkpointer using `thread_id = envelope.session.session_id`.

Operational data is stored separately in PostgreSQL:

- `workflow_threads`
- `workflow_runs`
- `prior_purchases`
- `recent_session_signals`
- `current_cart_snapshot_items`

`workflow_runs` now stores the agent plan, reasoning summary, last observation snapshot, and step trace alongside cart results. That lets the workflow restore both prior purchase history and current-thread session signals on subsequent runs.

For live browser-agent failures, the stored observation now explicitly distinguishes:

- `session_blocked`: login page, unauthenticated cart state, Access Denied, or security challenge
- `search_results`: reachable search page with visible product candidates
- `product_page`: reachable PDP or option-selection state

## Validation

Unit and integration tests:

```bash
uv run python -m unittest discover -s tests
```

Focused live-workflow tests:

```bash
uv run python -m unittest tests.test_live_browser_agent tests.test_live_workflow
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
uv run python -m coupang_cart_agent integration-live-request "양파 1개 담아줘"
uv run python -m coupang_cart_agent integration-live-telegram-once --timeout 10
uv run python -m coupang_cart_agent integration-live-telegram-worker --timeout 30 --sleep-seconds 1
```

Recorded live validation evidence on March 11, 2026:

- Real Telegram intake capture succeeded for `telegram-update-286968896` with text `콜라 제로 2개 담아줘`
- Real Telegram worker execution succeeded with:
  `POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/coupang_cart_agent COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-telegram-worker --timeout 1 --sleep-seconds 0 --max-cycles 1 --intake-db-path .artifacts/how22_telegram_intake.sqlite3 --fixture-path tests/fixtures/coupang_search_onion_fixture.json --skip-error-response`
- Worker restart restored offset `286968897` from `.artifacts/how22_telegram_intake.sqlite3` and resumed with no duplicate processing on a second `integration-live-telegram-worker` run.
- Telegram success notification payload recorded `총 1종, 2개, 17,960원 장바구니 담기 완료`.
- Fresh AOAI browser-agent live validation is still required after login/session access is prepared for the current workspace.

## Notes

- The workflow stops at verified add-to-cart. It must not continue into checkout or payment.
- `COUPANG_USERNAME` and `COUPANG_PASSWORD` are optional legacy fields and are not used by the attach-mode live path.
- `cart-live-inspect-session` is the fastest operator command to confirm whether the attached browser is really logged in, blocked by Access Denied/security, or simply missing a reachable CDP endpoint.
- A reachable CDP session can still be unusable if Coupang cart renders `로그인하기`; treat that as a session/auth blocker, not a selector problem.
- `cart-live-add` remains useful for isolated Coupang selector debugging.
- `integration-demo` and `/smoke/demo` are safe local validation paths.
- `integration-live-*` commands are the production-shaped integration paths.
- `--fixture-path` and `COUPANG_SEARCH_ENDPOINT` are fallback/debug inputs, not the primary live search path anymore.
