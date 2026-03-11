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
- [in_progress] Run validation, attempt live Telegram send, publish branch, and record evidence.

### Acceptance Criteria

- [ ] 성공 시 상품명, 가격, 개수, 요약을 전송한다
- [ ] 실패 시 실패 원인과 실패 지점을 전송한다
- [ ] 메시지 길이가 과도하지 않다
- [ ] `NotificationPayload` 계약과 일치한다
- [ ] 실제 Telegram chat으로 성공 또는 실패 메시지를 1건 이상 전송한다
- [ ] local sender double만으로 완료 처리하지 않는다

### Validation Checklist

- [ ] 성공 메시지 예시 전송 확인
- [ ] 실패 메시지 예시 전송 확인
- [x] 포맷 스냅샷 테스트 또는 문자열 테스트
- [ ] 실제 Telegram `sendMessage` 검증 1건
- [ ] live token 미보유 시 blocker로 남기고 완료 처리하지 않는다
- [ ] DB에서 읽은 cart snapshot 기준으로 상품명/수량/총액이 포함된 메시지 검증 1건

### Notes And Blockers

- No checked-in runbook or existing workpad file was present in this branch.
- Live bot token is present and the Bot API is reachable.
- `uv run python - <<'PY' ... getMe/getWebhookInfo/getUpdates ... PY`
  - `getMe`: bot `@coupang_cart_bot`
  - `getWebhookInfo`: no webhook configured
  - `getUpdates`: `[]`
- Live delivery blocker:
- Failed command: `uv run python -m coupang_cart_agent send-telegram-notification --chat-id 8725154905 --scenario failure`
- Result: Telegram Bot API `HTTP Error 403: Forbidden`
- Missing external input: a real Telegram chat ID for a user/group/channel that has started a conversation with `@coupang_cart_bot`
- Local validation complete:
  - `uv run python -m unittest tests.test_notifications`
  - `uv run python -m unittest tests.test_integration`
  - `uv run python -m unittest discover -s tests`
- DB-backed message validation is covered by `tests.test_notifications.NotificationTests.test_sqlite_notification_context_store_loads_snapshot_and_prior_purchase_rows` and `test_success_message_uses_db_snapshot_and_prior_purchase_context_when_provided`.
- Publish:
  - Branch: `how-18-telegram-notifications`
  - Commit: `62211a0`
  - Push: `git push -u origin how-18-telegram-notifications`
  - PR: `https://github.com/choijhyeok/coupang_agent/pull/11`

### Follow-up Issues Created

- None
