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
  - [ ] Real Telegram confirmation -> add-to-cart -> verification evidence
  - [x] Real rejection or rerank evidence
  - [x] Real pending proposal restore evidence after process restart
  - [x] DB evidence from live Postgres run
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-request '양파 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v3' --fixture-path /tmp/how35_onion_fixture_with_real_image.json`
  - [x] Result: real Telegram `sendPhoto` + proposal text succeeded; thread stored `awaiting_user_confirmation` with `active_proposal` in Postgres.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-request '다른 거 보여줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how35-live-onion-proposal-v3' --fixture-path /tmp/how35_onion_fixture_with_real_image.json`
  - [x] Result: rerank reply restored the pending proposal from Postgres and advanced to candidate index `1` without cart mutation.
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR=\"$HOME/Library/Application Support/Google/Chrome\" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent cart-live-inspect-session`
  - [x] Result: deterministic blocker reproduced; Coupang cart showed `로그인하기`, `page_kind=session_blocked`, and `LoginRequiredError`.

- Notes
  - `check-config` showed Telegram and Azure OpenAI credentials are present; live validation used the already running local Postgres container at `postgresql://postgres:postgres@localhost:5432/coupang_cart_agent`.
  - `COUPANG_SEARCH_ENDPOINT` is unset, so live proposal validation used a temporary captured-fixture variant with a publicly reachable image URL for Telegram `sendPhoto`.
  - During live validation I found and fixed two production-path issues:
    - the legacy `browser_shop` node still ran before proposal generation when a shopping agent was configured
    - synthetic `integration-live-request` envelopes did not preserve follow-up intent metadata, causing `다른 거 보여줘` to be misread
  - Existing LangGraph checkpoint warning about unregistered `CartAddStage` still appears in tests and remains outside this issue’s scope.

- Blockers
  - Live confirmation -> add-to-cart remains blocked by an operator-session prerequisite. Both `browser_use` and `cdp_chrome` on March 17, 2026 reached `https://cart.coupang.com/cartView.pang` in a logged-out state with `로그인하기`, so the guarded execution path correctly stopped with `LoginRequiredError`.
  - Human unblock step: re-authenticate the local Coupang Chrome profile (`Profile 1` or another approved profile), confirm `cart-live-inspect-session` no longer returns `session_blocked`, then rerun the confirmation step on a pending proposal thread.

- Follow-up Issues
  - None created.

- Publish
  - Push: `git push -u origin wowogur12/how-35-agent-ux-conversational-shopping-proposals` -> success
  - PR: `https://github.com/choijhyeok/coupang_agent/pull/20`
  - Exact published HEAD SHA is tracked in the Linear `## Codex Workpad` comment.
