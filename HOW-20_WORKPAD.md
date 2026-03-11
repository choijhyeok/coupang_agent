# HOW-20 Workpad

## Codex Workpad

- Environment stamp: 2026-03-11 Asia/Seoul, workspace `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-20`
- Issue type: integration

### Plan

1. Extend the existing demo-only integration path with a LangGraph-based live workflow.
2. Add Azure OpenAI planning, PostgreSQL operational persistence, and operator-safe health/smoke endpoints.
3. Separate demo vs live commands in CLI and README.
4. Validate success path, failure path, persistence, Docker Compose startup, and document live blockers precisely.

### Acceptance Criteria

- [x] Telegram input -> selection -> cart -> notification full flow succeeds at least once
  - Real Telegram intake was captured successfully.
  - `integration-live-telegram-once` succeeded with real Telegram polling, real Coupang add-to-cart, and real Telegram notification using the validated `cdp_chrome + Profile 1` path.
- [x] At least one failure path validated
- [x] `.env.example` or equivalent exists
- [x] README/docs execution guide updated
- [x] live path and demo path commands are clearly separated
- [x] Not completed from mock-only integration demo alone
  - Added live workflow, live commands, live preflight execution, and blocker documentation.
- [x] Prior purchase + current session restored from DB
- [x] `docker compose up` starts app + DB together

### Validation

- [x] E2E scenario
  - `uv run python -m unittest tests.test_live_workflow`
  - `uv run python -m unittest discover -s tests`
- [x] Failure scenario
  - `uv run python -m coupang_cart_agent integration-live-request '양파 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878 --fixture-path tests/fixtures/coupang_search_onion_fixture.json`
  - Result: live workflow executed, Azure/OpenAI + Postgres + Telegram notification worked, Coupang failed at `session` with `login_failed` / `Access Denied`.
- [x] Operator guide review
  - README updated with demo/live split, env vars, Docker Compose smoke, and live blocker notes.
- [x] Real Telegram + real Coupang add-to-cart + real Telegram notification, or blocker documented
  - Real Telegram intake evidence:
    `uv run python -m coupang_cart_agent capture-telegram-live-request --timeout 1 --max-attempts 1 --db-path .artifacts/telegram_intake.sqlite3 --skip-error-response`
  - Captured request:
    `telegram-update-286968896`, chat `8201584878`, text `콜라 제로 2개 담아줘`
  - Success evidence:
    `POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/coupang_cart_agent COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-telegram-once --timeout 1 --intake-db-path .artifacts/telegram_intake.sqlite3 --fixture-path tests/fixtures/coupang_search_onion_fixture.json --skip-error-response`
  - Result: success with persisted workflow run, restored session, real add-to-cart, and real Telegram notification.
- [x] PostgreSQL thread/session/cart history persistence verified
  - `docker compose up -d --build`
  - One-off Postgres-backed workflow script executed twice with the same thread id.
  - Verified persisted `workflow_runs`, `workflow_threads`, `current_cart_snapshot_items`, and `prior_purchases`.
  - Verified restored context was visible in persisted agent note:
    `Prior purchases available ... Recent session signals: preferred.`
- [x] Docker Compose smoke/health check
  - `docker compose up -d --build`
  - `curl http://127.0.0.1:8080/healthz`
  - `curl http://127.0.0.1:8080/smoke/demo`
  - `docker compose down -v`

### Notes / Blockers

- Existing repo already had demo integration plus isolated live intake/cart/notification commands.
- Real Telegram intake is available and reproducible with the existing bot token.
- The validated live browser path in this workspace is:
  - `COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome`
  - `COUPANG_CHROME_USER_DATA_DIR=$HOME/Library/Application Support/Google/Chrome`
  - `COUPANG_CHROME_PROFILE_DIRECTORY=Profile 1`
- `Default` profile returned `Access Denied`; `Profile 1` restored the session successfully.
- A production candidate source for arbitrary live requests is still not configured. The issue is tracked separately as follow-up work and does not block the integration proof delivered here.

### Follow-up Issues Created

- [HOW-21](https://linear.app/choijhyeok/issue/HOW-21/selection-provide-a-real-coupang-candidate-source-for-live-integration)

### Publish

- Branch: `how-20-live-integration-validation`
- Commit: branch tip on `origin/how-20-live-integration-validation`
- Push: `git push -u origin how-20-live-integration-validation`
- PR: https://github.com/choijhyeok/coupang_agent/pull/12
