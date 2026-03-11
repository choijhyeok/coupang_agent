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

- [ ] Telegram input -> selection -> cart -> notification full flow succeeds at least once
  - Real Telegram intake was captured successfully.
  - Real live add-to-cart did not complete because Coupang returned `Access Denied` during session establishment.
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
  - Real live completion blocker 1:
    `integration-live-telegram-once` fails before selection/cart because `COUPANG_SEARCH_ENDPOINT` is not configured.
  - Real live completion blocker 2:
    direct live request with real Azure/Telegram/Coupang session setup reaches cart executor but fails with `Coupang blocked the automated browser session with Access Denied.`
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
- Live candidate fetch still depends on a real `COUPANG_SEARCH_ENDPOINT` or an explicit fixture override for preflight.
- Full live completion remains blocked by two concrete issues:
  - Missing production candidate-source endpoint/config for arbitrary Telegram requests
  - Coupang rejects the automated browser session with `Access Denied` even on the existing `cdp_chrome` path in this environment

### Follow-up Issues Created

- [HOW-21](https://linear.app/choijhyeok/issue/HOW-21/selection-provide-a-real-coupang-candidate-source-for-live-integration)

### Publish

- Branch: `how-20-live-integration-validation`
- Commit: `895c5a0cd376ac576869ec511ea5915ed07ffe9e`
- Push: `git push -u origin how-20-live-integration-validation`
- PR: https://github.com/choijhyeok/coupang_agent/pull/12
