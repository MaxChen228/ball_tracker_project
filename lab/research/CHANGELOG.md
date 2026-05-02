# CHANGELOG

## 2026-05-02 — 9 sessions → 15 sessions refresh

GT 規模從 9 sessions / 1073 ball-in frames 擴到 **15 sessions / 1949 ball-in frames**
（mask 總量 ~2553；多出來的是新 SAM2 propagate 完成的 6 對 session）。

`lab-research/` 同步搬到 `lab/research/`，path 改為 marker-based ROOT
（見 `scripts/_paths.py`）。

### 主要結論：**全部 hold 住，物理 ceiling 不動**

| Metric | 9 sessions | 15 sessions | Δ |
|---|---|---|---|
| V11 (HSV+CC) R | 0.905 | **0.915** | +1.0pp |
| V11 R (different metric in 21_yd) | — | 0.898 | — |
| Y-diff(thr=15) alone | 0.519 | 0.468 | −5.1pp |
| V11 ∪ Y-diff(15) | 0.970 | **0.971** | +0.1pp ✓ |
| V11 ∪ Y-diff(30) | — | 0.976 | new |
| V11 ∪ all-3 (yd15+yd30) | 0.980 | **0.983** | +0.3pp |
| MI(V11, Y-diff15) bits | 0.009 | **0.0094** | +0.0004 (內變化) |
| V11 ∪ FRST | 0.996 | **0.998** | +0.2pp（FP 9730/frame，仍不可用） |

**Cue independence 結論 locked**：MI ≈ 0.009 在 ±0.001 內穩定，V11 與 Y-diff
仍 near-maximally independent。Gen 4（V11+Y-diff）作為 deploy candidate 不變。

### Mode-specific（V11 miss recovery）

`21_yplane_diff` thr=10 結果（15 sessions）：

| Mode | n_miss | rec by yd | rec % |
|---|---|---|---|
| M1 (specular) | 185 | 105 | 56.8% |
| M2 (fragmentation) | 11 | 4 | 36.4% |
| M3 (hue-shift) | **3** | 0 | 0% |

⚠ **M3 樣本反而更少了**（9-sess: n=9 → 15-sess: n=3）。可能：
新 6 sessions 採光條件較均勻，hue-shift 失敗模式較少；或新 propagate 的
mask 邊界較緊未踩到先前 M3 案例。需要重新跑 `15_v11_failure_modes.py` 或
看 `26_residual_table.json` 的 mode 分佈確認。

### Consensus residual（單幀失敗死區）

新增 33 frames 進入 `outputs/_figures/`（vs 9-sess 時 21 frames），符合
1.96%→1.69% 的稀有率預期。詳細 mode 分佈見 `outputs/26_residual_table.json`。

### Falsified（不變）

以下三條負結果在 15-session 規模仍然失敗，**code 移到
`scripts/_archive/falsified/`**：

- `25_dichromatic.py` — ISP 已 clip 到純灰白，模型前提不成立
- `23_ensemble_distillation.py` — 1073→1949 frames 仍不足以從 scratch 訓
- `22_dl_upper_bound.py` — 同上

### 結構改動

- `lab-research/` → `lab/research/`
- 新增 `scripts/_paths.py`（marker-based ROOT、manifest v1↔v2 適配、
  alpha-PNG mask reader）
- 新增頂層 `MAP.md`（單頁全景）
- `outputs/26_residual_*.png` (33 張) 移到 `outputs/_figures/`
- 28 支 script 改用 `from _paths import ROOT, WS, OUT, ...`，
  舊 inline `parents[2]` 全砍

### 未動

- Notes 內容（仍是 9-session baseline 的數字）— 主要 lock 結論不變，
  數字小幅偏差但不改變判斷。要逐筆修 notes 內 9-session 數字成本高、
  價值低，由 `MAP.md` 與本 CHANGELOG 蓋掉。
- `iOS` 端 `ball_tracker/BallDetector.mm` — 仍是 V11 (HSV+CC+gate)，
  Gen 4 (Y-diff) **未整合**。
