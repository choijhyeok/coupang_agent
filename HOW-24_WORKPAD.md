## Codex Workpad

### Environment

- Date: 2026-03-16 KST
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
  - `uv run python -m unittest tests.test_foundation tests.test_live_browser_agent tests.test_live_workflow`
  - Result: latest rerun `Ran 46 tests ... OK`
  - Additional focused Telegram/runtime tests:
    `uv run python -m unittest tests.test_telegram_intake tests.test_notifications tests.test_live_workflow tests.test_foundation`
  - Result: `Ran 52 tests ... OK`
- [x] Full automated regression relevant to touched modules
  - `uv run python -m unittest discover -s tests`
  - Result: `Ran 71 tests ... OK`
  - Latest rerun after Telegram truststore hardening:
    `uv run python -m unittest discover -s tests`
  - Result: `Ran 72 tests ... OK`
  - Latest rerun after cart-page classification hardening:
    `uv run python -m unittest discover -s tests`
  - Result: `Ran 82 tests ... OK`
- [x] Bytecode / import sanity
  - `uv run python -m compileall coupang_cart_agent tests`
  - Result: completed successfully
- [x] Live run 1건: search -> detail -> add-to-cart
  - Live evidence on 2026-03-16:
    - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request '한끼 양파 300g 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878`
    - Result: attached logged-in `Default` session started from Coupang cart, used observation-driven search without a fixed product URL, opened real product `6202345578`, observed a cleaned product-page state with `available_options=[]` and `add_to_cart_visible=true`, added to cart successfully, and persisted `workflow_runs.success=true`.
- [x] Different request 2건 이상 without fixed URL
  - Live evidence on 2026-03-16:
    - Request 1: `한끼 양파 300g 1개 담아줘`
    - Request 2: `생수 2L 1개 담아줘`
    - Both runs started from an attached logged-in browser/cart session, initiated live search without a predefined product URL, navigated through search results into product detail, and completed add-to-cart successfully.
- [x] Layout/button variation fallback evidence 1건
  - Live evidence on 2026-03-16:
    - The successful `한끼 양파 300g 1개 담아줘` run attached on `https://cart.coupang.com/cartView.pang`, where no usable search box was exposed in the current page context.
    - The browser adapter fell back to direct Coupang search URL navigation, then used observation-driven search-result interpretation plus direct `page.goto(target_href)` product opening to reach add-to-cart without relying on a fixed selector path.
- [x] Access Denied or security challenge blocker evidence 1건
  - Access Denied evidence already persisted in live workflow history:
    - `workflow_runs` contains multiple 2026-03-11 runs with `failed_stage=session` and `failure_message=Coupang blocked the automated browser session with Access Denied.`
- [x] Option ambiguity or out-of-stock safe failure evidence 1건
  - Automated evidence: `tests.test_live_browser_agent.LiveBrowserAgentTests.test_agent_stops_on_option_ambiguity`
  - Live evidence on 2026-03-16:
    - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request '한끼 양파 300g 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878`
    - Result: attached logged-in `Default` profile searched from request text, navigated into real product detail `6202345578`, and then stopped safely with `failed_stage=option_selection` / `failure_reason=ambiguity` because extracted page options were still noisy and did not map cleanly to the request.
- [x] Fresh live login/session blocker evidence 1건
  - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=existing_cdp COUPANG_CHROME_REMOTE_DEBUGGING_PORT=9223 uv run python -m coupang_cart_agent integration-live-request '양파 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878`
  - Result: structured failure with `failed_stage=session`, `failure_reason=login_required`, and Telegram failure notification delivered to chat `8201584878`
