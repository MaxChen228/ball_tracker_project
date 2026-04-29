---
name: deep-codebase-review
description: 深度 codebase audit + fix + review + consolidate 工作流。三階段全並行（audit agent 找問題 → fix worker 修 → reviewer 驗），每階段都並行不串行，主線禁止 idle 等待。彙整成 BLOCK/NIT 表，BLOCK 自動修 + per-branch deep review + cross-cutting integration review，全綠後 consolidate 開單一 PR。當使用者要求「找技術債 / 找 bug / review codebase / 深度 audit」時觸發。
---

# Deep Codebase Review

## 何時用

使用者明確要求「找問題 / 找 bug / 找技術債 / review / audit / 深度檢查」時觸發。**不**用於：寫新功能、單檔修改、純諮詢。

## 核心原則（從歷次 retro 固化）

1. **三階段全並行**。Audit / fix / review 每階段都派並行 agent。**禁止**用「單一 meta-reviewer」取代 per-branch reviewer — 實測會漏關鍵 BLOCK（曾因此漏一條 dead-code path BLOCK，整個 fix 退回上一輪 PR 的 fix）。
2. **Reviewer ≥ worker**。每個 fix branch ≥ 1 個 deep reviewer + 1 個 cross-cutting integration reviewer。Follow-up fix（FX）也要 follow-up reviewer（R-FX）。FX 不能略過 review。
3. **主線禁 idle**。派出 background agent 後的下一個動作必須是「主線盤點還能做什麼獨立工作」（PR body / memory update / 下個 reviewer prompt 預寫），**不能是「等」**。
4. **Audit agent 必產 patch 草稿**，不只 findings。下游 fix worker 直接 reference patch，省一輪 root-cause 重判。
5. **Worker 必驗 entry point**，不盲抄 audit 草稿。
6. **Main hash freeze**。Phase 0 record `BASE_HASH = git rev-parse origin/main`。每個 reviewer / cross-cutting agent 都要驗 `origin/main` 沒漂；漂了就 force fix branch rebase。**audit 進行中 user 仍可能 push main**，這是常態不是異常。
7. **Push 後立刻抽樣驗**。Worker 回報 `Branch: <name>` 後，coordinator **第一個動作**是 `git ls-remote origin <branch>` + `git diff origin/main..origin/<branch> --stat`，看改動範圍含不含預期檔案。**signal 漏在 worker self-report 與 reviewer 之間最危險** — 曾因此 worker force-push 錯 commit (W6 內容變 W4) 靜默通過，consolidate 才爆。
8. **決策快**。BLOCK > 5 / contrarian / strategic refactor 自決，不寫 thinking 權衡段落。
9. **ABSOLUTE RULES** 每個 agent prompt 必含：`pwd` 驗 worktree、relative path、commit→push→回報 branch、不停下等確認、不開 PR（coordinator 統一開）。

## 三條歷史事故 — 看完就知道為什麼有上面的規則

| 事故 | 表象 | 根因 | 救法 |
|---|---|---|---|
| Worker idle 等 monitor | result summary「Wait for monitor.」沒 `Branch:`，worktree auto-cleanup → 9.5 min work 全消失 | prompt 沒禁 idle；worker 自己想停下確認 | prompt 加「最後一個 tool call 必須是 push、不可中間 idle」；coordinator 看到 result 沒 `Branch:` 結束行立刻 query origin |
| FX-rebase 推錯 commit | force-with-lease push 完成、pytest 綠燈、cherry-pick 拿錯 commit hash（W4 docs 替 W6 tests）→ 整個 W6 內容靜默消失 | force-with-lease 只擋併發、不擋語意錯；rebase agent 沒 sanity check「diff 含預期檔案」 | rebase/cherry-pick agent 必跑 `git diff --stat` 並 assert 預期檔；coordinator 抽樣驗；reflog rescue（`git reflog show origin/<branch>`） |
| Cumulative silent revert | 7 branch base 在 audit 開始時 main，audit 進行中 user 推進 main 含 `presets.py` centralize；3-way merge text-clean 把 main 的 refactor 整段吞掉，pytest 仍綠 | branch staleness × text-clean merge × 「pytest 綠 = 整合正確」誤判 | cross-cutting reviewer 必驗「main 上 audit 期間新加的 commit / 新檔還在 cumulative tree」、不只看 pytest 綠

