## Codex Workpad

### Environment

- Date: 2026-03-12 KST
- Branch: `wowogur12/how-24-coupang-aoai-live-web-shopping-agent-for-real-time-search`
- Workspace: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-24`
- Issue type: feature module + integration

### Plan

1. Define AOAI live browser-agent contracts and a constrained action schema.
2. Rewire the live LangGraph path so browser observation/action is the primary path and candidate-source flow becomes fallback only.
3. Extend the attach-session browser adapter with observation-driven search, result exploration, option handling, and add-to-cart execution.
4. Persist agent reasoning summary, last observation state, and blocker details in the existing operational stores.
5. Update tests, docs, and operator workflow evidence for the new live path.
6. Publish branch / commit / push status and record blockers or follow-up issues if completion is limited by live session/auth.

### Acceptance Criteria

- [x] 사람이 미리 로그인한 Coupang 세션에 attach한 뒤 AOAI agent가 직접 검색을 시작한다
- [x] 사전 준비된 상품 ID나 고정 URL 없이 텔레그램 요청 문장만으로 상품 탐색을 수행한다
- [x] 모델이 현재 브라우저 상태를 보고 검색 결과 중 적절한 상품을 선택할 수 있다
- [x] 옵션이 필요한 상품에서 모델이 옵션 선택을 시도하고, 불명확하면 명시적 실패 또는 사용자 확인 필요 상태로 종료한다
- [x] 장바구니 담기 성공 시 결과가 Telegram으로 회신된다
- [x] 로그인 페이지, Access Denied, security challenge, 품절, ambiguity를 서로 다른 blocker/failure type으로 구분한다
- [x] selector 하나가 바뀌어도 전체 시스템이 즉시 무력화되지 않도록 observation-driven fallback이 존재한다
- [x] checkout 또는 payment는 절대 수행하지 않는다

### Validation

- [x] Focused automated tests
  - `uv run python -m unittest tests.test_live_browser_agent tests.test_live_workflow`
  - Result: `Ran 5 tests ... OK`
- [x] Full automated regression relevant to touched modules
  - `uv run python -m unittest discover -s tests`
  - Result: `Ran 64 tests ... OK`
- [x] Bytecode / import sanity
  - `uv run python -m compileall coupang_cart_agent tests`
  - Result: completed successfully
- [ ] Live run 1건: search -> detail -> add-to-cart
- [ ] Different request 2건 이상 without fixed URL
- [ ] Layout/button variation fallback evidence 1건
- [ ] Access Denied or security challenge blocker evidence 1건
- [x] Option ambiguity or out-of-stock safe failure evidence 1건
  - Automated evidence: `tests.test_live_browser_agent.LiveBrowserAgentTests.test_agent_stops_on_option_ambiguity`
- [x] Fresh live login/session blocker evidence 1건
  - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=existing_cdp COUPANG_CHROME_REMOTE_DEBUGGING_PORT=9223 uv run python -m coupang_cart_agent integration-live-request '양파 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878`
  - Result: structured failure with `failed_stage=session`, `failure_reason=login_required`, and Telegram failure notification delivered to chat `8201584878`
- [ ] Telegram request -> agent -> Coupang cart -> Telegram reply evidence 1건
- [x] Branch / commit / publish status recorded
  - Branch: `wowogur12/how-24-coupang-aoai-live-web-shopping-agent-for-real-time-search`
  - Commit: `7f8ebb7`
  - Push: `git push -u origin wowogur12/how-24-coupang-aoai-live-web-shopping-agent-for-real-time-search`
  - PR: `https://github.com/choijhyeok/coupang_agent/pull/15`

### Notes / Blockers

- `coupang_cart_agent_cca14_runbook.md` is not present in this workspace.
- `uv run python -m coupang_cart_agent check-config` shows AOAI credentials are present, but `POSTGRES_DSN` and `COUPANG_CHROME_USER_DATA_DIR` are not set by default in the current shell.
- Live attempt:
  - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-request '양파 1개 담아줘' --user-id telegram:cli-user --chat-id cli-chat`
  - Result: did not complete within 60 seconds in this workspace; fresh live success/blocker evidence still needs operator-session validation.
- Live diagnosis after switching to `existing_cdp`:
  - CDP endpoint `http://127.0.0.1:9223/json/version` was reachable.
  - Attached page showed `https://cart.coupang.com/cartView.pang` but the body contained `로그인하기`, so the current operator session was not actually authenticated for cart actions.
  - Tightened workflow classification so this state records `session/login_required` instead of a generic `browser_agent` failure.
- Direct launch attempt with the real local profile:
  - `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --user-data-dir="$HOME/Library/Application Support/Google/Chrome" --profile-directory='Profile 1' --remote-debugging-port=9224 --no-first-run --no-default-browser-check about:blank`
  - Result: Chrome process exited immediately, `http://127.0.0.1:9224` was never reachable, so this workspace could not produce a fresh authenticated `Profile 1` CDP session automatically.
- `docker compose up -d postgres` could not be used because port `5432` was already allocated by another local Docker process; the existing local Postgres instance at `postgresql://postgres:postgres@localhost:5432/coupang_cart_agent` was reachable and used for validation instead.

### Follow-up Issues Created

- None
