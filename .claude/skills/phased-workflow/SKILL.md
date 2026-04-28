---
name: phased-workflow
description: 規範多階段開發工作流。把任務切成可獨立 commit 的 phase，做第 N phase 時同步派 code review agent 審 N-1 phase。預設在 main commit；若 cwd 在 worktree 則在 worktree 完成並開 PR。當使用者要求做需要多步驟的功能、refactor、bugfix 並希望結構化推進時觸發。
---

# Phased Workflow

## 何時用此 skill

使用者用 `/phased-workflow` 觸發，或明確要求「分階段做 / 邊做邊 review / phased commit」。

任務若無法切成 ≥2 個獨立 phase（例如改一行字、純諮詢），直接退出此 skill 並照一般方式做。

## 步驟

### 1. 規劃 phase

先把任務拆成 N 個 phase，**N ≥ 2**，每個 phase 必須：

- 能獨立 commit（commit 後 repo 處於 working / 可編譯 / 測試可跑的狀態）
- 有清楚的 scope 邊界與 commit message 草稿
- 不依賴未來 phase 才會出現的程式碼

把 phase 列表用 TodoWrite 寫下來，每個 phase 一個 todo，再加一個「Phase N-1 review feedback 處理」的 todo（在 phase 2 之後加入）。

把 phase 計畫拿給使用者確認再動手。phase 切錯比 review 漏掉更貴。

### 2. 偵測 branch 策略

開工前跑一次：

```bash
[ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ] && echo WORKTREE || echo MAIN
```

- `MAIN` → 在當前 branch（通常是 main）直接 commit，不開 PR。
- `WORKTREE` → 在 worktree 完成所有 phase，最後 push 並用 `gh pr create` 開 PR。

使用者若在外部已經 `cd` 進 worktree、或透過 `Agent({ isolation: "worktree" })` 進入，都會被這個檢查抓到。

### 3. 執行 phase 1

正常做、正常 commit。記下 commit hash 給 phase 2 用。

不派 review agent（沒有前一個 phase 可審）。

### 4. 執行 phase N（N ≥ 2）

**同一個訊息裡同時送出兩件事**：

(a) **背景派 review agent 審 phase N-1**

```
Agent({
  subagent_type: "general-purpose",
  description: "Review phase N-1",
  run_in_background: true,
  prompt: <self-contained>
})
```

prompt 必須含：
- phase N-1 的 commit hash（`git log -1 --format=%H`）
- 該 phase 的目標與 scope
- 要審的重點（正確性、邊界條件、與既有 code 的契合、是否引入 dead code、安全 / 風格）
- 明確指示「只審這個 commit 的 diff，回傳 issues 清單，不要寫 code」
- 限定回傳格式：`severity (block / nit) | file:line | issue` 條列

(b) **主執行緒直接開始做 phase N 的程式碼**

不要等 review。review agent 會在背景跑，完成時系統會通知。

### 5. 處理 review 結果

當 phase N-1 的 review agent 回傳：

- **無 block 級 issue** → 把 nit 級的紀錄到 todo「Phase N-1 review feedback 處理」，phase N commit 後一起或分開處理。
- **有 block 級 issue** → 評估：
  - 影響 phase N 正在動的檔案 → 暫停 phase N，先修 phase N-1 的問題（用 fixup commit 或新 commit，**不要 amend 已 push 的 commit**），再回頭做 phase N。
  - 與 phase N 無關 → phase N 做完再修。

review 結果不可無聲忽略；必須在使用者面前明示處理方式。

### 6. 最後一個 phase

最後一個 phase commit 後，**還要再派一次 review agent** 審最後 phase（不然最後一個 phase 沒人審）。等這次 review 回傳並處理完才算完成。

### 7. 收尾

- `MAIN` 模式：報告所有 commit hash + 各 phase 的 review 結論摘要。不 push（除非使用者另外要求）。
- `WORKTREE` 模式：`git push -u origin <branch>` → `gh pr create`，PR body 列出每個 phase 的 commit + review 結論。

## 邊界與注意

- **不要平行做 phase**：phase 是順序的，平行的只有「做 N」與「審 N-1」。
- **不要把 review agent 改成同步等待**：那會浪費 phase N 的時間。
- **不要 amend 已被 review agent 看過的 commit**：會讓回傳的 hash 失效。需要修就加新 commit。
- **單一 phase 任務**：此 skill 不適用，照一般流程做。
- **CLAUDE.md 規則優先**：本專案禁用 silent fallback、禁止實驗階段向後相容等規則仍適用，review agent 也要被告知這些規則（在 prompt 裡帶到）。