## 流程

### Phase 0 — Snapshot main hash（必做）

```
git fetch origin
BASE_HASH=$(git rev-parse origin/main)
```

把 `BASE_HASH` 記在主 agent context（`audit BASE_HASH = <hash>`）。**所有 fix branch 必須 base on 這個 hash**。每次 reviewer / cross-cutting / consolidate 前都比對 `origin/main` 是否已飄離 `BASE_HASH`：

- 若 `origin/main == BASE_HASH` → 正常推進
- 若飄離 → **派 rebase fix**：把所有 active fix branch rebase 到當前 origin/main，cumulative reviewer 重跑（這次 batch 跑了 RX1 → RX2 → RX3 三輪 cross-cutting，origin/main 中間漂 4 次）

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
## ABSOLUTE RULES（同 audit + 五條額外）
- `pwd` first
- Relative paths only
- 最後 git add → commit → push → 回報 `Branch: <name>`，不開 PR
- 不停下等使用者確認、不等任何 monitor、不做「等審視」這類 idle 動作。
  最後一個 tool call 必須是 `git push`。Blocker 自己診斷自己修。
- worktree 沒 push = 工作消失（auto-cleanup）
- 禁跑 iOS test（per memory），只 xcodebuild build

## Setup
pwd
git fetch origin
git checkout -b claude/audit-fix-[DOMAIN] origin/main  # base on BASE_HASH

## 任務（BLOCK list + audit 草稿 patch）
[FROM AUDIT]

