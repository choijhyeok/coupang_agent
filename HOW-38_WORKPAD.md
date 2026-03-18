## Codex Workpad

- Environment
  - Date: 2026-03-18
  - Repo: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-38`
  - Branch: `codex/how-38-live-browser-proposals`
  - Issue: `HOW-38`
  - Issue type: integration

- Plan
  - [completed] Remove fixture-first live default and restore live browser candidate discovery as the primary proposal source.
  - [completed] Persist proposal/candidate source metadata so live vs debug evidence is distinguishable across proposal, rerank, and confirmation.
  - [completed] Update targeted tests for workflow, candidate discovery, and CLI-adjacent behavior.
  - [completed] Tighten README language so fixture/search-endpoint paths are clearly debug-only.

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

- Notes / Blockers
  - Live external validation evidence is not yet captured in this turn, so the issue is not complete against the full Linear acceptance criteria.
  - Need to check whether a Linear workpad comment can be updated through the available auth surface; initial GraphQL issue lookup returned HTTP 400.
  - Publish status: branch pushed to `origin/codex/how-38-live-browser-proposals`, existing PR detected at `https://github.com/choijhyeok/coupang_agent/pull/21`.
  - Commit: `a30365f2a127df9897d7ce48942b2258886d5aa8`

- Follow-up Issues Created
  - None
