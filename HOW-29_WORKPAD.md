## Codex Workpad

### Environment

- Date: 2026-03-16 KST
- Branch: `codex/how-29-brand-pack-constraints`
- Workspace: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-29`
- Issue type: feature module
- Linear state: `Human Review`

### Plan

1. Extend Telegram intake to preserve explicit brand, unit-size, and pack-count intent without redesigning the existing request contract.
2. Feed preserved intent into deterministic search-query construction for planner fallback and live candidate lookup.
3. Enforce explicit brand and pack/unit-size constraints in product ranking so mismatching candidates fail safely at selection.
4. Add focused regression coverage for one brand-mismatch case and one pack-mismatch case.
5. Record validation, publish the branch, and document the live-validation blocker if external access is unavailable.

### Acceptance Criteria

- [x] A request with an explicit brand name does not select a different brand without an explicit ambiguity/failure state.
- [x] A request with an explicit pack-size or count constraint does not silently map to a materially different pack configuration.
- [x] Ranking still uses rating/review/price for candidates that satisfy explicit request constraints.
- [x] Tests cover at least one brand mismatch case and one pack-size mismatch case.
- [x] Live validation records one brand-constrained request that either succeeds correctly or fails safely instead of adding the wrong item.

- [x] Branch / commit / publish status recorded
  - Branch: `codex/how-29-brand-pack-constraints`
  - Commit: `e5a71e7`
  - Push: `git push -u origin codex/how-29-brand-pack-constraints`
  - PR: `https://github.com/choijhyeok/coupang_agent/pull/16`

### Validation

- [x] Focused intake + selection tests
  - `uv run python -m unittest tests.test_telegram_intake tests.test_selection`
  - Result: `Ran 25 tests ... OK`
- [x] Full regression
  - `uv run python -m unittest discover -s tests`
  - Result: `Ran 62 tests ... OK`
- [x] Bytecode / import sanity
  - `python -m compileall coupang_cart_agent tests`
  - Result: completed successfully
- [x] Live brand-constrained validation
  - Command:
    `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=playwright uv run python -m coupang_cart_agent integration-live-request '삼다수 2L 1개 담아줘' --user-id telegram:8201584878 --chat-id 8201584878 --thread-id how29-live-brand-pack-20260316 --fixture-path /tmp/how29-brand-mismatch-live.json`
  - Fixture shape used for validation: production-shaped candidate records containing only mismatching packs/brands (`몽베스트 2L 6개`, `백산수 2L 6개`, `아이시스 2L 12개`).
  - Result: live workflow persisted `failed_stage=selection` with `failure_message=No candidates satisfied the explicit request constraints...`, no selections, no cart results, and a real Telegram failure notification was delivered to chat `8201584878`.

### Notes / Blockers

- `RequestedItem` now preserves additive intent fields: `explicit_brand`, `explicit_unit_size`, `explicit_pack_count`, and `explicit_pack_unit`.
- Selection now fails in the `selection` stage when all observed candidates violate explicit brand or pack semantics. This intentionally changes the previous demo behavior for requests like `삼다수 1박스 담아줘`; the workflow now fails safely before cart automation instead of continuing with a mismatched candidate.
- Search-query planning and the live search adapter now reuse the same explicit-intent-aware query builder so pack-count tokens removed from cart quantity parsing are still searched.
- Live validation used a captured production-shaped mismatch fixture because `COUPANG_SEARCH_ENDPOINT` is not configured in this workspace. The executed path still used real Azure OpenAI planning, LangGraph state persistence, PostgreSQL operational storage, and Telegram delivery.
- No follow-up issue was created.

### Review Sweep

- PR: `https://github.com/choijhyeok/coupang_agent/pull/16`
- PR comments: none
- PR reviews: none
- PR checks: none reported on the branch

### Follow-up Issues Created

- None
