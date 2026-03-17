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
  - [ ] Real Telegram proposal message + image preview evidence
  - [ ] Real Telegram confirmation -> add-to-cart -> verification evidence
  - [ ] Real rejection or rerank evidence
  - [ ] Real pending proposal restore evidence after process restart
  - [ ] DB evidence from live Postgres run

- Notes
  - `check-config` shows Telegram and Azure OpenAI credentials are present, but `POSTGRES_DSN` is not configured in this workspace.
  - Live workflow commands require PostgreSQL because `integration-live-request` and `integration-live-telegram-worker` build the LangGraph checkpointer and operational store on Postgres.
  - `COUPANG_SEARCH_ENDPOINT` is also unset, so a live proposal run would need either a captured fixture or an upstream search endpoint before true end-to-end validation.
  - Existing LangGraph checkpoint warning about unregistered `CartAddStage` still appears in tests and remains outside this issue’s scope.

- Blockers
  - Missing `POSTGRES_DSN` blocks required live workflow validation and DB evidence collection.

- Follow-up Issues
  - None created.

- Publish
  - Commit: `58f796b2d70d129fa843a082fbf2a74811b4bc83`
  - Push: `git push -u origin wowogur12/how-35-agent-ux-conversational-shopping-proposals` -> success
  - PR: `https://github.com/choijhyeok/coupang_agent/pull/20`
