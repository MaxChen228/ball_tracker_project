# V11 follow-up — 9-session refresh + V11 reference

接續 [`01_final_report.md`](01_final_report.md)。新增 2 個 done sessions
（21af9a82_b 54 frames、2546618f_b 116 frames），總計 9 sessions /
**1073 GT frames**（vs 原 7 sessions / 931 frames）。

## 1. 9-session baseline refresh — V10 仍 robust

| | n | PROD | V10 | Δ |
|---|---|---|---|---|
| 7-session（原報告） | 931 | 0.687 | 0.874 | +18.8pp |
| **9-session（新）** | **1073** | **0.719** | **0.884** | **+16.4pp** |

新 sessions 偏 saturated（21af9a82_b R=1.0, 2546618f_b R=0.93）拉抬 PROD
baseline，使絕對 Δ 降但**個別 session 全 0 退步**。V10 結論成立。

新 V10 miss 分布（**125 miss**）：

| Mode | n | % | 說明 |
|---|---|---|---|
| M1 HSV cube 在 GT 區零像素 | 68 | 54.4% | desat ball 跑出 cube |
| M2 HSV 抓到但無 ≥5px CC | 36 | 28.8% | 像素過於碎裂 |
| M3 CC aspect < 0.50 | 20 | 16.0% | motion blur |
| M4 fill < 0.35 | 1 | 0.8% | 邊緣 |
| M5 centroid drift > tol | 0 | 0.0% | 已不出現於 mid HSV |

M1 仍主導，**比例和 7-session 報告 (57.7%) 持平**。

## 2. V11 設計 — Pareto-dominant V10

V10 → V11 改了 3 個 param，全部來自 M2/M3 attack：

| 參數 | V10 | V11 | 動機 |
|---|---|---|---|
| morph CLOSE kernel | 無 | 3×3 ellipse | 連通 fragmented mask（M2）|
| aspect_min | 0.50 | 0.40 | motion blur 240fps 物理上限 ~0.62（12.5cm/7.8cm）|
| min_area_px | 5 | 3 | 接受邊緣碎片（M2）|

### 7 變體 ablation

| V | recall | Δpp | cands/f | 改動 |
|---|---|---|---|---|
| E0=V10 | 0.884 | 0 | 17.3 | baseline |
| E1 | 0.885 | +0.19 | 16.4 | close=3 only |
| E2 | 0.884 | 0 | 14.4 | close=5 only（過度 merge）|
| E3 | 0.896 | **+1.21** | 20.8 | aspect=0.40 only |
| E4 | 0.900 | +1.68 | 24.5 | aspect=0.30（過寬，吞 elongated clutter 風險）|
| E5 | 0.898 | +1.49 | 19.6 | close=3 + aspect=0.40 |
| **E6=V11** | **0.905** | **+2.14** | **24.8** | E5 + min_area=3 |

**關鍵發現**：

1. morph CLOSE 單獨 +0.19pp，**M2 fragmentation 不是大問題** — 我事前預期它會是主力，實測是邊際
2. **aspect 0.50 → 0.40 是真主力（+1.21pp 單獨貢獻）** — V10 報告當時 aspect 已從 PROD 0.75 降到 0.50 是最大單一 +7.1pp，再降 0.10 還能多救 1.21pp
3. close + aspect 組合略有正交性（E3 +1.21 + E1 +0.19 ≈ E5 +1.49，加總性 OK）
4. min_area 5→3 多救 7 frames（E5 → E6 +0.65pp），**不是 noise**，是真實的單像素 CC 救回

### V11 per-session（0 退步驗證）

| session | n | V10 | V11 | Δ |
|---|---|---|---|---|
| 16ec069a_b | 228 | 0.974 | 0.982 | +0.88 |
| 170a6a89_a | 112 | 0.973 | 0.991 | +1.79 |
| 170a6a89_b | 274 | 0.697 | 0.734 | +3.65 |
| 21af9a82_a | 76 | 0.737 | **0.803** | **+6.58** |
| 21af9a82_b | 54 | 1.000 | 1.000 | 0 |
| 22d1835e_a | 57 | 0.982 | 0.982 | 0 |
| 22d1835e_b | 77 | 0.987 | 0.987 | 0 |
| 2546618f_a | 107 | 0.953 | 0.972 | +1.87 |
| 2546618f_b | 88 | 0.932 | 0.955 | +2.27 |

