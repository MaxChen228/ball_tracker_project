# HTTP `/heartbeat` 退場計劃

> **Status**: Plan (agent-led implementation expected)
> **Date**: 2026-04-21
> **Scope**: 移除 HTTP `/heartbeat` endpoint + iOS `ServerHealthMonitor` HTTP 輪詢，改由 WebSocket 單一通道承擔所有命令下行 + device liveness
> **Why now**: 使用者決定不等 1-2 週穩定期，直接切單軌

---

## 1. 任務總覽（給後續 agent）

目前系統跑 **WS + HTTP heartbeat 雙軌**。這份計劃要求：

1. **分析**：完整盤點目前 HTTP heartbeat 承載的所有職責（不只是顯而易見的 arm/disarm）
2. **驗證**：確認 WebSocket 通道已經完整承擔每一項職責（或補上）
3. **施作**：刪 server `/heartbeat` + iOS `ServerHealthMonitor` HTTP 部分，保留必要的等價功能在 WS
4. **回歸**：pytest 全綠 + iOS build + 手動 smoke

Agent 被期望**自己跑一輪 analysis → design → implement**，不是照我這份文件盲刻。這份只提供**起點與驗收條件**。

---

## 2. Agent 需自行盤點的項目（起點清單）

至少必須搞清楚：

- `server/main.py` 的 `@app.post("/heartbeat")` 目前回傳的**所有欄位**（不是只看 `commands`）— 每個欄位是誰在讀、目的是什麼
- `server/main.py` 的 `HeartbeatBody` schema 上行欄位（`time_synced`, `sync_anchor_timestamp_s`, ...）— 這些訊息 WS 是否已經有等價上行？
- `ball_tracker/ServerHealthMonitor.swift` 整個檔案的職責 — 不只 POST heartbeat，還有「Last contact: N s ago」這類 UI 更新 / exp backoff / probeNow 等
- `ball_tracker/CameraViewController.swift` 所有 `healthMonitor` 的呼叫點 — 依賴 HTTP heartbeat 的 side effect
- `server/State` 內依 HTTP heartbeat 更新的狀態（`_devices` 時間戳、`_sync_command_pending` 消耗等）
- `/status` endpoint 的依賴關係 — dashboard 拉 `/status` 時 `devices` 列表哪裡來
- 測試：`server/test_server.py` 裡有多少 test 直接打 `/heartbeat`，退場後它們要怎麼遷移（改打 WS 或刪除）

**重要**：列清單前不要開始改動任何檔案。先讀懂。

---

## 3. 目標狀態

退場後：

- ❌ `server/main.py` 不再有 `/heartbeat` route
- ❌ `server/schemas.py` 可刪 `HeartbeatBody`（若無其他引用）
- ❌ `ball_tracker/ServerHealthMonitor.swift` 刪除或改造成純「WS 狀態顯示」worker（不發 HTTP）
- ✅ 所有「arm/disarm/settings/sync_command 下行」走 WS
- ✅ 所有「time_synced/sync_anchor 上行」走 WS `hello`/`heartbeat` message
- ✅ Device liveness 由 WS 連線狀態 + WS heartbeat msg 維持
- ✅ Dashboard `/status` 仍能顯示 devices 線上狀態（資料來源改為 WS connection snapshot）
- ✅ 所有 pytest + iOS build + 手動 smoke 通過

---

## 4. 關鍵設計決策（agent 自行判斷）

以下是 agent 會遇到的**必答題**，沒有標準答案，依分析結果選：

1. **WS heartbeat msg 的間隔** — 現在 HTTP 是 1s，WS msg 要不要跟？太頻繁會浪費，太稀 device 會被誤判離線
2. **斷線到「offline」的 grace** — 現在 `_DEVICE_STALE_S` 是 3s；WS disconnect 瞬間可區分，但若背景掛起要給多久寬限
3. **iOS 背景化時的 WS 行為** — `URLSessionWebSocketTask` 前景/背景切換的實際表現，會不會每次回前景都重連？若是，dashboard 會閃「device offline → online」
4. **test_server.py 舊 heartbeat tests** — 改寫成走 WS，還是直接刪（因為 WS handler 的 test coverage 已足夠）
5. **`/sync/trigger` 現在有雙推（WS + heartbeat flag）** — 退場後 `pending_sync_commands()` 是否還需要？
6. **AsyncIO event loop reentry** — `/calibration` 我已經改成 `async def`；退場時要確認其他同步 route 呼叫 `device_ws.broadcast` 時沒觸發 `asyncio.run_coroutine_threadsafe` 問題

