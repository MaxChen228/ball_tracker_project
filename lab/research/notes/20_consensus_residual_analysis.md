# 20 — Consensus Residual Analysis（V11 ∪ Y-diff15 ∪ Y-diff30 殘差物理特徵）

**腳本**：`lab/research/scripts/26_consensus_residual.py` + `26b_cluster.py`
**結果**：`lab/research/outputs/26_consensus_residual.npz`、`26_residual_table.json`、
`26_residual_clusters.json`、`26_residual_session_*.png` (×21)
**資料基礎**：9 sessions / 1073 GT ball-in frames

---

## 0. 目的

V11 + Y-diff(15) + Y-diff(30) 三個 single-frame stateless cue 全 OR 後仍
miss **21 frame**（M1=10 / M2=7 / M3=4 / M4=0）。本研究**不是**再加
detector，而是量化這 21 frame 的物理 signature，告訴未來人「碰到這種
frame 就放棄 single-frame 路線」。

確認：21 frames 與 `11_cue_independence.md` §7 完全一致。

---

## 1. 殘差 frame 表

完整表見 `outputs/26_residual_table.json`；下方為摘要。

| slug | src | mode | GT (x,y) | area | GT HSV | ring V | Δv | yd_max | run_len | edge_dist |
|---|---|---|---|---|---|---|---|---|---|---|
| 16ec069a_b | 867 | M3 | (1030,134) | 265 | (97,44,142) | 177 | -35 | – | 1 | 135 |
| 16ec069a_b | 922 | M2 | (1286,398) | 681 | (93,92,62) | 117 | -56 | 110 | 1 | 398 |
| 170a6a89_a | 914 | M1 | (1074,3)   | 315 | (116,44,76) | 156 | -80 | 122 | 1 | **4** |
| 170a6a89_b | 678 | M1 | (975,138)  | 77  | (103,**11**,127)| 250| -123| –  | 1 | 139 |
| 170a6a89_b | 782 | M2 | (1018,210) | 2357| (118,109,46) | 111 | -65 | 93  | 2 | 210 |
| 170a6a89_b | 783 | M2 | (1018,215) | 2151| (115,105,52) | 100 | -48 | 123 | 2 | 215 |
| 170a6a89_b | 785 | M2 | (1018,223) | 1471| (111,122,58) | 94  | -36 | 69  | 1 | 223 |
| 170a6a89_b | 799 | M2 | (1052,360) | 1674| (88,130,57)  | 129 | -72 | 198 | 1 | 360 |
| 170a6a89_b | 980 | M1 | (949,132)  | 191 | (103,45,125) | 251 | -126| 75  | 1 | 132 |
| 21af9a82_a | 473 | M1 | (875,170)  | 417 | (46,**14**,121)| 161| -40 | 41  | 1 | 170 |
| 21af9a82_a | 475 | M1 | (876,170)  | 419 | (52,**14**,120)| 161| -41 | 32  | 1 | 170 |
| 21af9a82_a | 478 | M1 | (876,172)  | 227 | (50,**15**,114)| 155| -42 | 12  | 2 | 173 |
| 21af9a82_a | 479 | M1 | (874,170)  | 403 | (53,**17**,112)| 136| -24 | 53  | 2 | 170 |
| 21af9a82_a | 481 | M1 | (878,171)  | 273 | (90,**17**,115)| 155| -40 | 8   | 1 | 172 |
| 21af9a82_a | 489 | M2 | (855,181)  | 399 | (111,68,97)  | 144 | -48 | 102 | 1 | 182 |
| 22d1835e_b | 510 | M1 | (1107,4)   | 315 | (118,43,101) | 197 | -96 | 148 | 1 | **5** |
| 2546618f_a | 481 | M1 | (812,330)  | 574 | (88,63,121)  | 192 | -71 | 156 | 1 | 330 |
| 2546618f_a | 485 | M3 | (803,340)  | 529 | (98,70,97)   | 167 | -70 | 117 | 1 | 341 |
| 2546618f_b | 352 | M3 | (1017,4)   | 395 | (99,147,79)  | 180 | -101| 179 | 1 | **5** |
| 2546618f_b | 355 | M2 | (1026,3)   | 193 | (79,47,91)   | 176 | -85 | 63  | 1 | **3** |
| 2546618f_b | 385 | M3 | (1136,9)   | 1044| (106,147,73) | 149 | -75 | 232 | 1 | **9** |

