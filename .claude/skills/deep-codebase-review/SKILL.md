---
name: deep-codebase-review
description: 深度 codebase audit + fix + review + consolidate 工作流。三階段全並行（audit agent 找問題 → fix worker 修 → reviewer 驗），每階段都並行不串行，主線禁止 idle 等待。彙整成 BLOCK/NIT 表，BLOCK 自動修 + per-branch deep review + cross-cutting integration review，全綠後 consolidate 開單一 PR。當使用者要求「找技術債 / 找 bug / review codebase / 深度 audit」時觸發。
---

# Deep Codebase Review

## 何時用

使用者明確要求「找問題 / 找 bug / 找技術債 / review / audit / 深度檢查」時觸發。**不**用於：寫新功能、單檔修改、純諮詢。

## 核心原則（從歷次 retro 固化）

1. **三階段全並行**。Audit / fix / review 每階段都派並行 agent。**禁止**用「單一 meta-reviewer」取代 per-branch reviewer — 實測會漏關鍵 BLOCK（曾因此漏一條 dead-code path BLOCK，整個 fix 退回上一輪 PR 的 fix）。
2. **Reviewer ≥ worker**。每個 fix branch ≥ 1 個 deep reviewer（從第一性原理到細節）+ 1 個 cross-cutting integration reviewer。Follow-up fix 也要 follow-up reviewer。
3. **主線禁 idle**。派出 background agent 後的下一個動作必須是「主線盤點還能做什麼獨立工作」（PR body / memory update / 下個 reviewer prompt 預寫），**不能是「等」**。idle 等待是並行架構最大失分項。
4. **Audit agent 必產 patch 草稿**，不只 findings。下游 fix worker 直接 reference patch，省一輪 root-cause 重判。
5. **Worker 必驗 entry point**，不盲抄 audit 草稿。Audit 可能漏關鍵呼叫處（曾因 worker 沒 grep `arm_session` 預創 LivePairingSession 的位置，把 freeze 邏輯放錯地方變 dead code，浪費 FX 一輪）。
6. **決策快**。BLOCK > 5 / contrarian / strategic refactor 自決，不寫 thinking 權衡段落。User 已授權 batch 就是不要被打擾。
7. **ABSOLUTE RULES** 每個 agent prompt 必含：`pwd` 驗 worktree、relative path、commit→push→回報 branch、不停下等確認、不開 PR（coordinator 統一開）。

## 流程

### Phase 1 — 範圍協商（≤1 round，可跳）

User 沒指定 domain 就問一次最多 4 個選項（multiSelect）：race / silent fallback、dead code / over-abstraction、stale docs、perf hotspot、structural debt。User 授權「全開」或不答 → default 5 domain 全跑（再加 alignment / domain-specific 若 conversation context 已暗示焦點）。

### Phase 2 — 派 audit agent（5-8 個並行）

`subagent_type: general-purpose`、`isolation: worktree`、`run_in_background: true`、`model: opus`（per memory rule）。

**Audit prompt 模板**：

```
你是 [project] 的 [DOMAIN] 稽核 agent。**只 audit + 寫 patch 草稿，不 commit/push/PR**。

## ABSOLUTE RULES
1. `pwd` 第一個動作。確認在 worktree 內。
2. Read/Edit/Write 用 relative path，禁 absolute。
3. 不修檔；只產 findings + unified-diff patch。
4. Final message 含「Findings / Proposed Patches / Contrarian view」三段。

## Setup
pwd
git fetch origin
git checkout origin/main

## 範圍
[列要看哪些檔、grep 哪些 pattern、特別懷疑哪些 hot spot]

## 輸出格式
# [DOMAIN] findings
## BLOCK 級
| file:line | 問題 | 證據 |
## NIT 級
| file:line | 問題 |
## Proposed Patches
```diff ... ```
## Contrarian view
- 看似問題實應保留：[...]
```

### Phase 3 — 彙整 + 仲裁

Audit agent 全回後，coordinator 自己做：

