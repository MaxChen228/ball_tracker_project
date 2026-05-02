# notes/

`lab/research` 的文字入口。**讀序見 [`../MAP.md`](../MAP.md)**；本表只是按編號列清單。

狀態標籤：

- 🔒 **locked** — 結論已確定，新 GT 不會改變判斷
- 🔄 **n-dependent** — 數字會隨 GT 規模微調，主結論穩定（見 `../CHANGELOG.md`）
- ❌ **falsified** — 負結果，路線退役
- 📚 **reference** — survey / 外部資訊查核
- 🗄 **superseded** — 被新 note 取代，留作 trace

## Index

| Note | Type | 狀態 | 角色 |
|---|---|---|---|
| [`00_synthesis_2026-05-01.md`](00_synthesis_2026-05-01.md) | report | 🔄 | **入口**。研究總整（部分數字停在 9-sess，看 CHANGELOG 取最新） |
| [`01_final_report.md`](01_final_report.md) | report | 🗄 | 7-session V10 原報告，被 `02` 取代 |
| [`02_v11_followup.md`](02_v11_followup.md) | report | 🔄 | V11 baseline 主量化（Gen 3） |
| [`03_dual_pipeline_arch.md`](03_dual_pipeline_arch.md) | design | 🗄 | 雙路徑架構提案（已 obsolete，現為單 pipe） |
| [`03_frst_eval.md`](03_frst_eval.md) | report | 🔒 | FRST 單路 R=0.96，FP 9730/frame 不可單用 |
| [`04_literature_survey.md`](04_literature_survey.md) | survey | 📚 | 文獻掃描 |
| [`05_github_survey.md`](05_github_survey.md) | survey | 📚 | 外部實作掃描 |
| [`06_orthogonal_ideas.md`](06_orthogonal_ideas.md) | design | 📚 | 正交 cue 腦暴（Y-diff 已驗證） |
| [`07_epipolar_rescue.md`](07_epipolar_rescue.md) | design | ⏸ blocked | stereo 解，待 charuco 雙機校正 |
| [`08_yplane_diff.md`](08_yplane_diff.md) | report | 🔄 | Y-diff 初版（Gen 4 起點） |
| [`09_a15_detection_benchmarks.md`](09_a15_detection_benchmarks.md) | survey | 📚 | A15/ANE latency 事實查核 |
| `10_dl_upper_bound.md` | — | ⛔ missing | `scripts/_archive/falsified/22_dl_upper_bound.py` 引用，從未寫 |
| [`11_cue_independence.md`](11_cue_independence.md) | report | 🔄 | **Gen 4 理論基礎**：MI(V11, Y-diff)=0.009 |
| [`12_ensemble_distillation_design.md`](12_ensemble_distillation_design.md) | design | ❌ | distillation proposal（被 13 falsify） |
| [`13_ensemble_distillation_results.md`](13_ensemble_distillation_results.md) | report | ❌ | distillation 1073 frames R=0.105，路線退役 |
| [`14_vision_circle_api_check.md`](14_vision_circle_api_check.md) | survey | 📚 | Apple Vision circle API 不存在 |
| [`15_yolo_ane_latency_check.md`](15_yolo_ane_latency_check.md) | survey | 📚 | YOLO ANE latency 標稱數字 myth-bust |
| [`16_roi_frst.md`](16_roi_frst.md) | report | 🔒 | ROI-FRST +0.37pp 邊際，不值得 deploy |
| [`17_dichromatic_design.md`](17_dichromatic_design.md) | design | ❌ | dichromatic proposal（被 18 falsify） |
| [`18_dichromatic_results.md`](18_dichromatic_results.md) | report | ❌ | dichromatic falsified by ISP clipping |
| [`19_multiscale_ydiff.md`](19_multiscale_ydiff.md) | report | 🔄 | **Gen 5 ceiling** D1+D2+D3=0.984 |
| [`20_consensus_residual_analysis.md`](20_consensus_residual_analysis.md) | report | 🔄 | 死區結構：G1 edge / G2 specular / G3 low-contrast / G4 mixed |

## 命名上的現況

- `03_*` 兩個檔（一 design 一 report），不同分支同編號，不更名。
- `10_*` 缺號是該研究線文件未補齊（DL upper bound 已退役）。
- 21+ 編號之後的實驗大多直接走 `scripts/` + `outputs/`，note 不一定有對應。