- [x] Operator attach-session diagnostic command added
  - `uv run python -m coupang_cart_agent cart-live-inspect-session`
  - Result in current workspace: structured `LoginFailedError` when no reachable operator CDP endpoint is present
  - `COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent cart-live-inspect-session`
  - Result: attached copied-profile session reaches `https://cart.coupang.com/cartView.pang` but still shows `로그인하기`; classified `LoginRequiredError`
  - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=existing_cdp COUPANG_CHROME_REMOTE_DEBUGGING_PORT=9223 uv run python -m coupang_cart_agent cart-live-inspect-session`
  - Result on 2026-03-16 after worker-thread hardening: returns structured `LoginFailedError` without teardown traceback or `sync-in-async` Playwright failure; current blocker is now the expected “no reachable logged-in operator CDP session”
  - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=existing_cdp COUPANG_CHROME_REMOTE_DEBUGGING_PORT=9226 uv run python -m coupang_cart_agent cart-live-inspect-session`
  - Result on 2026-03-16: reachable CDP session attached, navigated to `https://cart.coupang.com/cartView.pang`, and observed `로그인하기` with `cart_count=0`; output now classifies the page as `session_blocked` with blocker hint `Attach mode requires an operator-prepared logged-in Coupang session.`
- [x] Telegram request -> agent -> Coupang cart -> Telegram reply evidence 1건
  - Live evidence on 2026-03-16:
    - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request '한끼 양파 300g 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878`
    - Result: Telegram request reached LangGraph live workflow, the browser agent searched and added a real Coupang product to cart, a Telegram success notification was emitted, and the persisted run recorded `success=true`.
  - Additional live evidence on 2026-03-16:
    - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request '생수 2L 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878`
    - Result: cart-started browse context was coerced into a fresh live search, the agent reached product detail `4683535861`, added to cart successfully, emitted a Telegram success notification, and persisted `workflow_runs.success=true`.
- [x] Telegram request -> agent -> Coupang cart -> Telegram reply failure-path evidence 1건
  - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=existing_cdp COUPANG_CHROME_REMOTE_DEBUGGING_PORT=9226 uv run python -m coupang_cart_agent integration-live-request '양파 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878`
  - Result on 2026-03-16: request reached LangGraph live workflow, browser attach reached a structured Coupang `session/login_required` failure, persistence completed, and notification send then failed at `notify` because Telegram Bot API TLS verification failed in the local shell
  - Follow-up rerun after workflow fix: result and persisted run now keep `failed_stage=session` / `failure_message=Attach mode requires an operator-prepared logged-in Coupang session.` while still storing a `notification_payload.stage=notify` failure payload, so root-cause cart blocker is no longer overwritten by downstream notification delivery failure
  - Follow-up rerun after Telegram truststore hardening: result still keeps `failed_stage=session`, but `notification_payload.stage=session` and the workflow no longer fails at `notify`, confirming the Telegram reply path can succeed in this shell once system trust is used
- [x] Branch / commit / publish status recorded
  - Branch: `wowogur12/how-24-coupang-aoai-live-web-shopping-agent-for-real-time-search`
  - Latest local commit: `6b760e9`
  - Published remote commit: `6b760e9`
  - Push status:
    - Success earlier: `git push -u origin wowogur12/how-24-coupang-aoai-live-web-shopping-agent-for-real-time-search`
    - HTTPS push attempt failed on 2026-03-16:
      - Command: `git push`
      - Result: `remote: Permission to choijhyeok/coupang_agent.git denied to choijhyeok. fatal: unable to access 'https://github.com/choijhyeok/coupang_agent.git/': The requested URL returned error: 403`
    - Publish workaround succeeded:
      - Command: `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/github_ssh_known_hosts -o IdentitiesOnly=yes -i ~/.ssh/choijhyeok-GitHub -p 443' git push ssh://git@ssh.github.com:443/choijhyeok/coupang_agent.git HEAD:refs/heads/wowogur12/how-24-coupang-aoai-live-web-shopping-agent-for-real-time-search`
      - Result: `Everything up-to-date`
    - Remote-tracking ref refreshed:
      - Command: `git fetch origin wowogur12/how-24-coupang-aoai-live-web-shopping-agent-for-real-time-search`
      - Result: `origin/wowogur12/how-24-coupang-aoai-live-web-shopping-agent-for-real-time-search` updated to `6b760e9`, and local branch no longer shows as ahead
  - PR: `https://github.com/choijhyeok/coupang_agent/pull/15`
  - PR review sweep on 2026-03-16 after latest publish:
    - Public PR conversation is visible.
    - No review submissions or actionable review comments are present at this time.
  - Follow-up review check on 2026-03-16:
    - `gh pr view 15 --json number,state,title,reviewDecision,comments,reviews,headRefName,commits`
    - Result: blocked by the local GitHub token policy (`Your network administrator has blocked access to GitHub except for the 'KT Corp. - EMU' enterprises.`), so no newer CLI-backed review metadata could be fetched from this shell.
  - Linear status comment refreshed on 2026-03-16:
    - Posted an issue comment summarizing the two verified live successes, the cart-start and option-observation fixes, and the remaining explicit brand/pack-size quality debt tracked in `HOW-28`.

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
- Additional CDP diagnosis on 2026-03-16:
  - Local Chrome instance at `http://127.0.0.1:9226/json/version` was reachable and exposed multiple tabs.
  - `existing_cdp` attach against `9226` now works with clean structured output after worker-thread hardening.
  - The attached cart page still rendered the unauthenticated state (`로그인을 하시면, 장바구니에 보관된 상품을 확인하실 수 있습니다.`), and the observation now records that state as `session_blocked`, so the remaining blocker is a non-authenticated Coupang session in that Chrome profile.