**6/9 session 提升、3/9 持平、0 退步**。最大 winner 21af9a82_a +6.58pp
（從原本 V10 0.737 → V11 0.803，這 session 的低 R 主因是
desat-in-flight，V11 的 close+aspect 救回部分 motion-blur frames）。

### Latency

`ball_detector.py --bench`：240 frames @ 1080p Mac M-class →
**1.96 ms/frame**（V10 是 2.57 ms）。比 V10 還快 — morph CLOSE 連通 mask
讓 CC 數變少，抵消 morph 本身的 overhead。30 cands/frame avg。
iPhone 14 估 ~2.3 ms，4.16 ms budget 留 45% margin。

## 3. V11 後 miss 分布 — 收斂到 desat 死局

V11 R=0.905 後剩 **102 miss**：

| Mode | n | % | S_mean p50 |
|---|---|---|---|
| M1 | 68 | **66.7%** | 45 |
| M2 | 24 | 23.5% | **58** |
| M3 | 9 | 8.8% | 91 |
| M4 | 1 | 1.0% | – |

V10 救的 23 frames 全來自 M2/M3。**M1 完全沒動（68→68）**——HSV cube
在 GT 區零像素的 frame 用 V10/V11 任何 shape gate 改動都救不了。

**M1 + M2 = 90.2% 的 V11 miss**，且 saturation S_mean 都在 [45, 58]
區間，遠低於 HIT p50=145。這是純 desat 問題。

### Per-session 集中度

V11 全 102 miss 中：

| session | miss | % of all miss |
|---|---|---|
| **170a6a89_b** | 73 | 71.6% |
| **21af9a82_a** | 15 | 14.7% |
| 其他 7 sessions | 14 | 13.7% |

**86% 的剩餘 miss 集中在 2 個 session**。其他 7 sessions 平均 R ≥ 0.97，
等於 V11 在「正常條件」session 已逼近完美 recall。

### M1 GT hue median 分析

68 個 M1 frame 的 GT 區 hue median：p10=74、p50=103、p90=113。
p10=74 表示部分 desat 球連 hue 都跑掉（飽和度太低 hue 變雜訊）——
這類 frame **任何 HSV cube 都救不了**，hue 已經不在藍範圍。

## 4. 否決掉的方向 — 為什麼 9.5% 是 HSV 上限

### A: 二層 cube fallback（dead-end）

設計：主 cube V11 + 副 cube `H[103,118] S[0,80] V[150,255]`，候選合併
送下游。

| 模式 | 額外 cands/frame | recovery |
|---|---|---|
| 副 cube 不 cap | 30 | +1.49pp（救 16 frames） |
| 副 cube top-K=1 | 1 | +0.28pp（救 3 frames） |
| 副 cube top-K=3 | 3 | +0.28pp（**仍救 3**）|
| 副 cube top-K=5 | 5 | +0.28pp（**仍救 3**）|

**Top-K cap 後 recovery 從 16 暴跌到 3**。原因：副 cube area-sort 第一名
永遠是大塊背景（牆 / 地板）的 low-S high-V 區域，球 area 排在第 30 名以後。

這直接驗證 V10 報告「**S 是 spatial isolation gate**」結論的 corollary：
拿掉 S，球在 area-rank 上輸給背景 — 跟 hue-only -91pp 災難同源。

無 cap 救 16 但每 frame +30 cands 對 server O(N×M) 三角化是災難
（25×25 vs V11 25×25，等於 server load × 4），實務不可行。

### B: CLAHE/contrast 預處理（dead-end）

5 變體在 V11 上加 CLAHE/S-stretch：

| 變體 | recall | Δpp | ms/frame |
|---|---|---|---|
| C0 V11 | 0.905 | 0 | 1.86 |
| C1 CLAHE on V | 0.901 | -0.37 | 4.40 |
| C2 CLAHE on L (Lab) | 0.878 | -2.70 | 5.32 |
| C3 S × 1.5 | 0.852 | -5.31 | 4.63 |
| C4 CLAHE V + S × 1.5 | 0.847 | **-5.78** | 6.50 |

C4 per-session 揭示真相：
- 170a6a89_b（desat 重災） **+13.5pp**（救 37 frames！）
- 22d1835e_b **-72.73pp**（崩盤）
- 22d1835e_a -17.54pp、2546618f_a -22.43pp

**完全是 hue-only 災難的同構複製**。S stretch 把背景 desat 區域 S 推進
[120, 255] cube → 球與背景 merge → spatial isolation gate 失效。

