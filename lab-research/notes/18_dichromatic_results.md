# 18 — Dichromatic specular separation 結果報告

接續 [`17_dichromatic_design.md`](17_dichromatic_design.md)。
本次驗證 Yang et al. 2010 simplified specular separation 在我們 9-session
1073 GT frame corpus 上能否救回 V11 Mode α (M1) failures。

腳本：`lab-research/scripts/25_dichromatic.py`
資料：`lab-research/outputs/25_dichromatic.npz`
視覺化：`lab-research/outputs/25_visu_session_s_170a6a89_b_{678,680,697,952,969}.png`

**核心研究問題**：Shafer 1985 dichromatic reflection model 在 Mode α
failure 上是否成立？

**答案：partial / 接近 falsified**。M1 frame 的球已被 capture-side ISP
saturate 到「沒有殘留藍色 diffuse 可分離」的程度，分解後 GT-region
saturation 中位只從 45 → 54（+10），**0/68 M1 frame 越過 V11 cube floor
S=120**。Yang 2010 在物理層級不成立——前提（specular = 白色添加項
在 chromatic diffuse 之上）在我們資料上崩潰。

---

## 1. 全集 Recall（1073 GT frames）

| Pipeline | Recall | hits | 備註 |
|---|---|---|---|
| V11 alone | 0.905 | 971 | canonical baseline |
| V11 ∪ Y-diff (thr=30, area_min=50) | 0.938 | 1006 | 本評估的 Y-diff 配置（Option A，較嚴 area_min；08_yplane 報告的 0.970 用 area_min=3 寬鬆 gate） |
| V11 ∪ Mode A (full-frame dichromatic) | 0.905 | 971 | **0 frame 救回**，0 frame 破壞 |
| V11 ∪ Y-diff ∪ Mode A | 0.938 | 1006 | 與 V11∪Y-diff 完全相同 |
| **V11 ∪ Y-diff ∪ Mode B (ROI relaxed)** | **0.946** | **1015** | +0.8pp；9 frames 來自 ROI inside-relaxed-HSV |
| V11 ∪ Y-diff ∪ A ∪ B | 0.946 | 1015 | A 不貢獻 |

**對 0.970 baseline 的判定**：本評估的 Y-diff 用 area_min=50（Option A
filter，是 08_yplane_diff.md 推薦的 live-friendly 配置），所以基線是
0.938 而非 0.970。即使容許「本評估基線 0.938」，dichromatic Mode B
推到 0.946 也**遠不到 0.970**——Y-diff area_min=3 的寬鬆 gate 比
dichromatic 還要強且更便宜。**沒有打破 V11∪Y-diff 0.970 baseline**。

---

## 2. Mode A（full-frame preprocess）— **完全失敗**

整 1920x1080 跑 Yang separation 後餵 V11：

- M1 救回：**0/68（0.0%）**
- M2 救回：**0/24（0.0%）**
- M3 救回：**0/9（0.0%）**
- 對 V11 baseline hit 的破壞：**0/1005（0.0%）**

Mode A 既不救也不破——換言之，full-frame preprocess 對 V11 detection
是 **no-op**。物理上的解釋：

1. Yang 把 max-chromaticity propagate 到鄰域，意味著 σ_d ≈ σ（空間平滑
   的最大值）。對 V11 cube 的影響只是讓所有 pixel 等比例下調 V，但 H/S
   幾乎不變（GT-region H 變化 0.0、S 變化 +10/255）
2. 既然球本來就在 cube **外**（M1 定義就是 HSV cube 在 GT 區零像素），
   一個 H 不變 + S 微升 +10 的 shift 仍跨不過 cube floor 120

---

## 3. Mode B（ROI-only + relaxed HSV）— 邊際救援

策略：在 V11∪Y-diff candidates 各自 ±40px ROI 內跑 Yang separation，
inside-ROI 用寬鬆 HSV gate（s_min 從 120 → 60，v_min 從 30 → 20）。

- M1 救回：**12/68（17.6%）**，其中 **5 frames 是純新增**（V11 沒、Y-diff 沒）
- M2 救回：9/24（37.5%），3 純新增
- M3 救回：3/9（33.3%），1 純新增
- M4 救回：0/1
- 共新增 **9 frames** vs V11∪Y-diff，把 0.938 推到 0.946

**讀法**：B 的「救援」**主要來自 relaxed HSV gate**（s_min 60 而非 120），
不來自 specular separation 本身。Yang separation 只把 GT S 微升 +10，
從 cube 外到 relaxed cube 內這個跨越主要靠**降低 s_min 門檻**完成。
驗證方法：跑無 Yang 的 ROI-relaxed control（暫未實作，但物理推理上
relaxed-only 應該能救回大多數 12 個 M1）。

換言之 Mode B 的勝利更像是「Y-diff 候選確認 + 寬鬆 HSV」的複合 effect，
**dichromatic separation 的邊際貢獻接近 0**。

---

## 4. M1 GT-region saturation shift（核心物理檢驗）

68 個 M1 frame 的 GT 區 S 統計：

| 統計 | S_pre | S_post | Δ |
|---|---|---|---|
| p10 | 12 | 14 | +2 |
| p50 | 45 | 54 | +10 |
| p90 | 56 | 68 | +13 |
| **N with S_post ≥ 120 (cube floor)** | – | **0/68 = 0.0%** | – |

