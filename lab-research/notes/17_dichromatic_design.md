# 17 — Dichromatic specular separation 設計（Idea I-spec）

接續 [`02_v11_followup.md`](02_v11_followup.md)、[`08_yplane_diff.md`](08_yplane_diff.md)。
V11 baseline R=0.905；V11∪Y-diff R=0.970；剩餘 ~32 miss 主集中於 M1
（specular / desat），Y-diff 對 M1 救回 75.6% 已封頂。

本筆記設計 Yang et al. 2010 specular separation 評估，**核心研究問題**：
Shafer 1985 dichromatic reflection model 在我們資料的 Mode α failure 上
是否成立？分離 I_specular 後，剩下的 I_diffuse 能否恢復球的「真實藍色」
讓 V11 cube 救回？

腳本：
- `lab-research/scripts/25_dichromatic.py`（主評估）
- `lab-research/scripts/_dichromatic_sanity.py`（單幀 5-frame 物理檢查，先跑）
- `lab-research/scripts/_dichromatic_sweep.py`（全 1073 GT 群體 S-shift）

---

## 1. Dichromatic model ↔ Mode α 物理對應

Shafer 1985 dichromatic reflection model（DRM）：

```
I(x) = I_diffuse(x) + I_specular(x)
     = ρ_d(x) · L · cos(θ_i) + ρ_s · L · g(geom)
```

- I_diffuse：與表面顏色（depth-of-color）相關，本徵藍色
- I_specular：與光源顏色（illuminant chroma）相關，於白光下接近 RGB-equal
- 在足夠強的高光下 I_specular 主導 → 像素呈現「白底有藍 tint」
  → HSV 飽和度 S 崩潰（V11 s_min=120 cube 切掉）

**Mode α 假設**：M1 frame 的球被高光蓋住，pixel ≈ blue_diffuse + white_specular。
若能 algorithmically 估計 white_specular 並減去，剩下的 blue_diffuse 應
恢復 S ≥ 120 的本徵藍色 → V11 救回。

**前置驗證（需要在實作前 confirm 假設前提）**：實測 M1 GT pixel 的 BGR
分布。若 B/G/R 三通道近乎相等（pixel 已完全白化），代表並非「diffuse +
specular 疊加」而是「球本身就是白/灰」（HDR 過曝、capture-side AE 鎖
不住、或材質低反照率被光打飛），dichromatic 分解無物理可分。

從 `miss_run_physics.csv`（170a6a89_b 連續 31-frame miss run 開頭，
src=678-687）讀到：

```
gt_b ≈ 127-149, gt_g ≈ 125-146, gt_r ≈ 122-144   → 三通道差 < 6/255
gt_s ≈ 8-13                                       → 已飽和度極低
```

**強訊號**：這些 frame 的球幾乎是純白 / 純灰，**沒有殘留的藍色 diffuse**
可分離。這已是對假設的部分 falsification。但 Mode α 不是均質的：
保留實作以驗證 mid-S（S=45-100）的 M1 frame 上是否還有東西可救。

---

## 2. Yang 2010 algorithm summary（簡化版）

核心：max-chromaticity σ = max(R,G,B) / (R+G+B)。

- 純 diffuse pixel：σ 大致穩定，反映表面色
- 純 specular pixel（白光）：σ → 1/3（三通道相等）

**Yang 2010 主軸**：用 **bilateral filter 在 σ 域**逐次更新
"reference diffuse chromaticity"，把鄰域內的最大 σ propagate 到當前
pixel（因為 bilateral 跨高 intensity 邊界但跨低 chroma 邊界保留），然後
利用 σ_d → I_specular 的封閉解 subtract 出 specular component。

實作（OpenCV-only，無新依賴）：

```
f = bgr / 255
I = sum(f, axis=2)
σ = max(f, axis=2) / I
σ_d ← σ
for k in range(n_iter):
    σ_d = bilateralFilter(σ_d, sigmaColor=0.1, sigmaSpace=5)
    σ_d = clip(max(σ_d, σ), upper=1)         # never below observed σ
# Solve for I_s assuming achromatic specular (white illuminant):
#   max_c = σ_d · I_d + I_s/3,   I = I_d + I_s
#   ⇒ I_s = 3·(max_c − σ_d·I) / (1 − 3·σ_d)
I_s = clip(3*(max_c - σ_d*I) / (1 - 3*σ_d), 0, I)
diffuse = clip(f - I_s/3, 0, 1)
```

n_iter=3 在 1080p 上估 30-60 ms（純 Python），不適合 live；本評估純做
研究問題，不考慮 live latency。

---

## 3. 失敗情境預測

### 3a. 非白光源 illuminant

Yang 2010 假設 specular = 白色（achromatic）。場館燈若偏黃 (3000K)，真實
specular = 黃色，把它當白色 subtract 會錯誤：對藍色 pixel 多扣藍 → 反而
**降低 GT-region S**。

我們 9 sessions 是何種光源？`miss_run_physics.csv` 全 frame `global_v ≈
129-138` 暗場景（戶外傍晚/室內），未知 illuminant chroma。**不做 white
balance**：先看「假設成立」的上限是什麼，避免引入新自由度污染結論。

### 3b. 球本身過曝（核心風險）