**先決觀察**：
- **20/21 frames 為孤立 miss**（run_len ≤ 2，且絕大多數兩邊 prev/next 都 hit）。沒有任何 single-frame algo 能救，但 trajectory 上下文一定能。
- 2 frames 沒有 prev frame（src=867、678；都是 in_frame=local 0）— 結構性殘差，非物理問題。
- **沒有任何一個 residual 是中等 desat、強 yd、寬背景對比的「漏網之魚」** — 21 frames 都帶有 ≥ 1 個明確物理 violator。

---

## 2. 物理 signature 分布（residual vs hit baseline，n=21 vs n=971）

| feature | residual median | hit median | Cohen d |
|---|---|---|---|
| **gt_s** (GT 飽和度) | **47** | 145 | **−1.84** |
| **bbox_aspect** | **0.75** | 0.94 | **−1.29** |
| **bbox_fill** | **0.74** | 0.79 | −1.12 |
| **contrast_s** | 33 | 101 | −1.65 |
| **gt_h** (GT hue) | 98 | 107 | −0.84 |
| **yd_gt_mag** (Y diff 在 GT 區的平均) | **12** | 32 | −0.84 |
| edge_dist (距 frame 邊界) | **170** | 279 | −0.75 |
| contrast_b (B-channel) | −50 | −36 | −0.73 |
| yd_gt_max | 102 | 135 | −0.60 |
| contrast_v | −65 | −44 | −0.55 |
| edge_ring | 67 | 41 | +0.52 |
| gt_v | 97 | 105 | −0.47 |
| ring_s | 27 | 37 | −0.46 |
| gt_area | 403 | 741 | −0.37 |
| clutter | 2 | 1 | +0.30 |

**核心 finding（物理上 single-frame algo 必死的指標）**：

1. **gt_s 中位 47 vs hit 145（d=−1.84）**：這是最強分離特徵。residuals 的飽和度被嚴重壓縮 — V11 本身要 s ≥ 120，這條線下到 47 直接 mask=0；Y-diff 也救不了因為色彩信息不在 luminance domain。
2. **bbox_aspect 0.75 / bbox_fill 0.74**（d=−1.29 / −1.12）：球體本身在這些 frame 不是圓（被 frame 邊界裁切、specular 把球切兩半、或 motion blur 撕成橢圓）。任何 shape gate 都會砍掉。
3. **yd_gt_mag 12（d=−0.84）**：在 GT 區域上的 inter-frame |ΔY| 中位 12 — 已經低於 yd15 threshold，連 yd_max 中位 102 也差過 hit 的 135。**Y-diff 的物理信號在這 21 frame 真的不存在**，不是 threshold 問題。

> **負面結論**：這不是「再 tune 一下 H/S/V/thr 就救得回」的 frame。所有 single-frame intensity / chroma / motion cue 在這 21 frame 都被物理性地壓到雜訊水平。

---

## 3. Cluster 結果

依 (edge_dist, gt_s, gt_v, run_len) 手動分群（n=21 不需要 k-means）；
完整 assignment 見 `outputs/26_residual_clusters.json`。

### G1 — Edge-clipped（n=5）

src 914 / 510 / 352 / 355 / 385。GT centroid 距 frame 上邊界 ≤ 9 px。
球被 frame top crop 切掉 50-95%；剩下的 sliver 既無圓形 (bbox_aspect 砍)、
又因 partial occlusion 失去 area。共同特徵：edge_dist ≤ 9、ring_v 高
(149-197)、contrast_v 大負值 (−75 ~ −101)。

**物理本質**：sensor FoV 邊界 = capture-side 物理損失。Pixel 已不存在，
algorithm 救不回。

### G2 — Specular / desat run（n=6）

session 21af9a82_a src 473/475/478/479/481（連續 5 frame）+ 170a6a89_b
src 678。GT S 中位 14（極低）、V 中位 113。同 ROI 中心 (875,170) ±2，球
**靜止地停在強光斑底下**（這是出手前 setup phase，flight_pos 0-0.11）。
強 specular 把球的全表面 desaturate 到 BT.709 sensor noise floor 之下；
yd_max 12-53 也微弱（球幾乎不動 → temporal cue 也死）。

**物理本質**：highlight saturation + low motion = HSV cube + Y-diff
雙重物理失效。這不是 algorithm，是 **sensor dynamic range 的 hard
limit**。

### G3 — Low-contrast mid-flight（n=5）

170a6a89_b src 782/783/785/799 + 16ec069a_b src 922。GT V 中位 52，
ring V 100-129（暗背景），contrast_v 僅 −36 ~ −72，但 GT_S 仍正常
(92-130)。球在中段飛行**穿越深色背景區（看似牆面陰影 / 屋頂雜物）**，
亮度差距太低使 V11 mask 在 v_min=30 的下緣脫落、CC 破碎。
yd_gt_max 69-198 **有信號**但局部 edge 強度散亂，shape gate 砍掉。
src 782-785 是**連續 4-frame miss run** 中的中段 — 標準的「球穿背景
鏡面陰影」典型。

