## Codex Workpad

- Environment
  - Date: 2026-03-18
  - Repo: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-38`
  - Branch: `codex/how-38-live-browser-proposals`
  - Issue: `HOW-38`
  - Issue type: review/rework

- Plan
  - [completed] Remove fixture-first live default and restore live browser candidate discovery as the primary proposal source.
  - [completed] Persist proposal/candidate source metadata so live vs debug evidence is distinguishable across proposal, rerank, and confirmation.
  - [completed] Update targeted tests for workflow, candidate discovery, and CLI-adjacent behavior.
  - [completed] Tighten README language so fixture/search-endpoint paths are clearly debug-only.
  - [completed] Capture fixture-free live validation evidence and mirror the workpad into Linear.

- Acceptance Criteria Checklist
  - [x] `integration-live-*` no longer requires `--fixture-path` or `COUPANG_SEARCH_ENDPOINT` for default proposal generation.
  - [x] First proposal candidates come from live browser discovery by default.
  - [x] Confirmation still executes the previously proposed live-discovered candidate.
  - [x] Proposal state distinguishes live and debug candidate origins.
  - [x] Debug fixture usage is explicitly marked and separated from live evidence.

- Validation Checklist
  - [x] `uv run python -m unittest tests.test_live_workflow`
  - [x] `uv run python -m unittest tests.test_live_browser_agent`
  - [x] `uv run python -m unittest tests.test_telegram_worker`
  - [x] `uv run python -m unittest tests.test_foundation`
  - [x] `uv run python -m compileall coupang_cart_agent tests/test_live_workflow.py`
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent cart-live-inspect-session`
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent integration-live-request '양파 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how38-live-onion-20260318-a'`
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent integration-live-request '다른 거 보여줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how38-live-onion-20260318-a'`
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent integration-live-request 'ㅇㅇ 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how38-live-onion-20260318-a'`
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent integration-live-request '시리얼 1개 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how38-live-cereal-20260318-a'`
  - [x] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent integration-live-request 'ㅇㅇ 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how38-live-cereal-20260318-a'`
  - [ ] `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=browser_use COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent integration-live-telegram-once --timeout 2 --intake-db-path .artifacts/how38_telegram_intake.sqlite3 --skip-error-response`

- Notes / Blockers
  - Publish status: branch pushed to `origin/codex/how-38-live-browser-proposals`, existing PR detected at `https://github.com/choijhyeok/coupang_agent/pull/21`.
  - PR review sweep on March 18 found no GitHub review comments, review summaries, or top-level PR comments pending on PR `#21`.
  - Latest published commit SHA is reported in the final handoff because updating this workpad changes `HEAD`; the published branch and PR are the authoritative references in-repo.
  - Linear workpad comment created on HOW-38: comment id `7d3f8eed-0b23-48a5-a965-d5ef94f1ccf8`.
  - Focused automated validation was rerun on March 18 after resuming this workspace:
    - `uv run python -m unittest tests.test_live_workflow`
    - `uv run python -m unittest tests.test_live_browser_agent`
    - `uv run python -m unittest tests.test_telegram_worker`
    - `uv run python -m unittest tests.test_foundation`
    - `uv run python -m compileall coupang_cart_agent tests/test_live_workflow.py`
  - March 18 live validation restored the fixture-free default path:
    - `cart-live-inspect-session` attached to the real Chrome profile and reached `https://cart.coupang.com/cartView.pang` with `page_kind=browse`; the session was logged in and not blocked by `로그인하기` or Access Denied.
    - `integration-live-request '양파 담아줘'` produced a real proposal with `candidate_source_mode=live_browser` and live-discovered candidates including `5625813479`, `1395626422`, and `4876673058`; Telegram proposal delivery succeeded without `--fixture-path` or `COUPANG_SEARCH_ENDPOINT`.
    - The first confirmation attempt on onion failed at `failed_stage=verification` because the cart remained empty. This is retained as regression evidence that the workflow no longer reports success on empty-cart or verification-mismatch outcomes.
    - `integration-live-request '다른 거 보여줘'` stayed inside the same live candidate pool and advanced to candidate `1395626422` rather than reloading any fixture source.
    - The second onion confirmation completed successfully against the reranked live candidate, with `cart_results[0].success=true`, `stage=verification`, and persisted thread status `completed`.
    - A second distinct request, `integration-live-request '시리얼 1개 담아줘'`, also generated a fixture-free live proposal and then completed successfully on confirmation, increasing observed cart count from 1 to 2 and verifying `오리온 미쯔블랙 시리얼`.
  - Remaining caution: the highest-ranked first onion candidate was not addable in practice on March 18, while the reranked onion candidate and cereal candidate verified correctly. This looks like product-specific candidate quality debt rather than a fixture/live-path regression.
  - Remaining blocker for full validation closure: `integration-live-telegram-once` was exercised without `--fixture-path` against a fresh intake DB, but returned `No parseable Telegram requests were captured.` A direct Telegram Bot API check with `getUpdates(timeout=1)` then returned `update_count=0`, and a follow-up bot prompt sent successfully to chat `8201584878` also produced no inbound reply during a 180-second long poll. There were no pending real inbound Telegram updates available for the bot at run time. The missing evidence is blocked on external operator/user input rather than code.
  - A final `integration-live-telegram-worker --timeout 60 --max-cycles 1` run on March 18 also returned `intake_result_count=0` and `processed_count=0`. Existing historical `telegram-update-*` success rows in `workflow_runs` are not valid HOW-38 close-out evidence because at least one persisted Telegram proposal (`telegram-update-286968923`) shows a request for `시리얼` paired with onion candidates, which indicates stale or pre-fix invalid data rather than trustworthy fixture-free live completion evidence.

- Follow-up Issues Created
  - `HOW-39` `[Live Selection] Penalize non-addable live candidates before first proposal`
