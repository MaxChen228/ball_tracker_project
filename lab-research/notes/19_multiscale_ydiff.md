# 19 — Multi-scale Y-diff 評估（D1/D2/D3，apex 假設驗證）

腳本：`lab-research/scripts/26_multiscale_ydiff.py`  
結果：`lab-research/outputs/26_multiscale_ydiff_results.json`  
基準：V11+D1 = 0.9692（Track J baseline，thr=15，本次 D1 重現值）

---

## 1. 方法

三條 diff 流，統一 thr=15：

| 流 | 定義 | 需要 buffer |
|---|---|---|
| D1 | \|Y[t] − Y[t-1]\| | ≥1 frame |
| D2 | \|Y[t] − Y[t-2]\| | ≥2 frames |
| D3 | \|Y[t] − Y[t-3]\| | ≥3 frames |

各流：`absdiff → threshold(15) → MORPH_CLOSE 3×3 → V11 shape gate`  
buffer 不足時：明確 emit 0 candidates，不造假。  
Union dedup：centroid 距離 ≤5 px 視為同一 candidate，後者不加入。

Apex 假設：對每個 session 的 GT Y 軌跡做 2 次多項式 ballistic fit，取導數 |vy_fit|，把最小 20 百分位（|vy| ≤ p20）定義為「apex-proxy 幀」。

---

## 2. Per-stream alone R

| 流 | R_alone | hits/total | FP cands/f (noball) | no_buf |
|---|---|---|---|---|
| D1 | **0.5200** | 558/1073 | 1088 | 9 |
| D2 | **0.5377** | 577/1073 | 1366 | 18 |
| D3 | **0.6347** | 681/1073 | 1294 | 27 |

**觀察**：
- D3 alone R=0.635，比 D1 (0.520) 高 11.5pp。
- D2/D3 的 FP cands/f 比 D1 更高（1366 vs 1088），因為大位移 diff 把更大區域納入。
- 三流均不可單用（FP rate 過高，與 21_yplane_diff 結論一致）。

---

## 3. 累積 union R

| 組合 | R | hits/1073 | ΔR vs V11 |
|---|---|---|---|
| V11 alone | 0.9049 | 971 | — |
| V11 ∪ D1（Track J baseline） | 0.9692 | 1040 | +0.0643 |
| V11 ∪ D1 ∪ D2 | **0.9804** | 1052 | +0.0755 |
| V11 ∪ D1 ∪ D3 | **0.9804** | 1052 | +0.0755 |
| V11 ∪ D1 ∪ D2 ∪ D3 | **0.9842** | 1056 | +0.0792 |

**逐步收益**：

| 加入 | 新增 R | 新增 frames |
|---|---|---|
| D1（第一條） | +0.0643 | 69 |
| D2（第二條） | +0.0112 | 12 |
| D3（替換 D2 同位置） | +0.0112 | 12（完全重疊） |
| D2+D3 同時（第二+三） | +0.0112 | 12（共同救回 12 幀） |
| 全加（D1+D2+D3） | +0.0149 | 16（比 D2 alone 多救 4 幀） |

注意：V11∪D1∪D2 = V11∪D1∪D3 = 0.9804（D2 與 D3 的貢獻完全重疊在 12 幀上）。  
D1+D2+D3 全加後再多救 4 幀（0.9842），邊際效益遞減顯著。

---

## 4. V11+D1 殘差 miss 分析

V11+D1 miss 幀數：33  

| 能從 33 miss 中救回的 | 數量 | 佔殘差% |
|---|---|---|
| D2 alone | 12 | 36.4% |
| D3 alone | 14 | 42.4% |
| D2 ∪ D3 | 18 | 54.5% |

D2∪D3 能搶回殘差過半，但剩餘 15 幀（45.5%）D2/D3 均失敗——這些是更難案例（完全 desat / 靜態背景 luma 相近）。

---

## 5. Mode 拆解（V11 miss 幀，proxy 分類）

| mode | V11 miss n | D1 rec% | D2 rec% | D3 rec% |
|---|---|---|---|---|
| M1 (desat, gt_s<80) | 90 | 72.2% | **77.8%** | **81.1%** |
| M2 (fragmentation) | 9 | 55.6% | 22.2% | 22.2% |
| M3 (hue-shift, gt_h<100) | 3 | 0.0% | 33.3% | 0.0% |

**核心觀察**：
- M1 是主場：D2 比 D1 多救 5 幀（5.6pp），D3 比 D1 多救 8 幀（8.9pp）。大位移 baseline 讓深度 desat 球的邊緣 contrast 信號更強。
- M2 regression：D2/D3 在 fragmentation 幀救回率**下降**（55.6% → 22.2%）。大位移 diff 在碎裂球上會把多個碎片合並或和背景重疊，CC 形狀通過不了 shape gate。
- M3 樣本過少（3 幀），不得出結論。

---

## 6. Apex 假設驗證（quantitative）

Ballistic fit 有效 sessions：9 sessions。  
apex-proxy 定義：|vy_fit| ≤ p20 = **1.39 px/frame**（210 幀）。  
fast frames：|vy_fit| > 1.39（838 幀）。