---

## 5. PR 切分建議

Agent 可以視分析結果調整，但原則：**每個 PR 獨立可 revert**。

- **PR-1 分析 + 補齊 WS 缺的下行/上行**（純 additive，不刪東西）
- **PR-2 iOS 不再輪詢 HTTP heartbeat**（但保留 server endpoint 一週作保險）
- **PR-3 刪 server endpoint + schema + 舊 tests**（真正退場）
- **PR-4 刪 `ServerHealthMonitor` 或瘦身成純 UI worker**

若分析後發現能三步到位也 OK，agent 判斷即可。

---

## 6. 驗收條件（agent 自己跑）

- [ ] `grep -rn '/heartbeat' server/ ball_tracker/` 回傳 0 結果（或只剩註解）
- [ ] `grep -rn 'ServerHealthMonitor' ball_tracker/` 回傳 0 結果或只剩 UI-only 殘存
- [ ] `cd server && uv run pytest -q` 全綠
- [ ] iOS Xcode build 成功（target iPhone 14 Pro, Debug + Release）
- [ ] 手動 smoke：
  1. Server + app 啟動，dashboard 看到 device online
  2. Dashboard `Arm` → iPhone 反應延遲 < 200ms（現在 HTTP 是 1s，切 WS 後該顯著更快）
  3. Server 重啟一次，iPhone WS 重連，dashboard 看到 online → offline → online
  4. iPhone 切背景 10s 回前景，行為可接受（WS 重連 + UI 恢復）
  5. Dashboard 切 paths={live}，丟球，看 Active Session card frame count 正常遞增
- [ ] Dashboard `Devices` 卡片仍顯示正確的 online 狀態（從 WS 派生的）
- [ ] 程式碼行數淨減少（這次是真清理，不是搬家）

---

## 7. 風險 / 回滾

**主要風險**：iOS 背景化時 WS 被系統掛起，回前景才重連，若重連期間漏了 arm 訊息 → session 錯過。

**緩解**：WS reconnect 時立刻送 `hello` 夾帶 session_id claim；server 看到 hello 且 session armed 就重送 `arm` 訊息（這個 behavior 在現有 ws_device handler 已有，agent 需驗證）。

**回滾**：每個 PR 獨立 revert。若 PR-3（刪 endpoint）出問題，`git revert` 一次即可，iOS 端已經不 call HTTP heartbeat 所以不影響功能。

---

## 8. 期望交付

Agent 完成後應在 PR 描述或 reply 中涵蓋：

- **分析結論**：列出盤點到的所有 HTTP heartbeat 職責 + 每一項在新架構下的去向
- **設計決策**：對 §4 那幾個必答題的選擇與理由
- **實測數字**：arm 延遲 before/after、iPhone 背景切換後的 reconnect 時間
- **遺留項目**：若有來不及清的（比如 ServerHealthMonitor 只瘦身沒刪），說明為什麼 + 何時處理

---

## 9. 起手點

- 上層計劃：[docs/live_streaming_architecture_plan.md](docs/live_streaming_architecture_plan.md) §PR-I（這份是它的具體執行）
- 當前 WS 實作：[server/ws.py](server/ws.py), [server/main.py:2447](server/main.py:2447)（ws_device handler）
- 當前 HTTP heartbeat：[server/main.py:2554](server/main.py:2554)
- iOS 當前：`ball_tracker/ServerHealthMonitor.swift`, `ball_tracker/CameraViewController.swift:1458` 附近（WS）+ 相同 controller 裡 healthMonitor 的綁定