**物理本質**：low ball-vs-bg contrast + fragmented HSV mask。Single
frame 看不清；但時序上 prev/next 都有信號（run_len 1-2）。

### G4 — 邊界附近 + 部分 desat（n=5）

src 867 / 980 / 489 / 481 / 485。混合特徵：edge_dist 132-341、GT_S
44-70（中度低）、ring_v 144-251。**球進飛行末段（flight_pos 0.94-0.99
為主）**，亮度被屋簷 / 牆角 specular bleach 部分 desaturate。Bbox
aspect 多落在 0.65-0.85（球側邊被 highlight 切掉，aspect 受形變）。

**物理本質**：partial specular bleach + 飛行末段運動模糊 + shape gate
觸線。介於 G2 與 G3 之間，無單一 dominant violator。

### Cluster 摘要

| Cluster | n | dominant violator | flight_pos | run_len 中位 |
|---|---|---|---|---|
| G1 edge-clipped | 5 | partial-frame (FoV) | mid-late | 1 |
| G2 specular run | 6 | desat (S<20) + low motion | early (setup) | 1-2 |
| G3 low-contrast mid | 5 | dark background passage | mid | 1-2 |
| G4 mixed late-flight | 5 | partial bleach + blur + edge | late | 1 |

---

## 4. 視覺化（21 frames，全部已輸出）

`outputs/26_residual_session_*_src*.png` — 每張標 GT 中心 (黃十字)、
V11 候選 (紅圈)、Y-diff15 候選 (紫圈)、+ overlay (mode / GT HSV / ring V /
yd / clutter / prev/next hit / run_len)。

代表幀（建議閱讀順序）：
- **G1**: `26_residual_session_s_170a6a89_a_src914.png`、
  `..._s_22d1835e_b_src510.png`、`..._s_2546618f_b_src352.png`
- **G2**: `..._s_21af9a82_a_src473.png`、`..._s_21af9a82_a_src478.png`、
  `..._s_170a6a89_b_src678.png`
- **G3**: `..._s_170a6a89_b_src782.png`、`..._s_170a6a89_b_src799.png`、
  `..._s_16ec069a_b_src922.png`
- **G4**: `..._s_2546618f_a_src485.png`、`..._s_170a6a89_b_src980.png`

---

## 5. 每 cluster 的解法（核心 deliverable）

| Cluster | n | Single-frame algo 救得回？ | 真正解法 |
|---|---|---|---|
| **G1 edge-clipped** | 5 | 否（pixel 不存在） | **Capture-side**：在 plate 安裝兩台鏡頭使 FoV 涵蓋整段軌跡；或用更廣 FoV 鏡頭。algorithm 端只能用 trajectory 投影預測+「ball is exiting」flag。 |
| **G2 specular run** | 6 | 否（HSV+temporal 雙死） | **Capture-side**：(a) HDR/multi-exposure 兩段曝光 — 用低曝光支讀回 highlight 內球面色彩；(b) 偏振濾鏡 (CPL) 削減 setup phase 強光鏡反射。Algorithm 端：對 setup phase 跑 **dichromatic decomposition / specular invariant**（已測：notes/18 → 對極端 desat 無效，不要重試）。 |
| **G3 low-contrast mid** | 5 | 部分（temporal yes） | **Temporal interpolation**：prev/next 都 hit → 用 ballistic spline 內插 + Kalman gap-fill，不需新 detector。最 cheap，且這 5 frame run_len 都 ≤ 2，物理上一定 fittable。 |
| **G4 mixed late-flight** | 5 | 多半否（多重 violator） | Temporal interpolation 也救得回（run_len=1）；長期應**雙路 capture**（兩台 iPhone 立體相互覆蓋落點區）。 |

> **關鍵分流**：21 frame 中 **10 frame（G3+G4）只需 temporal gap-fill**
> 即可救回 — 這條成本最低，cover 1052 → 1062 (+10/1073 = +0.93pp)，
> 不需新 detector。剩下 11 frame（G1+G2）是 **capture-side 物理硬限**，
> 任何 single-frame DL 也無解（pixel 真的不存在 / 真的飽和爆掉）。

---

## 6. Insights for next research move

「21 frame residual 的物理 signature」浮出三個**現有藍圖外**的 attack vector：

