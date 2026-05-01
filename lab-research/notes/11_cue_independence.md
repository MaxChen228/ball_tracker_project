# 11 — Cue Independence Analysis（V11 / Y-diff / FRST / DL）

腳本：`lab-research/scripts/22_cue_independence.py`  
結果：`lab-research/outputs/22_cue_independence.json`  
資料基礎：9 sessions，1073 GT ball-in frames

---

## 1. 各 cue 邊際 Recall

| Cue | R (alone) | hits / 1073 |
|-----|-----------|-------------|
| V11 (HSV color) | **0.9049** | 971 |
| Y-diff thr=15 | 0.5191 | 557 |
| Y-diff thr=30 | 0.5704 | 612 |
| FRST | N/A — Track A pending | — |
| Tiny FCN DL | N/A — Track L not started | — |

---

## 2. Cue × Cue Mutual Information

| Pair | I(X;Y) (bits) | NMI | ΔP conditional |
|------|---------------|-----|----------------|
| V11 vs Y-diff15 | **0.0087** | 0.019 | -0.185 |
| V11 vs Y-diff30 | **0.0043** | 0.009 | -0.128 |
| Y-diff15 vs Y-diff30 | **0.1759** | 0.178 | +0.479 |

**解讀**：

- **V11 與 Y-diff 幾乎完全不相關**（MI ≈ 0.004–0.009 bits，NMI < 0.02）。這是最互補的一對。
- 條件概率 ΔP 為**負值**（V11 hit 時 Y-diff hit rate 更低）：Y-diff 偏好 V11 miss 的幀（specular/desat），符合理論預期。
- **Y-diff15 vs Y-diff30 高度相關**（MI=0.176 bits，NMI=0.178）：兩個 threshold 捕捉相同信號，邊際增益小。
- **最互補對**：V11 + Y-diff（任意 threshold），I 最低 + joint R 最高。

---

## 3. Union Recall 與 Diminishing Returns

| 組合 | R | 邊際增益 (pp) |
|------|---|--------------|
| V11 alone | 0.9049 | — |
| V11 ∪ Y-diff15 | **0.9702** | **+6.52pp** |
| V11 ∪ Y-diff30 | 0.9702 | +6.52pp |
| V11 ∪ Y-diff15 ∪ Y-diff30 | **0.9804** | +1.03pp（on top of V11∪yd15） |

**Saturation 分析（在已評估 cue 集合內）**：

- 第一個互補 cue（Y-diff，任一 thr）貢獻 **+6.52pp**。
- 第二個同類 cue（另一個 Y-diff threshold）只貢獻 **+1.03pp**（已接近 saturation）。
- **結論：saturation 點在 2 cues（V11 + Y-diff）**。第三個 Y-diff variant 邊際報酬遞減至 1pp。
- 注意：此 saturation 結論僅在 {V11, Y-diff} 集合內成立；FRST/DL 可能打破此天花板。

**V11∪Y-diff30 = 0.9702 vs 21_yplane_diff.py 的 0.9692 差異說明**：  
21_yplane_diff.py 在 union 前對 Y-diff candidates 做了 5px dedup（Y-diff cand 若在 V11 cand 5px 內則丟棄），本腳本做純 binary OR on hit indicators。兩種方法在約 1 frame 上不同。**本腳本的 binary OR 是 MI 分析的標準方法**。

---

## 4. Per-mode Best Cue

分類邏輯：canonical classifier（與 `15_v11_failure_modes.py` 相同）

| Mode | n | V11 | Y-diff15 | Y-diff30 | Best cue | V11∪yd15 | V11∪yd30 | all3 | residual |
|------|---|-----|----------|----------|----------|----------|----------|------|----------|
| **M1** (specular/desat) | 68 | 0.000 | **0.750** | 0.750 | Y-diff15 | 0.750 | 0.750 | 0.853 | **10** |
| **M2** (fragmentation) | 24 | 0.000 | 0.542 | **0.625** | Y-diff30 | 0.542 | 0.625 | 0.708 | **7** |
| **M3** (hue-shift/β) | 9 | 0.000 | **0.556** | 0.333 | Y-diff15 | 0.556 | 0.333 | 0.556 | **4** |
| M4 (fill gate) | 1 | 0.000 | 1.000 | 1.000 | Y-diff* | 1.000 | 1.000 | 1.000 | 0 |
| HIT | 971 | 1.000 | 0.502 | 0.558 | V11 | 1.000 | 1.000 | 1.000 | 0 |

**關鍵觀察**：

