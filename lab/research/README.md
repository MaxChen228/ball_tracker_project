# lab/research

球體偵測研究工作區。獨立於 `lab/` 標註流程，**用途是研究與驗證，不是 production code**。

## 先看哪裡

- 當前總結：[`notes/00_synthesis_2026-05-01.md`](notes/00_synthesis_2026-05-01.md)
- V11 主報告：[`notes/02_v11_followup.md`](notes/02_v11_followup.md)
- 筆記索引：[`notes/README.md`](notes/README.md)
- 產物索引：[`outputs/README.md`](outputs/README.md)

## 現況一句話

目前最有價值的結論不是 single-frame HSV，而是：

- `V11`：recall `0.905`
- `V11 + Y-diff(thr=15)`：recall `0.970`
- `ROI-FRST`：只有邊際增益
- tiny FCN / distillation：在 1073 GT frames 規模下失敗

詳見 [`notes/00_synthesis_2026-05-01.md`](notes/00_synthesis_2026-05-01.md)。

## 目錄

```
lab/research/
├── README.md          入口與重跑方式
├── notes/             研究筆記、報告、設計稿
├── outputs/           腳本產出的量化結果與視覺化
└── scripts/           可重跑研究腳本
    └── _archive/      已確認 dead-end 的舊探索腳本
```

這裡先**不做實體搬檔**。很多 script / note 直接引用既有檔名與路徑，先把索引補齊，避免整理過程把研究鏈條弄斷。

## 研究主線

| 範圍 | 主要文件 | 對應腳本 / 產物 |
|---|---|---|
| V10/V11 HSV 基線 | [`01_final_report.md`](notes/01_final_report.md), [`02_v11_followup.md`](notes/02_v11_followup.md) | `01`-`18`, `ball_detector.py` |
| Y-plane / temporal cues | [`08_yplane_diff.md`](notes/08_yplane_diff.md), [`11_cue_independence.md`](notes/11_cue_independence.md), [`19_multiscale_ydiff.md`](notes/19_multiscale_ydiff.md) | `21`, `22`, `26_multiscale_ydiff.py` |
| FRST / ROI-FRST | [`03_frst_eval.md`](notes/03_frst_eval.md), [`16_roi_frst.md`](notes/16_roi_frst.md) | `19_frst.py`, `24_roi_frst.py` |
| Distillation / DL feasibility | [`09_a15_detection_benchmarks.md`](notes/09_a15_detection_benchmarks.md), [`12_ensemble_distillation_design.md`](notes/12_ensemble_distillation_design.md), [`13_ensemble_distillation_results.md`](notes/13_ensemble_distillation_results.md) | `22_dl_upper_bound.py`, `23_ensemble_distillation.py` |
| Physical / failure-mode analysis | [`17_dichromatic_design.md`](notes/17_dichromatic_design.md), [`18_dichromatic_results.md`](notes/18_dichromatic_results.md), [`20_consensus_residual_analysis.md`](notes/20_consensus_residual_analysis.md) | `25_dichromatic.py`, `26_consensus_residual.py`, `26b_cluster.py` |
| Side investigations | [`03_dual_pipeline_arch.md`](notes/03_dual_pipeline_arch.md), [`04_literature_survey.md`](notes/04_literature_survey.md), [`05_github_survey.md`](notes/05_github_survey.md), [`07_epipolar_rescue.md`](notes/07_epipolar_rescue.md), [`14_vision_circle_api_check.md`](notes/14_vision_circle_api_check.md), [`15_yolo_ane_latency_check.md`](notes/15_yolo_ane_latency_check.md) | mostly note-only |

## 環境

跑在 `server/.venv`（`uv` 管理，Python 3.13）。

- 傳統 CV 腳本主要依賴：`opencv-python`, `numpy`, `scipy`
- DL 腳本另外依賴：`torch`

不是所有 script 都是「純 cv2 + numpy + scipy」。`22_dl_upper_bound.py` 和 `23_ensemble_distillation.py` 需要 PyTorch。

## 重跑方式

大部分腳本都假設從 repo root 或 `lab/research/` 執行最穩：

```bash
cd /Users/chenliangyu/Desktop/active/ball_tracker_project/lab/research
uv run python scripts/19_frst.py
uv run python scripts/21_yplane_diff.py
uv run python scripts/22_cue_independence.py
uv run python scripts/23_ensemble_distillation.py
```

若你想從 `server/` 執行，也可以：

```bash
cd /Users/chenliangyu/Desktop/active/ball_tracker_project/server
uv run python ../lab/research/scripts/19_frst.py
```

所有腳本都讀 `lab/standalone_workspace/manifest.json` 與對應 frame/mask，輸出到 `lab/research/outputs/`。

## 命名規則

- `notes/<nn>_*.md`：研究筆記或報告
- `scripts/<nn>_*.py`：實驗腳本
- `outputs/<nn>_*.{json,npz,png,csv}`：該階段的主要產物

同一編號不保證嚴格一對一；有些 track 只有 note、有些只有 script，有些是多檔輸出。對照表放在 [`notes/README.md`](notes/README.md) 和 [`outputs/README.md`](outputs/README.md)。

## 已知缺口

- `scripts/22_dl_upper_bound.py` 提到的 `notes/10_dl_upper_bound.md` 和 `outputs/22_dl_upper_bound_results.json` 目前不在 tree 內，視為未整理完成的研究分支。
- `outputs/` 目前約數百 MB，包含大型 `.npz` 與視覺化 PNG；先保留原路徑，不拆子資料夾，避免破壞現有引用。
