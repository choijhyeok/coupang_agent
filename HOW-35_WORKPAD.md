## Codex Workpad

- Environment
  - Date: 2026-03-17
  - Workspace: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-35`
  - Branch: `wowogur12/how-35-agent-ux-conversational-shopping-proposals`
  - Issue: `HOW-35`
  - Issue type: feature module

- Plan
  1. Extend shared contracts and Telegram delivery to support proposal images and follow-up reply parsing.
  2. Refactor the LangGraph live workflow into proposal -> confirmation -> guarded cart execution -> result notification.
  3. Persist proposal state, user decisions, and metadata in workflow state plus operational storage.
  4. Add targeted tests for proposal, confirmation, rejection/rerank, image sending, and session restore behavior.
  5. Validate locally, publish branch, and document live blockers.

- Acceptance Criteria
  - [x] First request generates a recommendation and confirmation question instead of executing cart mutation.
  - [x] Proposal message includes product name, price, option summary, and recommendation reason.
  - [x] Telegram notification path supports product image delivery through `sendPhoto`.
  - [x] Confirmation replies map back to the pending proposal and execute add-to-cart only after confirmation.
  - [x] Rejection and `다른 거 보여줘` follow-up replies use pending proposal state instead of mutating the cart.
  - [x] Guard prevents cart execution before confirmation.
  - [x] Final success/failure notification is sent only after execution/verification path completes.
  - [x] Pending proposal state is persisted in workflow state and operational storage for restore.

- Validation
  - [x] `uv run python -m unittest tests.test_telegram_intake tests.test_notifications tests.test_live_workflow tests.test_telegram_worker`
  - [x] `uv run python -m unittest tests.test_foundation tests.test_selection tests.test_integration tests.test_live_workflow_verification tests.test_live_browser_agent tests.test_cart_verification`
  - [x] `uv run python -m py_compile coupang_cart_agent/*.py tests/test_live_workflow.py tests/test_notifications.py tests/test_telegram_intake.py tests/test_telegram_worker.py`
  - [x] Real Telegram proposal message + image preview evidence
  - [x] Real Telegram confirmation -> add-to-cart -> verification success evidence
  - [x] Real rejection or rerank evidence
  - [x] Real pending proposal restore evidence after process restart
  - [x] DB evidence from live Postgres run
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-request '양파 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v3' --fixture-path /tmp/how35_onion_fixture_with_real_image.json`
  - [x] Result: real Telegram `sendPhoto` + proposal text succeeded; thread stored `awaiting_user_confirmation` with `active_proposal` in Postgres.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-request '다른 거 보여줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v3' --fixture-path /tmp/how35_onion_fixture_with_real_image.json`
  - [x] Result: rerank reply restored the pending proposal from Postgres and advanced to candidate index `1` without cart mutation.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent cart-live-inspect-session`
  - [x] Result: deterministic blocker reproduced; Coupang cart showed `로그인하기`, `page_kind=session_blocked`, and `LoginRequiredError`.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent cart-live-inspect-session`
  - [x] Result: `Default` profile is attached and logged in; `page_kind=browse`, `session_mode=attached_browser_use_profile`.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request 'ㅇㅇ 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v3'`
  - [x] Result: confirmation resumed the pending proposal on a logged-in session; initial failure was a real navigation-timing bug (`Page.title: Execution context was destroyed`) which I fixed in `cart_adapters.py`.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request '양파 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v4' --fixture-path /tmp/how35_onion_fixture_with_real_image.json`
  - [x] Result: fresh proposal thread `how35-live-onion-proposal-v4` created with candidate index `0` and Telegram image preview.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request 'ㅇㅇ 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v4'`
  - [x] Result: confirmation executed browser cart automation and verification on candidate index `0`; product page did not yield a cart mutation, and verification recorded `cart_count_before=0`, `cart_count_after=0`, empty cart evidence, screenshot path `.artifacts/browser-agent/verification-cart.png`, and `failure_reason=verification_mismatch`.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request '다른 거 보여줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v4'`
  - [x] Result: rerank advanced candidate index from `0 -> 1 -> 2` on the same thread without cart mutation.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request 'ㅇㅇ 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v4'`
  - [x] Result: candidate index `1` and candidate index `2` both failed on real product pages with `failure_reason=out_of_stock`.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Default' uv run python -m coupang_cart_agent integration-live-request 'ㅇㅇ 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-cereal-proposal-v1'`
  - [x] Result: saved proposal thread `how35-live-cereal-proposal-v1` resumed after restore, confirmed the proposed `오리온 미쯔블랙 시리얼, 360g, 1개`, executed add-to-cart, and verified success with `cart_count_before=0`, `cart_count_after=1`, `stage=verification`, screenshot `.artifacts/browser-agent/verification-cart.png`, and Telegram success notification after verification.

- Notes
  - `check-config` showed Telegram and Azure OpenAI credentials are present; live validation used the already running local Postgres container at `postgresql://postgres:postgres@localhost:5432/coupang_cart_agent`.
  - `COUPANG_SEARCH_ENDPOINT` is unset, so live proposal validation used a temporary captured-fixture variant with a publicly reachable image URL for Telegram `sendPhoto`.
  - During live validation I found and fixed two production-path issues:
    - the legacy `browser_shop` node still ran before proposal generation when a shopping agent was configured
    - synthetic `integration-live-request` envelopes did not preserve follow-up intent metadata, causing `다른 거 보여줘` to be misread
    - attach-mode confirmation could fail on a transient navigation with `Page.title: Execution context was destroyed`; fixed by making title-based session blocker checks use safe title reads
  - Existing LangGraph checkpoint warning about unregistered `CartAddStage` still appears in tests and remains outside this issue’s scope.

- Blockers
  - None at issue scope. `Default` profile is attached successfully, and the required proposal, rerank, restore, and confirmation success evidence is now captured.

- Follow-up Issues
  - Created `HOW-36` Backlog: `[Agent UX] Auto-fallback to next proposal candidate after confirmation add-to-cart failure`
  - Link: `https://linear.app/choijhyeok/issue/HOW-36/agent-ux-auto-fallback-to-next-proposal-candidate-after-confirmation`

- Publish
  - Push: `git push -u origin wowogur12/how-35-agent-ux-conversational-shopping-proposals` -> success
  - PR: `https://github.com/choijhyeok/coupang_agent/pull/20`
  - Latest published HEAD SHA is tracked in the Linear `## Codex Workpad` comment.