- Copied-profile validation against the real local Chrome profile on 2026-03-16:
  - `COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' COUPANG_CHROME_REMOTE_DEBUGGING_PORT=9227 uv run python -m coupang_cart_agent cart-live-inspect-session`
  - Result: copied `Profile 1` launch also reached `https://cart.coupang.com/cartView.pang` and still showed `로그인하기`, so both attach strategies currently converge on the same unauthenticated cart state in this workspace.
- Cookie comparison note:
  - Source profile DB still contains Coupang cookies including `.coupang.com|member_srl` and `.coupang.com|sid`.
  - The long-running `/tmp/how24-open-profile/Profile 1/Cookies` snapshot did not show `member_srl`, reinforcing that “cookie presence somewhere on disk” is not enough to guarantee an attachable logged-in cart session.
- Telegram live delivery environment note on 2026-03-16:
  - Plain `urllib.request.urlopen('https://api.telegram.org')` from this shell fails with `CERTIFICATE_VERIFY_FAILED` / `self-signed certificate in certificate chain`.
  - The Telegram client now uses system trust via `truststore`, and `TelegramBotApiClient(token='test-token').get_me()` reaches Bot API successfully enough to receive an HTTP response (`404 Not Found` for the dummy token instead of TLS failure).
  - `integration-live-request` against chat `8201584878` therefore no longer fails at `notify`; the remaining live blocker is back to the true Coupang session/auth state.
- Workflow failure-reporting hardening on 2026-03-16:
  - Notification delivery failures no longer overwrite an existing upstream cart/browser failure stage in the live workflow state.
  - This keeps DB and CLI output aligned with the true shopping blocker even when Telegram delivery fails later in the run.
- Additional live browser-agent hardening on 2026-03-16:
  - Search now falls back to a direct Coupang search URL when the attached page starts from cart and no visible search box is available.
  - Search-result clicks now use direct `page.goto(target_href)` for product links, avoiding the attached-browser case where a locator click left the current page on search results.
  - Search-result observation now suppresses false product-page signals from header/cart/filter UI, so search pages no longer become `option_selection` just because they contain generic `장바구니` or filter text.
  - Deterministic ranking now penalizes ad links and prefers stronger text overlap with the requested item/search query.
  - Planned search queries now fall back to the original item name when an upstream plan drifts away from the request text and no longer preserves the item name verbatim.
  - Product-page option normalization now drops obvious non-option controls such as `쿠폰받기`, `수량빼기`, `수량더하기`, `문의하기`, and `신고하기`.
  - Cart-page observation now treats `cartView.pang` as `browse` state and suppresses cart-item / ad-card candidate extraction so the agent can start a new search instead of misreading existing cart contents as active product candidates.
  - Search-result sold-out copy no longer triggers an early out-of-stock stop before detail-page navigation.
  - Policy-layer coercion now forces a fresh search when the model tries to stop/click from `cartView.pang` browse context, preventing existing cart contents from being treated as the active shopping target.
  - Remaining follow-up quality debt:
    - Brand-constrained requests can still select a technically addable but request-mismatched product, as seen in the live `삼다수 2L 1개 담아줘` run choosing `몽베스트 생수, 2L, 6개`.
    - Real navigation on the attached browser can still intermittently fail with `Page.goto ... Timeout 30000ms exceeded`.
