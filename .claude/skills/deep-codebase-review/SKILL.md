---
name: deep-codebase-review
description: 深度 codebase audit 工作流。派 5-8 個並行 agent 各自掃一個 domain（race / silent fallback / dead code / 過時文檔 / 效能 / 結構債），每個 agent 必產 file:line + 修法草稿。彙整成 BLOCK/NIT 排名表，BLOCK 自動派 fix worker 修，NIT 條列給使用者點選。當使用者要求「找技術債 / 找 bug / review codebase / 深度 audit」時觸發。
---

# Deep Codebase Review

## 何時用此 skill

使用者明確要求「找問題 / 找 bug / 找技術債 / review / audit / 深度檢查 codebase」時觸發。**不**用於：寫新功能、單檔修改、純諮詢。

## 核心設計原則（從歷次失敗 retrospective 固化）

1. **Audit 並行、Review 串行**。並行只在「找未知問題」階段有意義。Review 階段用單一 meta-reviewer 跑一次測試 + 看全部 diff，省 N× 成本。
2. **Audit agent 必產 patch 草稿**，不只 findings。下游 fix worker 直接 `git apply`，省一輪重讀 code。
3. **Worktree 隔離強制 `pwd` 驗證**。歷次經驗 30%+ worker 會漂移到 parent worktree。Prompt 必含 absolute rule。
4. **BLOCK 自動修，NIT 才上交**。User 授權 batch 後不該被技術細節打擾。
5. **大 task 拆小**。預估 >20 分鐘 wall-clock 的 worker 強制拆。

## 步驟

### 1. 範圍協商（最多 1 輪 question，沒有就跳過）

如果使用者沒指定 domain，問一次最多 4 個選項（multiSelect）：
- 並行 / 同步問題（race / lock / silent fallback）
- 死碼 / facade 過度抽象
- 過時文檔（CLAUDE.md / 註解 / docstring drift）
- 效能 hotspot
- 結構債 / 拆分提案

如果使用者授權「全開」或不回答，default 5 個 domain 全跑。

### 2. 派並行 audit agent（一 domain 一 agent，5-8 個）

每個 agent 用 `Agent` tool，`subagent_type: "general-purpose"`，`isolation: "worktree"`，`run_in_background: true`。

**Prompt 模板（必逐字包含「ABSOLUTE RULES」段）**：

```
你是 ball_tracker_project 的 [DOMAIN] 稽核 agent。**只 audit + 寫 patch 草稿，不 commit、不 push、不開 PR**。

## ABSOLUTE RULES（違反即視為 bug）
1. 第一個動作必須是 `pwd` 並回報路徑。確認在 worktree 內、不在 parent。
2. 所有 Read / Edit / Write 用 relative path（`server/state.py`），**禁止** absolute path（`/Users/.../server/state.py`）。
3. 不修任何檔案；只產 findings + unified-diff patch 草稿。
4. 最終 message 必須含「Findings」+「Proposed Patch」兩段。

## Setup
```
git fetch origin
git checkout origin/main  # detached HEAD 即可，不需 branch
```

## 你的 domain 範圍
[DOMAIN-SPECIFIC SCOPE：列出要看哪些檔、grep 哪些 pattern]

## 輸出格式（嚴格遵守）
# [DOMAIN] findings

## BLOCK 級
| file:line | 問題 | 證據（grep 結果 / log） |

## NIT 級
| file:line | 問題 |

## Proposed Patches（unified diff 格式）
```diff
--- a/server/foo.py
+++ b/server/foo.py
@@ -X,Y +X,Y @@
-old line
+new line
```
（每個 BLOCK 至少給一份；NIT 可選）

## Contrarian view
- 哪些第一眼看似問題實際應該保留：[]

完成後 final message 直接貼上面格式。
```

### 3. 等所有 agent 回來，彙整成總表

格式：

```
| # | domain | severity | file:line | 問題 | patch 已就緒？ |
|---|---|---|---|---|---|
| 1 | race | BLOCK | live_pairing.py:79 | ... | yes |
| 2 | silent fallback | BLOCK | state.py:785 | ... | yes |
| 3 | dead code | NIT | state.py:298 | ... | yes |
| ... contrarian ... |
```