| 流 | apex hit% | fast hit% | apex > fast? | D_n − D1 @ apex |
|---|---|---|---|---|
| D1 | 74.3% | 46.7% | YES | —（baseline） |
| D2 | 70.0% | 50.6% | YES | **−4.3pp** |
| D3 | 71.9% | 63.2% | YES | −2.4pp |

**關鍵發現**：

1. **Apex 假設反假設**：D2/D3 在 apex 幀的 hit% **低於** D1（而非更高）。D2 在 apex 幀比 D1 低 4.3pp，D3 低 2.4pp。

2. **D3 在 fast 幀的優勢大**（D3−D1 = +16.6pp）：大位移 diff 在球速快時信號更強（3 frames × ~10px/frame = ~30px baseline；slow apex 只有 ~3px）——這是物理直覺的反轉，但邏輯成立：D3 的時間窗口本身不帶來「更快的慢球偵測」，而是在快球時給出更強的 motion signal。

3. **「apex 需要多尺度」假設被 falsified**：
   - 球在 apex 時 V(image-Y) 趨近 0 → |Y diff| 確實更弱
   - 但 D2/D3 的位移 baseline 在 apex 時也只有 2-3 px，不足以補償信號
   - D3 在 fast 幀補強才是真實的效益來源

---

## 7. FP rate 比較

| 流 | FP cands/f (noball) |
|---|---|
| D1 | 1088 |
| D2 | **1366（+25.5%）** |
| D3 | 1294（+19.0%） |

D2/D3 FP 更高，符合預期（更大 baseline → 更多 motion blur 與背景變動）。  
三流均需要相同的 FP 抑制策略（Option A area_min=50 / Option C V11-miss-only）。

---

## 8. 與 V11+Y-diff baseline 的 Pareto 比較

| 配置 | R | FP cands/f | ms/frame 估算 | 備注 |
|---|---|---|---|---|
| V11 alone | 0.905 | ~8 | ~2ms | production |
| V11 ∪ D1 | **0.969** | ~1088 | ~4ms | Track J baseline |
| V11 ∪ D1 ∪ D2 | **0.980** | ~1400 est | ~5ms | +1.1pp |
| V11 ∪ D1 ∪ D2 ∪ D3 | **0.984** | ~1500 est | ~6ms | +1.5pp total |

**Pareto 邊界**：

- **V11+D1**（0.969）仍是最 cost-efficient 單一加法：+6.4pp / +2ms / +1088 FP。
- D2 或 D3 各加 +1.1pp，但也各加 ~300-400 FP cands/f 和 ~1ms。
- 全加（D1+D2+D3）比 D1 多 +1.5pp（16 幀），FP 和計算成本都增加。

若要從 0.969 繼續提升，多尺度確實有效，但 cost 加成是線性的、效益是遞減的。

---

## 9. 結論

| 問題 | 答案 |
|---|---|
| D1 alone R | 0.520（重現 Track J baseline） |
| D2 alone R | 0.538（+1.8pp vs D1） |
| D3 alone R | 0.635（+11.5pp vs D1） |
| V11 ∪ D1 | 0.969（Track J，baseline） |
| V11 ∪ D1 ∪ D2 | **0.980（+1.1pp）** |
| V11 ∪ D1 ∪ D3 | **0.980（+1.1pp）** |
| V11 ∪ D1 ∪ D2 ∪ D3 | **0.984（+1.5pp）** |
| apex 假設驗證 | **Falsified**：D2/D3 在 apex 幀比 D1 弱（−2.4 至 −4.3pp）；D3 在 fast 幀才佔優（+16.6pp） |
| 多尺度補強 V11+D1 殘差？ | 是，D2∪D3 從 33 殘差幀中再救 18（54.5%）|
| 哪個 fold 最受益 | M1（desat）：D3 比 D1 多救 8.9pp |
| M2 regression | 是：D2/D3 在 fragmentation 幀從 55.6% 降至 22.2% |

**生產整合判斷**：

多尺度（D2/D3）能把 V11+D1 從 0.969 推到 0.984（+1.5pp / 16 幀）。  
代價：FP cands/f 從 1088 增至 ~1500（+38%），計算多 ~2ms（串行）。

- **若只做一步**：V11+D1 仍是最佳 trade-off（+6.4pp 的大頭都在 D1）。
- **若要積極追求 recall**：加 D3（fast-frame 補強）比加 D2 帶來相同 union R，但 FP 略低。
- **不建議三流全加到 production**：+1.5pp 在 live 場景邊際效益低（每 pitch 約幾幀差異），而 FP pool 增大會放慢 triangulation。
- **apex-only 啟動策略無效**：apex 假設已被 falsified，不存在「只在 apex 幀啟動 D2/D3 就免 FP 代價」的 cherry-pick。

**整合建議**：D1 alone（Option C, V11-miss-only activation）仍是最低風險起點。  
若要追 +1.5pp，優先用 D3（fast-frame strong），配合 area_min=50 FP 壓制。
