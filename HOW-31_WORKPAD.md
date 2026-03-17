## Codex Workpad

### Environment

- Date: 2026-03-17 KST
- Branch: `how-31-goal-driven-recovery-loop`
- Workspace: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-31`
- Issue type: feature module
- Linear state: active

### Plan

1. Inspect the existing live browser loop, observation layer, cart verification, and persistence seams that HOW-31 must extend.
2. Introduce recovery-loop contracts for scroll/reobserve/back/substitute attempts and explicit goal-check evidence without redesigning unrelated modules.
3. Replace the Playwright DOM-eval-first observation path with a Scrapling-first extractor while keeping Playwright as the execution/attach layer.
4. Refactor the browser shopping agent to use goal completion, dynamic UI recovery, substitute-product replanning, and intent verification before success.
5. Add focused regression coverage for fold-below CTA recovery, wrong-category false-success prevention, substitute selection, and persistence of recovery evidence.
6. Run targeted validation, attempt publish steps, and update this workpad with exact commands, results, branch, commit, and PR status.

### Acceptance Criteria

- [x] add-to-cart CTA가 fold 아래에 있어도 agent가 스크롤 후 재탐색하여 장바구니 담기를 완료할 수 있다
- [x] 첫 상품이 구매 불가 또는 장바구니 미지원이어도, 같은 사용자 intent를 만족하는 대체 상품을 재탐색할 수 있다
- [x] 잘못된 카테고리 상품이 담긴 경우 success로 종료하지 않고 recovery loop를 다시 수행한다
- [x] "시리얼 1개 담아줘" 요청에서 양파 같은 무관 상품을 담고 성공 회신하는 false success가 차단된다
- [x] 종료 조건이 "액션 1회 성공"이 아니라 "요청 의도 충족 검증 완료"로 변경된다
- [x] selector/tag 일부 변경이 있어도 agent가 scroll/reobserve/replan으로 계속 진행할 수 있다
- [x] 핵심 관찰 계층이 `Scrapling-first` 구조로 교체되고, Playwright는 보조 실행기 수준으로 축소된다
- [x] `Scrapling`을 기준으로 CTA 탐색, 상품 정보 추출, cart verification이 동작한다
- [x] Branch / commit / publish status recorded

### Validation

- [x] Fold-below CTA recovery evidence
- [x] Purchase-restricted substitute recovery evidence
- [x] False-success prevention for wrong-category cart item
- [x] Recovery loop performing 2+ recovery action types
- [x] Goal-check before Telegram success
- [x] Scrapling-based key element rediscovery evidence
- [x] Selector-drift resilience regression evidence
- [x] Focused tests
- [x] Full regression
- [x] Publish status

### Notes / Blockers

- Repository started on `main` with a clean working tree.
- `coupang_cart_agent_cca14_runbook.md` is not present in this workspace.
- Implemented a `ScraplingObservationAdapter` and moved the Playwright page observer to a Scrapling-first extraction path with persisted adaptive selector hints.
- Added recovery actions and goal-driven replanning in the live browser agent: `scroll`, `go_back`, substitute-result selection, and goal-check retry on cart mismatch/review-needed outcomes.
- Live validation setup:
  - Chrome profiles detected under `$HOME/Library/Application Support/Google/Chrome`.
  - `Profile 1` was not logged into Coupang.
  - `Default` profile attached successfully with `COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome`.
  - Docker Postgres container `coupang_agent-postgres-1` was available on `localhost:5432`.
- Validation commands:
  - `uv run python -m unittest tests.test_live_browser_agent` -> `OK` (14 tests)
  - `uv run python -m unittest tests.test_live_workflow_verification` -> `OK` (1 test)
  - `uv run python -m unittest tests.test_foundation tests.test_integration tests.test_live_workflow tests.test_cart_verification tests.test_notifications` -> `OK` (48 tests)
  - `uv run python -m unittest discover -s tests` -> `OK` (94 tests)
  - `uv run python -m compileall coupang_cart_agent tests` -> success
- Additional validation commands after live-driven extractor fixes:
  - `COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent cart-live-inspect-session` -> success, `session_mode: attached_cdp_profile`
  - `uv run python -m unittest tests.test_scrapling_adapter tests.test_live_browser_agent` -> `OK` (17 tests)
- Regression evidence:
  - Fold-below CTA recovery: `tests.test_live_browser_agent.LiveBrowserAgentTests.test_agent_scrolls_when_add_to_cart_exists_below_fold`
  - Purchase-restricted substitute recovery: `tests.test_live_browser_agent.LiveBrowserAgentTests.test_agent_replans_to_substitute_when_first_product_is_purchase_restricted`
  - False-success prevention and retry after wrong cart item: `tests.test_live_browser_agent.LiveBrowserAgentTests.test_agent_blocks_false_success_and_recovers_after_wrong_cart_verification`
- Live validation evidence:
  - Initial live run for `integration-live-request '시리얼 1개 담아줘'` failed with `purchase_restricted`; saved diagnostic HTML to `.artifacts/how31-search-page.html` and confirmed search results were mis-extracted while generic `와우회원할인` copy was over-classified as a purchase blocker.
  - Added JSON-LD-backed search result extraction, product-page-only purchase restriction classification, and product JSON-LD / `og:title` preference for selected-product identification in `coupang_cart_agent/scrapling_adapter.py`.
  - Final live run:
    - `POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/coupang_cart_agent' COUPANG_BROWSER_LAUNCH_MODE=cdp_chrome COUPANG_CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome" COUPANG_CHROME_PROFILE_DIRECTORY='Default' COUPANG_BROWSER_HEADLESS=false uv run python -m coupang_cart_agent integration-live-request '시리얼 1개 담아줘' --user-id 'telegram:8201584878' --chat-id '8201584878' --thread-id 'how31-live-cereal-rerun2'`
    - Result: `success: true`
    - Selected product: `오리온 미쯔블랙 시리얼, 360g, 1개`
    - Cart verification: `cart_count_before: 0`, `cart_count_after: 1`, `stage: verification`, `message: Item added to cart and verified.`
    - Telegram success notification was sent through the live workflow path after verification.
- Branch: `how-31-goal-driven-recovery-loop`
- Commit: handoff head recorded via `git rev-parse HEAD`
- Push: `git push origin how-31-goal-driven-recovery-loop` -> success
- PR: `https://github.com/choijhyeok/coupang_agent/pull/18`

### Follow-up Issues Created

- None