- 跨 agent **去重**（同檔同 line BLOCK 合併）
- **仲裁衝突**（A agent 想刪 X comment，B agent contrarian 想留 — 用 user goal 判斷誰對）
- 採納 contrarian 把高成本低收益 BLOCK **降級** NIT（如「LAN 單機效能微優化」）
- 自決派工策略：BLOCK 怎麼分組、哪些合併同一 worker（避免同檔 merge conflict）、哪些獨立

**禁止問 user**：是否照做 / 優先序 / commit message。**只**問：要動 origin push、砍 user-facing feature、cross-file strategic refactor 三類。

### Phase 4 — 派 fix worker（按 BLOCK 分組並行）

每 BLOCK 一個 worker，或同 domain 多 BLOCK 合一個 worker（例：silent-fallback 三條 = 一 worker；calibration.py split 一條 = 一 worker）。同檔 BLOCK 合併避免 merge conflict。

**Fix worker prompt 必含**：

```
## ABSOLUTE RULES（同 audit + 三條額外）
- `pwd` first
- Relative paths only
- 最後 git add → commit → push → 回報 `Branch: <name>`，不開 PR
- 不停下等使用者確認，blocker 自己診斷自己修
- worktree 沒 push = 工作消失（auto-cleanup）
- 禁跑 iOS test（per memory），只 xcodebuild build

## Setup
pwd
git fetch origin
git checkout -b claude/audit-fix-[DOMAIN] origin/main

## 任務（BLOCK list + audit 草稿 patch）
[FROM AUDIT]

## 必驗 entry point（防呆 — 不要盲抄草稿）
在動 patch 前先 grep 確認 audit 草稿 touch 對位置：
- [grep 命令 #1]：例 grep -n "arm_session\|LivePairingSession" server/state.py
- [grep 命令 #2]：...
若 grep 結果跟 audit 描述不符，先確認 root cause、再動 patch。

## 驗證
cd server && uv run pytest -x

## 完成
git add -A && git commit -m "..." && git push -u origin claude/audit-fix-[DOMAIN]
最後 message 以 `Branch: claude/audit-fix-[DOMAIN]` 結尾。
```

**派工同時主線立刻動**：開始草擬 reviewer prompt 模板（共用骨架）、PR body 大綱、memory update list。**不要 idle**。

### Phase 5 — Reviewer 並行（≥ worker count）

Worker 完成通知到時，**立刻**派該 branch 的 reviewer，**不要批次等**（worker 完成時間錯開，等批次 = 等最慢）。

每 fix branch 1 個 deep reviewer。額外派 1 個 cross-cutting integration reviewer 等 ≥3 條 fix branch push 後啟動（不必等全到）。

**Deep reviewer prompt 模板**（每 branch 只填差異）：

```
你是 [project] 的 deep reviewer，審 `claude/audit-fix-[DOMAIN]`。不寫程式 / 不 commit。

## ABSOLUTE RULES
- `pwd` first
- Relative path only
- 不修檔/不 commit/不 push
- 禁跑 iOS test，只 xcodebuild build

## Setup
git fetch origin
git checkout origin/claude/audit-fix-[DOMAIN]
git diff origin/main..HEAD --stat
git diff origin/main..HEAD

## Context（fix 預期解什麼 BLOCK）
[從 audit + worker self-report 摘要]

## 審查維度（必逐項覆蓋）
A. 第一性原理：fix 真治本？同類問題還有漏網嗎（grep 全 codebase）
B. 設計合理性：副作用、邊界 case
C. Regression：自跑 pytest 不只信 worker 自報、找有沒有「test silent fallback 還在」的反向 assert
D. 細節：error message actionable、log level、無 lazy import in hot loop
E. 測試：worker 加了哪些 test、缺什麼 regression test
F. Contrarian：有沒有引入新 silent fallback / 把 transient 變 crash

## 輸出
# Review of [branch]
## Verdict: APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES
## A-F 逐項評論
## BLOCK issues
## NIT issues
```

**Cross-cutting reviewer prompt** 額外加：

