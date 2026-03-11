## Codex Workpad

### Environment

- Date: 2026-03-11 16:34 KST
- Branch: `wowogur12/how-22-operations-always-on-telegram-worker-and-browser-use-live`
- Workspace: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-22`
- Issue type: integration

### Plan

1. Inspect the existing one-shot Telegram live path, LangGraph workflow, Coupang adapter strategy, and persistence seams.
2. Add an always-on Telegram worker with restart-safe cursor and pending-envelope replay.
3. Promote a browser-use-oriented real Chrome profile path for live cart automation and document the Access Denied mitigation.
4. Update docs/compose and record validation evidence.

### Acceptance Criteria

- [x] 별도 수동 CLI 실행 없이 계속 실행되는 Telegram worker가 존재한다
- [x] 사용자가 Telegram으로 "`~~~ 담아줘`" 요청을 보내면 worker가 이를 감지한다
- [x] worker가 LangGraph live workflow를 호출한다
- [x] 실제 Coupang add-to-cart 시도 경로가 `browser-use` 중심으로 정리된다
- [x] 기존 browser path의 `Access Denied` 회피 또는 완화 전략이 문서화된다
- [x] 성공 시 Telegram으로 담긴 상품 목록, 수량, 총액이 회신된다
- [x] 실패 시 Telegram으로 실패 단계와 원인이 회신된다
- [x] checkout 또는 payment는 절대 수행하지 않는다

### Validation

- [x] Worker end-to-end run
  - `POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/coupang_cart_agent COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Profile 1' uv run python -m coupang_cart_agent integration-live-telegram-worker --timeout 1 --sleep-seconds 0 --max-cycles 1 --intake-db-path .artifacts/how22_telegram_intake.sqlite3 --fixture-path tests/fixtures/coupang_search_onion_fixture.json --skip-error-response`
  - Result: worker processed `telegram-update-286968896` and returned `success=true`.
- [x] Worker polling evidence
  - `uv run python -m coupang_cart_agent capture-telegram-live-request --timeout 1 --max-attempts 1 --db-path .artifacts/how22_telegram_intake.sqlite3 --skip-error-response`
  - Result: captured `telegram-update-286968896`, text `콜라 제로 2개 담아줘`, next offset `286968897`.
- [x] Add-to-cart success or block evidence
  - `sqlite3 .data/cart_results.sqlite3 ...`
  - Result: `곰곰 국내산 양파, 3kg, 1개`, quantity `2`, `Item added to cart.`, recorded at `2026-03-11T07:39:41.782174+00:00`.
- [x] Telegram success/failure notification evidence
  - `docker exec how-22-postgres-1 psql -U postgres -d coupang_cart_agent -c "select ... notification_payload_json ..."`
  - Result: success payload summary `총 1종, 2개, 17,960원 장바구니 담기 완료`.
- [x] Worker restart restores DB thread/session state
  - Same worker command re-run with `--max-cycles 1`.
  - Result: worker started from persisted offset `286968897`, processed nothing, and did not duplicate the prior request.
- [x] Docker Compose worker service or equivalent operational script
  - `docker compose config`
  - Result: `worker` service renders with `browser_use` mode, `.artifacts/.data` mounts, and host Chrome bind mount support.
- [x] Automated tests
  - `uv run python -m unittest discover -s tests`
  - `uv run python -m compileall coupang_cart_agent tests`

### Notes / Blockers

- `coupang_cart_agent_cca14_runbook.md` is not present in this workspace.
- Real candidate fetch for arbitrary live requests is still tracked separately in `HOW-21`; this issue validated the worker path with the checked-in fixture.

### Follow-up Issues Created

- None
