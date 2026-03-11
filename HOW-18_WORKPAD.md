## Codex Workpad

### Environment Stamp

- Issue: `HOW-18`
- Type: feature module
- Workspace: `/Users/jaehyeokchoi/code/coupang-cart-workspaces/HOW-18`
- Branch at start: `main`
- Date: `2026-03-11`

### Plan

- [completed] Inspect existing notification contracts, sender path, CLI surface, and validation hooks.
- [completed] Implement formatter/sender separation with Telegram live adapter support.
- [completed] Add DB-backed cart snapshot and prior purchase context message composition.
- [completed] Add targeted tests and operator CLI for example success/failure sends.
- [completed] Run validation, attempt live Telegram send, publish branch, and record evidence.

### Acceptance Criteria

- [x] 성공 시 상품명, 가격, 개수, 요약을 전송한다
- [x] 실패 시 실패 원인과 실패 지점을 전송한다
- [x] 메시지 길이가 과도하지 않다
- [x] `NotificationPayload` 계약과 일치한다
- [x] 실제 Telegram chat으로 성공 또는 실패 메시지를 1건 이상 전송한다
- [x] local sender double만으로 완료 처리하지 않는다

### Validation Checklist

- [x] 성공 메시지 예시 전송 확인
- [x] 실패 메시지 예시 전송 확인
- [x] 포맷 스냅샷 테스트 또는 문자열 테스트
- [x] 실제 Telegram `sendMessage` 검증 1건
- [x] live token 미보유 시 blocker로 남기고 완료 처리하지 않는다
- [x] DB에서 읽은 cart snapshot 기준으로 상품명/수량/총액이 포함된 메시지 검증 1건

### Notes And Blockers

- No checked-in runbook or existing workpad file was present in this branch.
- Live bot token is present and the Bot API is reachable.
- `uv run python - <<'PY' ... getMe/getWebhookInfo/getUpdates ... PY`
  - `getMe`: bot `@coupang_cart_bot`
  - `getWebhookInfo`: no webhook configured
  - initial `getUpdates`: `[]`
  - later `getUpdates`: captured reachable chat `8201584878`
- Live Telegram validation completed:
  - failure send:
    `uv run python -m coupang_cart_agent send-telegram-notification --chat-id 8201584878 --scenario failure --failure-stage cart_add --failure-reason "장바구니 버튼 탐색에 실패했습니다." --failure-detail "상품 페이지에서 버튼 셀렉터가 확인되지 않았습니다."`
  - success send with DB-backed snapshot context:
    `uv run python -m coupang_cart_agent send-telegram-notification --chat-id 8201584878 --scenario success --user-id telegram:8201584878 --database-path tmp/how18_notification_live.sqlite3`
- Local validation complete:
  - `uv run python -m unittest tests.test_notifications`
  - `uv run python -m unittest tests.test_integration`
  - `uv run python -m unittest discover -s tests`
- DB-backed message validation is covered by `tests.test_notifications.NotificationTests.test_sqlite_notification_context_store_loads_snapshot_and_prior_purchase_rows` and `test_success_message_uses_db_snapshot_and_prior_purchase_context_when_provided`.
- Publish:
  - Branch: `how-18-telegram-notifications`
  - Commit: branch tip on `origin/how-18-telegram-notifications`
  - Push: `git push -u origin how-18-telegram-notifications`
  - PR: `https://github.com/choijhyeok/coupang_agent/pull/11`

### Follow-up Issues Created

- None