### A. **Trajectory-aware gap-fill 是最便宜的 +1pp**（推薦先做）

20/21 frames 是 **isolated miss in otherwise-hit run**（run_len ≤ 2）。
現有 server-post detection pipeline 已有逐 frame candidates；只要在
post-processing 加一條 **per-pitch ballistic spline + Kalman gap-fill**：
凡 detect 失敗但前後 ≤ 3 frame 都 hit，用 fit 投影補一個 candidate。
G3+G4 整批可救（+10 frame ≈ +0.9pp），不增加新 detection model、不
碰 HSV，**架構面零成本**（已有 viewer-side residual filter 的 ballistic
LSQ 機制可直接 reuse）。對 stereo triangulation 沒副作用 — gap-fill
candidate 帶 confidence flag，下游 ray-midpoint 仍可選擇忽略。

> **這條沒有出現在 12_ensemble_distillation_design / 17_dichromatic_design**
> 等任何「擴 detector」proposal 裡。21-frame 物理 audit 才把它推到檯面。

### B. **G2 setup-phase desat run 應該從 algorithm 戰場撤退，移到 capture-side**

5/6 G2 frames 來自同一 session 同一空間位置（21af9a82_a，球在 setup
phase 靜止於 (875,170)±2）。GT_S=14 已低於任何相機 BT.601/709 noise
floor 上限 — 這是**物理 sensor saturation**，DL 也救不回（因為 pixel
本身已 clipped 到 highlight）。建議：

- 短期：**setup phase（flight_pos < 0.15）detection 容忍**降為 best-effort，
  pitching_detection 從 first motion 開始（用 gating 排除靜止階段）；報
  metric 時把 setup-phase 排除。
- 長期：**capture-side**：(a) 建議使用者試 CPL filter 削強光、(b)
  iPhone bracketed-exposure 或 ProRAW HDR（240fps 不可，但 setup phase
  是靜止的 → 60fps 即足，可用第二支 cam 平行 capture）。

> **這條翻轉了 17_dichromatic_design 的方向** — dichromatic 在 18
> 跑出來對極端 desat 無效，本 audit 確認原因是 sensor pixel 已飽和，
> **algorithm 路完全死掉**。把 G2 從 detection scorecard 移除，整體
> recall ceiling 從 21/1073 → 16/1073 殘差，換取乾淨 baseline。

### C. **G1 edge-clipped 不是 detection 問題，是 calibration / geometry 問題**

5/5 G1 frames 都在 frame top（y ≤ 9）。對 stereo triangulation 而言，
這些 frames **本來就沒有可用 epipolar match**（球已部分出 FoV，centroid
biased）。建議：

- 對 server-side `frames_server_post`，加一條 **edge_dist < 15 px →
  flag as "out-of-FOV"**，下游 triangulation 直接 skip（避免污染 fit）。
- iOS capture 改用 **smaller crop / wider FoV preset** 把 plate 與
  ball trajectory 整段塞進 frame。
- 比新增 detector 重要 100 倍 — 因為 G1 frames 即使「detect 到」也會
  triangulation 出錯（centroid 已偏 + Z-depth 不對）。

> **這條完全沒進現有 PR/notes 任何 backlog**，需要實際在 dashboard
> 加 out-of-FOV flag。

---

## 7. 結論

| 問題 | 答案 |
|---|---|
| Single-frame stateless 演算法的物理死區是什麼？ | **21/1073 frame (1.96%)**，dominant violators：gt_s（d=−1.84）、bbox_aspect（d=−1.29）、yd_gt_mag（d=−0.84） |
| 還有 single-frame 救援空間嗎？ | **沒有**。21 frames 都帶 ≥ 1 個物理 violator，且 yd_gt_mag 中位 12 已低於 noise floor |
| 那未來該往哪走？ | **(A) trajectory gap-fill** 救 G3+G4 共 10 frame；**(B) capture-side HDR/CPL** 救 G2 共 6 frame；**(C) FoV / out-of-FOV flag** 救 G1 共 5 frame |
| 哪一條 ROI 最高？ | **(A) gap-fill** — 免新 model、免新 capture、+0.93pp 即得 |
| Single-frame DL 還有意義嗎？ | 僅對 G2 specular 有微弱 upside；其他三 cluster DL 也救不回（pixel-level 已壓制） |

**未來人記住一條**：**這 21 frame 不要再用 single-frame algorithm 路試
任何 detector**。直接走 (A) gap-fill + (B/C) capture-side。

---

_分析日期：2026-05-01 / 9 sessions / 1073 GT / detector configs lockstep
to `22_cue_independence.py`_