### 4. User checkpoint（最小化 round-trip）

**只**對下列三類問 user：
- BLOCK 數量超過 5 條時，問是否分批處理還是一次到底
- Contrarian view 列表（不做的判斷需要 user 認可）
- 任何「需要跨檔大重構」的 NIT（譬如「拆分 state.py」這種 strategic 決定）

**不**問 user：是否照做、優先序、commit message wording。授權 batch 模式 = 全做、自己決定。

### 5. 派 fix worker（每個 BLOCK 一個 worker，並行）

**只對 BLOCK 派**。NIT 集中由一個 worker 處理（節省 dispatch overhead）。

每個 fix worker：
- `isolation: "worktree"`
- `run_in_background: true`
- Prompt 內含 audit agent 已產的 patch 草稿
- 預估 >20 分鐘的 task 強制拆成 2 個並行 worker

**Fix worker prompt 模板**：

```
## ABSOLUTE RULES（同 audit）
1. `pwd` first
2. Relative paths only
3. 最後動作必須是：git add → commit → push → 回報 `Branch: <name>`
4. 不開 PR（coordinator 統一開 ONE PR）

## Setup
```
pwd  # confirm worktree
git fetch origin
git checkout -b claude/audit-fix-[DOMAIN]-[N] origin/main
```

## 你的任務
[FROM AUDIT FINDINGS：file:line + 描述]

## 候選 patch（audit agent 已草擬）
```diff
[PASTE PATCH DRAFT]
```

驗證 patch 對齊當前 origin/main HEAD（line 可能漂）。可改 patch，但邏輯保持。

## 驗證
cd server && uv run pytest -x

全綠才能 push。失敗 → debug → 不要直接砍 test。

## 完成
git add -A && git commit -m "..." && git push -u origin claude/audit-fix-[DOMAIN]-[N]
最後 message：`Branch: claude/audit-fix-[DOMAIN]-[N]` 或失敗原因。
```

### 6. 串行 meta-review（單一 agent）

所有 fix branch push 後，**派 1 個** meta-reviewer agent，**不**為每個 branch 各派一個。

Meta-reviewer prompt：
- 拿到所有 fix branch list
- `git fetch origin && git checkout origin/main`
- 對每個 branch 跑 `git diff origin/main..origin/<branch>` 看 diff
- 跑一次全套 test on each（或一次合到 staging branch 跑）
- 輸出統一表：`| branch | verdict | issues |`
- BLOCK issue 自動派 follow-up fix worker；NIT 寫在報告

預估省 70-80% reviewer wall-clock vs 並行 N 個 reviewer。

### 7. Consolidate（單一 step，自己做或派 1 個 agent）

- 從 origin/main 開 `claude/audit-batch-<date>` branch
- 依序 `git merge --no-ff origin/<each-fix-branch>`
- ort strategy 通常 auto-merge 過；遇衝突 stop + report user
- `cd server && uv run pytest -x`
- `git push -u origin claude/audit-batch-<date>`
- `gh pr create` with body 列出所有改動 + verification 結果

### 8. 收尾報告

對 user 列：
- BLOCK 修了幾個（PR 列表）
- NIT 列表 + 哪些已順手修哪些待之後
- Contrarian 留下的判斷
- 預估技術債整體變化（LOC / 風險面下降）

## 反模式（明確禁止）

- ❌ 把 review 階段也並行 N 個 agent（重複跑 N× tests，token + wall-clock 浪費）
- ❌ Audit agent 只給 findings 沒給 patch 草稿（fix worker 重做一遍 root cause 分析）
- ❌ Worker prompt 沒寫 `pwd` 強制檢查（worktree 漂移會發生）
- ❌ BLOCK issue 上交 user 詢問「要不要修」（user 已授權 batch）
- ❌ 派一個 25 分鐘的 mega-task（無進度可見、無法早停、final 回報不可信）

## 結束條件

下列任一即視為完成：
- All BLOCK 已修 + 1 個 consolidate PR 開出 + tests 全綠
- User 主動中止
- 出現 review agent 無法解決的 BLOCK 且超過 3 次 retry（升級給 user）

完成後在 final message 貼 PR URL + 總計改動表。