- **M1（specular，n=68，最大 miss 群）**：yd15 或 yd30 單用救回 75%；兩者 OR（all3）升至 85.3%，殘餘 10 frames。Y-diff 的核心價值。
- **M2（fragmentation，n=24）**：yd30 優於 yd15（0.625 vs 0.542）；all3 升至 70.8%，殘餘 7 frames。高 threshold 過濾 JPEG noise，CC 更完整。
- **M3（hue-shift，n=9）**：yd15 救回 55.6%（5/9），yd30 降至 33.3%。all3 與 yd15 相同（0.556），殘餘 4 frames。M3 的 motion signal 微弱，低 threshold 更適合；yd15 和 yd30 對 M3 捕捉到相同 5 frames。
- **V11 對所有 miss frames 貢獻為 0**（定義如此），Y-diff 是唯一救援路徑。

**Mode β（M3）補充說明**：n=9 樣本偏小，56% 結論方向可信但不強。需要更多 session 驗證。

---

## 5. Oracle Ceiling vs Simple Union

| 分析模式 | R |
|---------|---|
| Simple OR（V11∪yd15∪yd30） | **0.9804** |
| Mode-routed oracle（per mode 最佳 cue） | 0.9720 |
| Gap（mode_routed − simple_OR） | **−0.84pp** |

**解讀**：

- 對二元 hit/miss detector，**per-frame oracle == simple OR**（所有 cue 有信號就算 hit）。
- Mode-routed oracle（模擬「已知 mode → 選最佳 cue」）反而比 simple OR **低 0.84pp**。原因：單 cue routing 無法同時利用 yd15 和 yd30 在同一 mode 內的 within-threshold 互補性（I(yd15;yd30)=0.176 bits，NMI=0.178，非完全相關）。simple OR 同時利用兩個，routing 只能選一個。
- **結論：simple OR 已是 binary cue ensemble 的 oracle ceiling**。演算法 gap = 0。若要進一步提升，只能靠：(a) 新增互補 cue（FRST/DL），或 (b) 在連續信心分數上做加權 ensemble。

**Oracle ceiling 定義**（二元情境）：  
`R_oracle = R_union_all = 0.9804`。與 V11 alone (0.905) 相差 **7.55pp**，代表「所有已評估 Y-diff variants 的總可提取信息量」。

---

## 6. 結論

| 研究問題 | 答案 |
|---------|------|
| V11 vs Y-diff 有多獨立？ | **極高互補**：I(V11; Y-diff) ≈ 0.004–0.009 bits，NMI < 0.02 |
| 最互補 pair | **V11 + Y-diff（任一 thr）** — MI 最低 + joint R 最高 |
| Saturation 在幾個 cue？ | **2 個**（V11 + Y-diff → 0.970）；第 3 個同類 variant 只 +1pp |
| Mode β 最佳 cue | **Y-diff15**（thr=15 優於 thr=30，樣本 n=9，弱信號） |
| Mode α 最佳 cue | Y-diff（any thr），救回 75% |
| Mode M2 最佳 cue | Y-diff30（高 threshold 過濾 noise） |
| Oracle ceiling | **0.9804**（V11 + 兩 Y-diff thr 的 simple OR），gap vs 0.905 = 7.55pp |
| Simple OR vs perfect routing | Simple OR 已是 oracle；mode routing 無額外收益（反 −0.84pp） |
| 下一個最有潛力 cue | FRST（Track A）或 DL（Track L）— 但須先確認與 Y-diff miss frame（M3 + M2 殘餘 25–37.5%）有 overlap |

---

## 7. 剩餘 miss 分析（all cues 的 OR 後）

`V11∪yd15∪yd30` 後剩 **21 miss frames（21/1073 = 2.0%）**。

- V11 miss（102）中：yd15 救 70/102，yd30 救 70/102，兩者 OR 共救 81/102（79.4%）。
- 殘餘 21 miss：V11=0, yd15=0, yd30=0 → 三個 cue 均無信號。
- 這 21 frames 是 **FRST / DL 的最大目標區**。
- Per-mode 精確殘差（V11∪yd15∪yd30）：**M1=10, M2=7, M3=4, M4=0**（合計 21，驗證正確）。

M1 是最大殘餘群（10 frames），為最嚴重 specular/desat 場景（Y-diff 也無法偵到的幀）。

---

## 8. 待辦

1. **FRST 落地後重跑**：`lab-research/outputs/` 出現 `19_frst_*.json` → 補 cue_independence 分析（V11 × Y-diff × FRST 三向 MI）
2. **Track L（DL）**：補相同 per-frame vector + MI 分析
3. **M3 n=9 樣本補充**：增加 hue-shift 場景 session
4. **連續信心分數版 MI**：當 DL 有 confidence score，改用 `mutual_info_regression`

---

_分析日期：2026-05-01_