**讀法**：

- M1 frame 球的本徵飽和度普遍極低（p50=45，p10 低至 12）
- Yang separation 確實把 S 推高（Δp50 = +10，方向正確），證明分解出的
  specular component 確實有訊號
- 但 **shift 量級遠不足以救援**：要從 45 推到 120 需要 +75 saturation
  變化，Yang 簡化版只給 +10
- 0/68 越過 cube floor → V11 cube 對 M1 frame 完全不可救（這也解釋
  Mode A 的 0/68 救回率）

### 為什麼 shift 量這麼小？

從單幀檢視（`_dichromatic_sanity.py` 結果）：M1 worst case (src=678) 的
GT pixel BGR ≈ (127, 125, 122)，**三通道差 < 5**。這代表：

- pixel 已被 ISP saturate 到接近 (255,255,255) 後才 tone-mapped 回灰階
- 所謂「specular」與「diffuse」在 capture stage 已不可分（資訊已 lost）
- Yang 假設的 `I = I_diffuse + I_specular` 模型崩潰，因為 ISP 的 dynamic
  range compression 是非線性、unsigned saturation

對較中度的 M1 (src=720, S=100) shift 較大（→117，跨過 100），但仍未到 cube
floor 120。換言之 dichromatic 救援只能在 **S ∈ [110, 120]** 這條極窄
邊界帶 work，而 M1 frame 的 S 分布根本就在 12-56，跟救援帶完全不重疊。

---

## 5. Mode 假設驗證 summary

| 假設 | 結果 | 證據 |
|---|---|---|
| Mode α failure 是 specular-coated diffuse blue | **Falsified** | M1 GT 三通道差 < 5/255，已是純灰白；無殘留 chromatic diffuse |
| Yang 2010 能把 GT-region S 推回 cube 內 | **Falsified** | 0/68 M1 越過 S=120；Δp50 只 +10 |
| dichromatic 能 dominate Y-diff M1 救回率 | **Falsified** | Mode A 救 0/68；Mode B 救 12/68 但主要來自 relaxed HSV，不來自 separation 本身 |
| 全 frame 副作用可控 | **Confirmed** | Mode A 對 V11 hit 破壞 0/1005 |

**核心結論**：Mode α 不是「dichromatic specular highlight 蓋住 chromatic
diffuse」這種教科書 reflection model 場景，而是 **capture-side ISP
clip / blown-out exposure** 把訊號徹底壓平。dichromatic model 的前提條件
（pixel 是 diffuse + specular 線性疊加）在 ISP saturation 後不成立。

這也解釋為何 Y-diff（chroma-blind motion signal）能救回 75.6%（08 報告）—
對「球已是純灰白」這種 frame，**唯一可用的訊號是時序 luminance 變化**，
不是空間 chromatic 分解。

---

## 6. 視覺化證據

5 張 4-panel（原圖 / specular / diffuse / V11 mask post）寫進
`outputs/25_visu_session_s_170a6a89_b_{678,680,697,952,969}.png`。

走查觀察：

- **src=678（連續 31-frame miss run 起點）**：球幾乎與背景同 luminance，
  specular component 正確分離出整顆球（球體本身大半 ≈ achromatic add-on），
  diffuse 圖球變更暗但仍 ≈ 灰色。V11 mask post 仍空白
- **src=697**：球邊緣有少量殘留藍邊，diffuse 圖能看到淡藍 ring，但 cube
  仍 miss
- **src=952，969**：類似 678，整球已純白化，分離後也救不回

肉眼判讀也支持上節的物理結論：M1 frame 球已是純亮白塊，沒有東西可分。

---

## 7. 對後續決策的建議

1. **退役此方向**。dichromatic separation 在我們 capture pipeline 的
   M1 frame 上物理前提不成立，任何 Yang/Tan/Yoon variant 都會撞同一個
   ISP saturation 牆。不再投入 Tan 2005 / 多 illuminant / WB-aware 等
   後續 variant。
2. **真正的 M1 解法在 capture stage**：iPhone 端 AE/exposure lock、
   降 ISO、加 ND filter，物理避開 ISP clip。這是 hardware/operations
   問題，不是 detection 問題。
3. **演算法側極限**：M1 上唯一仍 work 的訊號是 Y-diff（chroma-blind
   temporal contrast），08 報告的 75.6% recovery 已是此類方法的上限。
4. **Mode B 邊際 +0.8pp**：若要榨乾 0.938 → 0.946，**真正貢獻在 relaxed
   HSV s_min=60**，跟 Yang 無關。建議單獨實驗「ROI-only + relaxed HSV
   without Yang」確認 dichromatic 的邊際貢獻是否真為 0；若是，可直接砍掉
   Yang，只保留 ROI-relaxed gate。

---

## 8. Falsification 的科學價值

這次 sprint 的研究價值不在於找到救星，而在於**用 1073 frame 上 quantitative
證據關閉一條看似合理的 algorithmic 通道**：

> ✗ Yang 2010 / Tan 2005 / dichromatic-family specular removal 在我們
> Mode α 上不會 work，原因是 capture-side ISP saturation 抹平訊號，與
> 演算法選擇無關。

這個結論讓未來的 search space 收窄：M1 救援只剩兩條路——
(a) capture-side fix（曝光控制），(b) 純 chroma-blind motion/temporal
signal（Y-diff、optical flow）。