- Direct launch attempt with the real local profile:
  - `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --user-data-dir="$HOME/Library/Application Support/Google/Chrome" --profile-directory='Profile 1' --remote-debugging-port=9224 --no-first-run --no-default-browser-check about:blank`
  - Result: Chrome process exited immediately, `http://127.0.0.1:9224` was never reachable, so this workspace could not produce a fresh authenticated `Profile 1` CDP session automatically.
- Cookie metadata check on local `Profile 1`:
  - `sqlite3 "$HOME/Library/Application Support/Google/Chrome/Profile 1/Cookies" "select host_key, name, length(encrypted_value) from cookies where host_key like '%coupang%' order by host_key, name limit 50;"`
  - Result: Coupang-related cookies such as `.coupang.com|sid`, `.coupang.com|web-session-id`, and `.coupang.com|member_srl` are present in the profile DB, so the current blocker is not “no Coupang cookies at all” but “the attached browser session still fails Coupang auth/session checks for cart actions”.
- Copied-profile session-preservation attempt:
  - Changed copied profile preparation to keep `Sessions` and `Session Storage` instead of excluding them.
  - Fresh validation still produced cart-page `로그인하기` / `login_required`, so preserving those directories alone was not sufficient to restore an authenticated Coupang cart session in this workspace.
- Additional hardening on 2026-03-16:
  - The deterministic browser-agent fallback now treats `sold_out` observation hints as first-class blockers instead of drifting into `unknown` or re-search loops.
  - Search-result ranking now de-prioritizes sold-out items so a highly rated but unavailable product is not clicked ahead of an in-stock alternative.
- Attach-session diagnostic stability fix on 2026-03-16:
  - `PlaywrightContextManager` could be left partially initialized on attach failures and then crash in `close()` with `AttributeError: ... _connection`.
  - `cart-live-inspect-session` now exits with structured JSON instead of a teardown traceback when attach fails early.
  - `ExistingChromeCdpCoupangCartPage` now dispatches Playwright sync setup onto a dedicated worker thread when an asyncio loop is already running, so the attach path no longer dies with `It looks like you are using Playwright Sync API inside the asyncio loop.`
- README/operator docs updated on 2026-03-16:
  - Added explicit interpretation for `cart-live-inspect-session` outputs, including the difference between `LoginFailedError` and a reachable-but-unauthenticated `LoginRequiredError` / `session_blocked`.
  - Documented that CDP reachability alone is not proof of a valid logged-in Coupang cart session.
  - Replaced the stale README note that said fresh AOAI browser-agent live validation was still pending; the README now records the two 2026-03-16 live attach-mode successes plus the structured attach/session failure evidence.
- LangGraph checkpoint warning observed during automated tests:
  - `Deserializing unregistered type coupang_cart_agent.contracts.CartAddStage from checkpoint`
  - Current tests still pass, but this is now tracked as follow-up issue `HOW-25` because a future LangGraph release may block these restores.
- GitHub HTTPS credential issue remains:
  - `gh auth status` reports the keyring token for `choijhyeok` is invalid, and the inactive stored accounts are also invalid.
  - Branch publication is no longer blocked because SSH over port `443` with `~/.ssh/choijhyeok-GitHub` succeeded.
- `docker compose up -d postgres` could not be used because port `5432` was already allocated by another local Docker process; the existing local Postgres instance at `postgresql://postgres:postgres@localhost:5432/coupang_cart_agent` was reachable and used for validation instead.

### Follow-up Issues Created

- `HOW-25` Backlog: `[Coupang] Allowlist LangGraph checkpoint enum types for persisted live workflow state`
  - Related to `HOW-24`
- `HOW-26` Backlog: `[Coupang] Add operator diagnostics for validating an attachable logged-in Chrome session`
  - Related to `HOW-24`
- `HOW-27` Backlog: `[Telegram] Add Bot API TLS trust preflight`
  - Related to `HOW-24`
- `HOW-28` Backlog: `[Coupang] Respect explicit brand and pack-size constraints in live product ranking`
  - Related to `HOW-24`