## 必驗 entry point（防呆 — 不要盲抄草稿）
在動 patch 前先 grep 確認 audit 草稿 touch 對位置：
- [grep 命令 #1]：例 grep -n "arm_session\|LivePairingSession" server/state.py
- [grep 命令 #2]：...
若 grep 結果跟 audit 描述不符，先確認 root cause、再動 patch。

## Push 前 sanity check（必跑）
git diff origin/main..HEAD --stat
# 自問：列出來的檔案是不是「我這次 BLOCK 應該動的檔」？
# 若出現預期外的檔（如 W6 應加 test_state*，但 diff 沒列）→ reset 重做
# 若預期該動的檔沒在 diff 上 → 同樣 reset 重做
# 不對就不要 push

## 驗證
cd server && uv run pytest -x

## 完成
git add -A && git commit -m "..." && git push -u origin claude/audit-fix-[DOMAIN]
最後 message 以 `Branch: claude/audit-fix-[DOMAIN]` 結尾。
```

**派工同時主線立刻動**：開始草擬 reviewer prompt 模板（共用骨架）、PR body 大綱、memory update list。**不要 idle**。

### Phase 4.5 — Worker post-completion reflex（必做，本 batch 教訓）

Worker `Branch: <name>` 回報後，coordinator **第一個動作**（早於派 reviewer）：

```bash
git fetch origin
git ls-remote origin claude/audit-fix-[DOMAIN]  # branch 真的在 origin？
git log --oneline origin/main..origin/claude/audit-fix-[DOMAIN]  # 真的有 worker 的 commit？
git diff origin/main..origin/claude/audit-fix-[DOMAIN] --stat | head -10  # 改動範圍含預期檔案？
```

異常徵兆 → 立刻處理，**不要派 reviewer 後才發現**：

| 徵兆 | 處理 |
|---|---|
| Worker result 沒有 `Branch:` 結束行 | branch 大概率不存在（worktree auto-cleaned）。git ls-remote 確認，重派 worker。 |
| commit message 不是 worker 該寫的內容 | force-push 推錯。`git reflog show origin/<branch>` 找原始 commit，cherry-pick 救回。 |
| diff 範圍不含預期檔（例：worker 該寫 test_X.py 但 diff 沒列） | worker 漏做或推錯。診斷後重派或補修。 |
| diff 範圍多了不該有的檔（例：W7 dead-code 出現 docs/* 改動） | rebase stale base，跨進別 branch 的改動。立刻 rebase。 |

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
- Main hash drift check：當前 origin/main hash vs audit BASE_HASH。
  漂了就列出 audit 期間 main 加進的所有 commit，逐一驗 cumulative tree
  是否含這些 commit 的內容（檢新檔存在、新 import 形式存在、新 API 用法存在）。
  **pytest 綠不算數** — branch 自帶配套會讓 main refactor 被靜默吞掉但 test 仍綠。
- Merge dry-run（每對 branch + cumulative）找 conflict
- 跨 branch state 同檔不同 hunk 邏輯交叉檢查
- 整合後新 silent fallback grep
- User goal scorecard（algorithm / pixel / payload 三層各 close 嗎）
- 重複 / 不一致（多 branch 都動同一份 doc 等）
- 推薦 merge 順序

## 輸出
- BLOCK pre-merge 必修（含 staleness — 列要 rebase 哪些 branch）
- 推薦 merge order
- 是否可開單一 PR consolidate（NEEDS-FX-FIRST / APPROVE-CONSOLIDATION）
```

**Reviewer 找到 BLOCK** → coordinator 立刻派 FX (follow-up fix) 在原 branch 上加 commit，**FX 也要派 R-FX 驗證**。FX 不能略過 review。

**Cross-cutting NEEDS-FX-FIRST → 派 rebase 後**，cross-cutting reviewer **必再跑一輪**（RX2、RX3...）。本 batch 跑了 3 輪 cross-cutting 才止血，因為 user 持續 push main。每輪都要重 fetch + 重比 hash。

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

1. ❌ **「pytest 綠 = 整合正確」誤判** — 最隱形。Branch 自帶配套會讓 main 上 audit 期間新加的 refactor 被 silently 吞掉、test 仍綠。Cross-cutting reviewer 必驗 main hash drift + 列「audit 期間 main 加的 commit / 新檔」逐條看 cumulative tree 是否含。
2. ❌ **派工後主線 idle 等待** — 每次 background dispatch 後 coordinator 必須立刻盤點獨立副線（草擬下個 prompt / 寫 PR body / 更新 memory），idle = 並行架構失敗。
3. ❌ **Worker `Branch:` 結束行沒驗證就派 reviewer** — Phase 4.5 的 `git ls-remote` + `git diff --stat` 抽樣是必跑。沒驗會讓 force-push 推錯 commit 透過（W6 內容變 W4 事件）。
4. ❌ **Worker 中間 idle 等 monitor** — worker prompt 沒禁就會發生。result 結尾「Wait for monitor.」沒 `Branch:` 結束行 → worktree auto-cleanup → 整段 work 消失。prompt 必含「最後一個 tool call 必須是 git push」。
5. ❌ **單一 meta-reviewer 取代 per-branch reviewer** — 實測漏關鍵 BLOCK（dead-code path、entry point 錯配）。
6. ❌ **Audit agent 只給 findings 沒 patch 草稿** — fix worker 重做 root cause。
7. ❌ **Worker 盲抄 audit 草稿不驗 entry point** — 曾因此把 freeze 邏輯放在 dead code path。
8. ❌ **Follow-up fix 不派 reviewer** — 違反 reviewer ≥ worker 規則。
9. ❌ **Worker prompt 沒寫 `pwd` 強制檢查** — worktree 漂移會發生（30%+ 機率）。
10. ❌ **BLOCK 上交 user 問「要不要修」** — user 已授權 batch。
11. ❌ **小決策寫 thinking 權衡段** — obvious 直接做（reviewer 看到 BLOCK 就派 FX，不要寫 200 字權衡）。
12. ❌ **派一個 25 分鐘 mega-task** — 無進度可見、無法早停。
13. ❌ **`force-with-lease` 當作 push safety net** — 它只擋併發、不擋語意錯。Push 前要 worker 自跑 `git diff --stat` sanity check + coordinator 抽樣驗。
14. ❌ **Branch base 不固定** — fix branch 必須全 base on Phase 0 的 BASE_HASH，不要混 base。混 base 會讓 cumulative merge 出現詭異 reverse delete diff。

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
