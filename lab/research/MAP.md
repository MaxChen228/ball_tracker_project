# MAP — lab/research 全景

**單頁入口**。研究在做什麼、哪些 script 服務哪條主線、哪些結論已 lock-in、哪些待重跑。
細節仍在 [`notes/`](notes/) 與 [`scripts/`](scripts/)；這裡只給導覽。

關鍵事實：

- **GT 規模**：15 sessions / ~2553 mask frames（2026-05 起；之前的 9 sessions / 1073 frames 結論在 [`CHANGELOG.md`](CHANGELOG.md) 有對照）
- **iOS 跑的演算法 = HSV+CC+aspect/fill gate**（`ball_tracker/BallDetector.mm`）— 研究稱它為 V11，是研究的起點，不是研究產物
- **研究最高成果 = V11+Y-diff (R=0.970)**，**尚未整合進 iOS**

---

## 演算法世代

```
Gen 3  V11 (HSV+CC+gate)             R=0.905   ← iOS 編譯進去的
                                                 注意：是研究以 iOS baseline 為起點命名，
                                                 不是研究→整合的事件

Gen 4  V11 + Y-diff (thr=15)          R=0.970   ← 推薦 deploy candidate（未整合）
                                                 cue 正交：MI(V11, Y-diff)=0.009 bits

Gen 5  候選（差距遞減）：
       V11 + Y-diff + ROI-FRST         R=0.974  +0.37pp 邊際
       V11 + multi-scale Y-diff        R=0.984  D1+D2+D3 ceiling
       + trajectory gap-fill (post)    R=0.979-0.985  不需新 model

未來方向（尚未開始）：
       Multi-frame stateful（Kalman + ballistic prior） 預估 0.99+
       Capture-side Mode α fix（CPL/ND filter）         物理消除問題
```

---

## 死路（負結果，**已 falsify**）

| 路線 | 失敗根因 | code 位置 |
|---|---|---|
| Dichromatic separation (Yang 2010) | ISP 已 clip 訊號到純灰白，模型前提不成立 | `scripts/_archive/falsified/25_dichromatic.py` |
| Tiny FCN distillation | 1073 frames 不足以從 scratch 訓 dense regression | `scripts/_archive/falsified/23_ensemble_distillation.py` |
| DL upper bound from-scratch | 同上 | `scripts/_archive/falsified/22_dl_upper_bound.py` |
| CLAHE / S-stretch global pre-process | 全域 contrast 破壞 spatial isolation | `scripts/17_clahe_preproc.py` |
| Top-K candidate cap | bg distractors 主導 area top-K | `scripts/13_dual_cube_topk.py` |
| Stateful ROI-FRST | ROI 依賴 recent detection；最難 frame 沒 detector fire | `scripts/24_roi_frst.py`（也是 active；ROI 部分結論為負） |
| Single-frame Mode α algorithmic rescue | M1 frame 已 pure-white，無 chromatic info 可挖 | `scripts/15_v11_failure_modes.py` |

---

## Script ↔ Note ↔ Output 矩陣

### Active（headline，會隨資料重跑）

| Script | 服務 | Note | Output | 狀態 |
|---|---|---|---|---|
| `02_head_to_head.py` | Gen 3 baseline | `02_v11_followup.md` | `head_to_head.npz` | ✅ refreshed (15 sess) |
| `19_frst.py` | Gen 5 cue | `03_frst_eval.md` | `19_frst_results.json` | ✅ refreshed |
| `21_yplane_diff.py` | Gen 4 主力 | `08_yplane_diff.md` | `21_yplane_diff_results.json` | ✅ refreshed |
| `22_cue_independence.py` | Gen 4 理論基礎 | `11_cue_independence.md` | `22_cue_independence.json` | ✅ refreshed |
| `24_roi_frst.py` | Gen 5 候選 | `16_roi_frst.md` | `24_roi_frst_results.json` | ✅ refreshed |
| `26_consensus_residual.py` | 死區結構 | `20_consensus_residual_analysis.md` | `26_consensus_residual.npz` + `_figures/` | ✅ refreshed |
| `26_multiscale_ydiff.py` | Gen 5 ceiling | `19_multiscale_ydiff.md` | `26_multiscale_ydiff_results.json` | ✅ refreshed |