如上節，170a6a89_b 起點 31-frame miss run 的 BGR 三通道差 < 6/255。
這代表 capture-side ISP 已把訊息抹平，**dichromatic 分解無資料可分**。
預測：S_post − S_pre ≈ 0 在最深 desat frame。

### 3c. 鄰域被同樣 specular 污染

bilateral filter 用鄰域估 σ_d。若球周圍背景也偏白（例如背景是天空、白
地板），σ_d 估計被拉到 1/3，分解失效。

### 3d. 副作用

對非 M1 frame（ball 飽和度正常）跑分解，可能誤把球的 diffuse 訊號當
specular 扣掉 → 破壞 V11 baseline。Mode B（只 ROI 內 separate）可規避。

---

## 4. 評估 protocol

### 4a. 兩種 application mode

| Mode | 範圍 | 假設 | 風險 |
|------|------|------|------|
| **A** Full-frame preprocess | 整 1920x1080 跑 separation 再餵 V11 | 無偏移、簡潔對照 | 對非 M1 frame 可能破壞 baseline |
| **B** ROI-only inside V11∪Y-diff candidates | 只在候選 ROI（含 Y-diff 給的 motion blob）跑 separation，inside-ROI 用更寬鬆 HSV gate（s_min 從 120 降至 60） | 副作用最小、能堆疊在已 ship 的 V11∪Y-diff 路徑上 | ROI 不含 ball 時無救 |

**先跑 Mode B（primary）**：因為 V11∪Y-diff 已 0.970，目標只是搶剩下
~32 frame；Mode A 對非 M1 frame 的 side-effect 風險高，僅作為診斷對照。

### 4b. 評估指標

1. **Macro R**：1073 GT 全集 V11∪Y-diff vs V11∪Y-diff∪dichromatic
2. **Mode α (M1) recovery**：n_M1_saved / n_M1（baseline V11∪Y-diff 已救
   的不算）
3. **Side-effect**：對 non-M1 frames，V11 hit 是否被 separation 破壞
4. **GT-region S shift**（核心物理檢驗）：M1 frame 上 S_post − S_pre 分布

### 4c. 通過 / 不通過 baseline

V11∪Y-diff = 0.970 是現任 SOTA。dichromatic 加進去：

- R > 0.975 → confirmed，dichromatic 在我們資料上 work
- R ∈ [0.970, 0.975] → partial，救少數但物理方向對
- R = 0.970（純疊加同 frame）→ falsified，Yang 救的 frame Y-diff 都救過
- R < 0.970 → 副作用 dominate，falsified 且 harmful

### 4d. 視覺化

對 5 個經典 M1 frame（170a6a89_b 中段選取）輸出 4-panel：
原圖 / specular component / diffuse output / V11 mask before-vs-after。
寫進 `outputs/25_dichromatic_visu_*.png`。

---

## 5. 已預先驗證（單幀 sanity）

`_dichromatic_sanity.py` 在 5 個 M1 frame 跑前後對比：

| src | local | n_GT | H pre→post | S pre→post | V pre→post | spec_I |
|-----|-------|------|------------|------------|------------|--------|
| 678 | 0   | 77  | 102.7→102.7 | 10.7→**12.1** | 127→96  | 30 |
| 700 | 22  | 127 | 103→103     | 87→**94**     | 70→57   | 12 |
| 720 | 42  | 241 | 101→101     | 100→**117**   | 105→67  | 37 |
| 740 | 62  | 310 | 104→104     | 125→**134**   | 92→73   | 19 |
| 760 | 82  | 421 | 106→106     | 158→**163**   | 99→90   |  9 |

**讀法**：

1. H 完全不動（dichromatic separation 不改 hue，符合理論）
2. V 都下降（specular 是「亮 + 白」→ subtract 後 V 變低，符合理論）
3. **S 上升幅度與初始 S 正相關**：低 S 救不上來（src=678 的 ball 已純灰，
   S 10.7→12.1 仍遠低於 cube 120），中 S 才有效（src=720 100→117 越過
   100，src=740 已從 cube 外進入 cube 內 125→134）

**結論**：物理模型在「中度 desat」上 work，在「極端過曝」上 falsified。
這是預期且物理合理的——pixel 已 saturate 到全白，沒有藍色 diffuse 殘留
可分。

繼續走 Phase 2 主評估，量化「中度 desat」這部份能在 1073 GT 上救回多少。

---

## 6. 實作清單

`scripts/25_dichromatic.py`：

1. `yang_separate(bgr, n_iter=3)` — 上節實作
2. `eval_full_frame(items)` — Mode A：preprocess 後跑 V11 detect
3. `eval_roi_only(items, roi_radius=40)` — Mode B：在 V11∪Y-diff 候選
   ROI 內 separation + 寬鬆 HSV gate (s_min=60, h 同 V11)
4. 輸出 `outputs/25_dichromatic.npz`：per-frame hit_v11 / hit_yd /
   hit_dich_A / hit_dich_B / mode (M1/M2/M3/HIT) / GT_S_pre / GT_S_post
5. 視覺化 5 frames 寫 `outputs/25_visu_*.png`

`notes/18_dichromatic_results.md` 報告結構於 Phase 3 撰寫。