170a6a89_b 在 C4 大幅救回證明：**desat session 的球可被 frame-wide S
boost 救出，但代價是 saturated sessions 崩**。中間方案需要
**per-ball-region adaptive boost**，那需要 ball localization →
temporal anchor → 落入 D 方向的限制。

### D: Temporal anchor（理論 +1-2pp，違反 production stateless）

V11 102 miss 集中在 170a6a89_b (73) + 21af9a82_a (15)。檢驗時間結構：

| | 170a6a89_b | 21af9a82_a |
|---|---|---|
| miss runs（連續長度）| **[21, 8, 1, 4, 1, 1, 4, 2, 31]** | [11, 2, 1, 1] |
| max run | **31 frames** | 11 frames |
| miss within 1 frame of hit | 19.2% | 33.3% |
| miss within 5 frames of hit | 49.3% | 60.0% |

170a6a89_b 開頭 21 frame 連續 miss、結尾 31 frame 連續 miss。
**51% 的 miss 在 ≥5 frame distance 到 hit**——temporal anchor
過了那麼多 frame 已過期。

理論上限估算：

| Gap 距離 | 對應 miss frames | 假設 anchor 救率 | 救回 |
|---|---|---|---|
| 1-2 frame | 23 | 70% | 16 |
| 3-5 frame | 13 | 30% | 4 |
| ≥5 frame（含 21+31 long runs）| 37 | 5% | 2 |
| **總救回（170a6a89_b）** | **73** | – | **22** |

170a6a89_b +22 + 21af9a82_a +5 ≈ +27/1073 ≈ **+2.5pp 上限**。
要達 V11 0.905 + 0.025 = **0.93** 需要 stateful detector，違反
[ball_tracker_project/CLAUDE.md] iOS=server stateless 對齊（已 ship）。

研究結論：D 在純 stateless 不可行。stateful 路徑在這 codebase
明確 out-of-scope。

## 5. V11 真實上限 = 0.905；剩 9.5% 是「**球的可見 color signature 物理消失**」

### 物理特徵歸因（17_miss_run_physics.py）

逐 frame dump 170a6a89_b + 21af9a82_a 的 GT-region BGR/HSV 統計 +
frame-global brightness/saturation：

| 170a6a89_b | HIT p50 | MISS p50 | Δ |
|---|---|---|---|
| gt_s（球本身飽和度）| 118 | 51 | **-67** |
| gt_v（球本身亮度）| 96 | 122 | **+26** |
| **global_v（全 frame）** | **132** | **133** | **+1** |
| **global_s（全 frame）** | **57** | **57** | **0** |

| 21af9a82_a | HIT p50 | MISS p50 | Δ |
|---|---|---|---|
| gt_s | 92 | 17 | -75 |
| gt_h（hue 偏移）| 107 | 90 | **-17** |
| gt_v | 116 | 114 | -2 |
| global_v | 156 | 153 | -3 |
| gt_area | 813 | 416 | -397 |

### 證偽：不是 capture-side AE 問題

- **global_v / global_s 在 HIT 和 MISS frames 幾乎完全相同**
  （Δ ≤ 1 unit）
- 整 frame 亮度沒變，**只有球局部 desat**
- → iOS exposure/AE 不需要修正；曝光是對的

### 真實物理機制

兩種 mode 共存：

**Mode α — specular reflection（170a6a89_b 開頭 21 frame）**
- 球 gt_s=19 / gt_v=131（vs HIT 平均 118/96）
- 球被強光直射，表面金屬 specular reflection 把藍色 chroma 洗掉
- 球 apparent color 變灰白色高光點

**Mode β — ambient color reflection（21af9a82_a, 170a6a89_b 結尾 31
frame）**
- 球 gt_h=89（vs HIT 107）+ gt_area=416（vs HIT 813，球 deeper into scene）
- 球反射環境色（草地 / 牆 / 觀眾席），原色被環境反射 dominate
- 球 apparent color 變偏 cyan/green-blue

### Pre-DL fundamental limit

**球的可見 color signature 物理上消失**——任何 color-based detector
都失效：