### Legacy（freeze，保留 reproducibility，不維護）

| Script | 用途 | 結論去處 |
|---|---|---|
| `01_sample_pixels.py` | 像素分布 baseline | `01_final_report.md` |
| `03_variant_sweep.py` | V1-V7 變體掃描 | superseded by `02` |
| `04_mask_quality_audit.py` | suspect mask 過濾 | 併入 `05` |
| `05_robust_eval.py` | head-to-head + IoU | `01_final_report.md` |
| `06_ablation.py` | 1-D 屬性貢獻拆解 | `01_final_report.md` |
| `07_failure_modes.py` | M1/M2/M3 早期 | superseded by `15` |
| `08_hue_only.py` | hue-only sanity | 沒結論 |
| `09_refresh_9sessions.py` | 9-session batch runner | 被 active scripts 取代 |
| `10_m1_hsv_profile.py` | M1 specular profile | `02_v11_followup.md` §3 |
| `11_fallback_cube_recovery.py` | fallback HSV cube | 沒進展 |
| `12_dual_cube.py` | 雙 HSV cube | 早期探索 |
| `14_m2_m3_attack.py` | M2/M3 targeted | 沒進展 |
| `15_v11_failure_modes.py` | M1/M2/M3 canonical | `02_v11_followup.md` |
| `16_temporal_structure.py` | trajectory 描述 | descriptive |
| `18_miss_run_physics.py` | 兩 session 物理分析 | `01_final_report.md` |
| `26b_cluster.py` | residual 群手動標 | `20_consensus_residual_analysis.md` G1-G4 |

### Falsified

`scripts/_archive/falsified/` — 三支負結果，code 留著當證據，**不再跑**。

### Library

- `scripts/_paths.py` — ROOT/WS/OUT/NOTES + `load_manifest()` (v1↔v2 適配) + `SEG_BY_SLUG` + `read_mask()` (alpha-PNG 兼容)
- `scripts/ball_detector.py` — `BallDetectorV11` reference impl，被 `19_frst` 和 falsified `23_distillation` import

### `_archive/`（pre-V10 早期探索）

`scripts/_archive/` 已存在，9 支 V1-V9 早期 heuristic。**不要碰**，當化石看。

---

## Notes 入口順序

新讀者依此順序，30 分鐘掌握全景：

1. [`notes/00_synthesis_2026-05-01.md`](notes/00_synthesis_2026-05-01.md) — 唯一 TL;DR
2. [`notes/02_v11_followup.md`](notes/02_v11_followup.md) — Gen 3 baseline 量化
3. [`notes/11_cue_independence.md`](notes/11_cue_independence.md) — Gen 4 理論
4. [`notes/19_multiscale_ydiff.md`](notes/19_multiscale_ydiff.md) — Gen 5 ceiling
5. [`notes/20_consensus_residual_analysis.md`](notes/20_consensus_residual_analysis.md) — 死區結構

其餘 notes（survey、design proposal、early exploration）為 process 文件，不需要循序讀。

---

## 重跑

新 GT propagate 完後重跑：

```bash
cd lab/research
for s in 02_head_to_head 19_frst 21_yplane_diff 22_cue_independence \
         24_roi_frst 26_consensus_residual 26_multiscale_ydiff; do
  uv run --project ../../server python scripts/${s}.py
done
```

或 background：

```bash
mkdir -p outputs/logs
for s in ...; do
  nohup uv run --project ../../server python scripts/${s}.py \
    > outputs/logs/${s}.log 2>&1 &
done
```

依賴：`server/.venv` (uv-managed Python 3.13 + opencv + numpy + scipy + pytorch)。
