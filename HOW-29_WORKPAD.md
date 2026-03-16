## Codex Workpad

### Environment

- Date: 2026-03-16 KST
- Branch: `codex/how-29-brand-pack-constraints`
- Workspace: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-29`
- Issue type: feature module

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
- [ ] Live validation records one brand-constrained request that either succeeds correctly or fails safely instead of adding the wrong item.

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
- [ ] Live brand-constrained validation
  - Intended request: `삼다수 2L 1개 담아줘`
  - Status: blocked locally because this workspace does not provide usable Telegram bot credentials, Azure OpenAI config, or a verified live Coupang session for an end-to-end request.

### Notes / Blockers

- `RequestedItem` now preserves additive intent fields: `explicit_brand`, `explicit_unit_size`, `explicit_pack_count`, and `explicit_pack_unit`.
- Selection now fails in the `selection` stage when all observed candidates violate explicit brand or pack semantics. This intentionally changes the previous demo behavior for requests like `삼다수 1박스 담아줘`; the workflow now fails safely before cart automation instead of continuing with a mismatched candidate.
- Search-query planning and the live search adapter now reuse the same explicit-intent-aware query builder so pack-count tokens removed from cart quantity parsing are still searched.
- No follow-up issue was created because the remaining live-validation gap is an external-access blocker for this issue, not a separate implementation track.

### Follow-up Issues Created

- None