```
## 範圍：跨 branch 整合面
- Merge dry-run（每對 branch + cumulative）找 conflict
- 跨 branch state 同檔不同 hunk 邏輯交叉檢查
- 整合後新 silent fallback grep
- User goal scorecard（algorithm / pixel / payload 三層各 close 嗎）
- 重複 / 不一致（多 branch 都動同一份 doc 等）
- 推薦 merge 順序

## 輸出
- BLOCK pre-merge 必修
- 推薦 merge order
- 是否可開單一 PR consolidate
```

**Reviewer 找到 BLOCK** → coordinator 立刻派 FX (follow-up fix) 在原 branch 上加 commit，**FX 也要派 R-FX 驗證**。FX 不能略過 review。

### Phase 6 — Consolidate

Cross-cutting reviewer APPROVE 後，coordinator 一次做：

```
git checkout -b claude/audit-batch-<date> origin/main
# 按 cross-cutting reviewer 推薦順序 cumulative merge
git merge --no-ff -m "merge: [domain]" origin/claude/audit-fix-[domain]
# ... 每條一次
cd server && uv run pytest -x
git push -u origin claude/audit-batch-<date>
```

### Phase 7 — NIT batch

從 R1...Rn + cross-cutting reviewer 累積的 NIT 一次處理。**派 W8 在 consolidate branch 上 fix**（不在 origin/main 上開新 branch — 避免再一次 merge）。W8 也派 R-FINAL 驗。

### Phase 8 — Open PR + 收尾報告

```
gh pr create --title "..." --body "..."
```

PR body 包含：BLOCK 修了幾個（list）/ NIT 修了哪些 / Contrarian 留下的判斷 / Cross-cutting reviewer scorecard / 預估技術債變化（LOC delta / 風險面下降）。

**Memory update 在這階段（或更早）並行排程**：派一個 memory-update agent 改過時條目、新增 audit outcome 條目。可以在 Phase 6 consolidate 時就派出，不用等 PR open。

## 反模式（按嚴重度）

1. ❌ **派工後主線 idle 等待** — 最嚴重。每次 background dispatch 後 coordinator 必須立刻盤點獨立副線（草擬下個 prompt / 寫 PR body / 更新 memory），idle = 並行架構失敗
2. ❌ **單一 meta-reviewer 取代 per-branch reviewer** — 實測漏關鍵 BLOCK（dead-code path、entry point 錯配）
3. ❌ **Audit agent 只給 findings 沒 patch 草稿** — fix worker 重做 root cause
4. ❌ **Worker 盲抄 audit 草稿不驗 entry point** — 曾因此把 freeze 邏輯放在 dead code path
5. ❌ **Follow-up fix 不派 reviewer** — 違反 reviewer ≥ worker 規則
6. ❌ **Worker prompt 沒寫 `pwd` 強制檢查** — worktree 漂移會發生（30%+ 機率）
7. ❌ **BLOCK 上交 user 問「要不要修」** — user 已授權 batch
8. ❌ **小決策寫 thinking 權衡段** — obvious 直接做（reviewer 看到 BLOCK 就派 FX，不要寫 200 字權衡）
9. ❌ **派一個 25 分鐘 mega-task** — 無進度可見、無法早停

## User checkpoint 標準（嚴格收斂）

**只**問 user：

- 要 push origin / force push（destructive）
- 要砍 user-facing feature
- Cross-file strategic refactor（如「state.py 1700 行要不要拆」）

**自決**（不問 user）：分批策略、commit message、contrarian 採納、reviewer 找到的 BLOCK 派 FX、merge 順序、NIT 哪些做哪些放。

## 結束條件

下列任一即視為完成：

- All BLOCK 修完（含 follow-up FX）+ all per-branch reviewer APPROVE/APPROVE-WITH-NITS + cross-cutting reviewer APPROVE-CONSOLIDATION + 1 個 consolidate PR 開出 + tests 全綠
- User 主動中止
- 同一 BLOCK retry > 3 次（升級 user）

完成後 final message 貼 PR URL + BLOCK 修復總計表 + NIT 處理狀態 + cross-cutting scorecard。