| 方法 | 為何失效 |
|---|---|
| HSV cube wide H | hue 漂到 89-103，已在 cube 邊界外 |
| HSV cube low S | 收 background 太多（spatial isolation gate 失效）|
| LAB a*b* | desat ball 的 a*/b* 趨近 0（neutral gray）|
| YCbCr | desat ball 的 Cb-Cr 趨近 (128, 128)（neutral）|
| 色度 chromaticity (r/(r+g+b)) | desat ball 趨近 (1/3, 1/3, 1/3)|
| frame-global feature | global_v Δ ≤ 1，沒有任何 predict 信號 |

### 還剩的可行方向（all 違反 stateless 或超 budget）

1. **Stateful temporal anchor (D)**：用 prev hit ROI 在當前 frame 內
   放寬 cube。上限 +2.5pp，違反 production stateless 對齊
2. **FRST radial symmetry**：不依賴 color 用 gradient 投票找圓。但
   1080p ~10ms 超 4.16ms budget；要 ROI 內跑必須 prev anchor → 又回到
   stateful
3. **Capture-side hardware**：CPL filter 砍 specular reflection；
   ND filter + 較長曝光（但會 break 240fps）；不是 algorithm 問題
4. **Deep learning**：YOLO / 球專門 detector 學 desat ball 的 visual
   prior — 不是本研究 scope

研究結論：**HSV pre-DL 在這個 capture setup 的天花板就是 0.905**。

## 6. 建議的下一步研究方向（已 out-of-budget）

僅列，未做：

1. **FRST (Fast Radial Symmetry Transform, Loy 2003)** as 獨立信號
   - 不依賴顏色，靠 gradient 方向投票找圓心
   - 1080p 可能 ~5-15 ms（超 budget）→ 縮小 ROI 到 prev anchor 才可行
2. **Stateful Kalman + ROI 內極寬 HSV** 救 isolated 1-2 frame miss
   - 上限 +2.5pp，違反 stateless production
3. **Capture-side AE 修正**
   - iOS exposure compensation -1.0 EV in flight 區段
   - 跑 capture 比較實驗（不是 algorithm 比較）
4. **Per-frame on-line S adaptation**
   - frame-wide S histogram 偵測 desat-frame，只在該 frame stretch S
   - 仍會破 spatial isolation，但 burden 縮成 single-frame，可能可接受
5. **Two-stream 並行**：HSV stream（V11）+ gradient stream（FRST）
   merge candidates by physics gate

## 7. Files

```
lab-research/
├── notes/
│   ├── 01_final_report.md            ← 7-session V10 原報告（保留）
│   └── 02_v11_followup.md            ← 本檔（9-session V11）
├── scripts/
│   ├── ball_detector.py              **V11 reference**
│   ├── 01-08                          7-session 原研究腳本
│   ├── 09_refresh_9sessions.py        9-session baseline + miss breakdown
│   ├── 10_m1_hsv_profile.py           M1 HSV 分布分析（驅動 A 設計）
│   ├── 11_fallback_cube_recovery.py   A 邏輯 bug（保留作 audit trail）
│   ├── 12_dual_cube.py                A always-on dual cube（無 cap）
│   ├── 13_dual_cube_topk.py           A top-K cap 失效驗證
│   ├── 14_m2_m3_attack.py             B variants E0-E6（V11 設計來源）
│   ├── 15_v11_failure_modes.py        V11 後 miss 重新分類
│   ├── 16_temporal_structure.py       D 可行性（170a6a89_b miss runs）
│   ├── 17_clahe_preproc.py            B CLAHE 變體 dead-end
│   └── 18_miss_run_physics.py         物理歸因（證偽 capture-side AE 假設）
└── outputs/                           各支對應 .npz
```

## 8. 結論濃縮

對深藍硬球，9 sessions / 1073 GT frames：

- **PROD R = 0.719**（aspect 0.75 主要瓶頸）
- **V10 R = 0.884**（+16.5pp）
- **V11 R = 0.905**（+2.14pp on V10，+18.6pp on PROD，0 session 退步）
- **V11 上限 = 0.905**，剩 9.5% 集中在 2 個 desat-dominant session
  (86% of miss)
- **HSV cube 怎麼調都救不了**剩餘 desat miss——已驗證 wide H、low-S、
  CLAHE、S stretch 全部 dead-end，根因是 spatial isolation gate
  與 desat recovery 互斥
- **真上限突破需 capture-side 修正或 multi-cue (FRST, temporal)**，
  algorithm-side 已收斂
- 計算成本 V11 = **1.96 ms/frame** Mac M-class 單核（比 V10 還快），
  iPhone 14 估 ~2.3 ms，240fps 4.16ms budget 留 45% margin
