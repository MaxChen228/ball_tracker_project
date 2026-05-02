"""Convert per-frame HSV ratio into auto labels for the 13 confirmed
blue-ball sessions. Special-case the 3 anomalous sessions.

Thresholds (validated empirically iter3 sheet):
  r >= 0.60 -> ok          (mask is mostly ball pixels)
  0.30 <= r < 0.60 -> borderline  (HSV-cleaned mask is better GT)
  r < 0.30 -> bad          (mask drifted off ball or major merger)
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import OUT

AUDIT = OUT / "_mask_audit"

BLUE_BALL_SESSIONS = {
    "session_s_16ec069a_b", "session_s_170a6a89_a", "session_s_170a6a89_b",
    "session_s_21af9a82_a", "session_s_21af9a82_b",
    "session_s_22d1835e_a", "session_s_22d1835e_b",
    "session_s_2546618f_a", "session_s_2546618f_b",
    "session_s_2ac8f0de_a", "session_s_2ac8f0de_b",
    "session_s_36fc7945_a", "session_s_36fc7945_b",
    "session_s_3a4cf041_a", "session_s_3a4cf041_b",
}
SPECIAL = {
    "session_s_373bbf6e_b": "drop_entire_session_gt_drift_to_non_ball",
}


def classify(r: float) -> str:
    if r >= 0.60:
        return "ok"
    if r >= 0.30:
        return "borderline"
    return "bad"


def main():
    src = json.loads((AUDIT / "hsv_ratio_per_frame.json").read_text())
    out = {}
    summary = []
    for slug, frames in src.items():
        if slug in SPECIAL:
            out[slug] = {"_status": "special", "_reason": SPECIAL[slug]}
            summary.append((slug, len(frames), "?", "?", "?", SPECIAL[slug]))
            continue
        if slug not in BLUE_BALL_SESSIONS:
            continue
        per = {}
        c = {"bad": 0, "borderline": 0, "ok": 0}
        for f in frames:
            label = classify(f["ratio"])
            per[str(f["src"])] = {
                "label": label,
                "ratio": f["ratio"],
                "area": f["area"],
                "hsv_area": f["hsv_area"],
            }
            c[label] += 1
        out[slug] = per
        summary.append((slug, len(frames), c["bad"], c["borderline"], c["ok"], ""))

    out_path = AUDIT / "auto_labels.json"
    out_path.write_text(json.dumps(out, indent=2))

    print(f"{'slug':<28} {'N':>4} {'bad':>5} {'bord':>5} {'ok':>5}  bad%  note")
    print("-" * 76)
    grand = {"N": 0, "bad": 0, "borderline": 0, "ok": 0}
    for slug, n, b, bo, o, note in sorted(summary, key=lambda r: -r[2] if isinstance(r[2], int) else -1):
        if isinstance(b, int):
            pct = 100.0 * b / n if n else 0.0
            print(f"{slug:<28} {n:>4} {b:>5} {bo:>5} {o:>5}  {pct:5.1f}%  {note}")
            grand["N"] += n
            grand["bad"] += b
            grand["borderline"] += bo
            grand["ok"] += o
        else:
            print(f"{slug:<28} {n:>4} {'?':>5} {'?':>5} {'?':>5}  ----- {note}")
    print("-" * 76)
    pct_g = 100.0 * grand["bad"] / grand["N"] if grand["N"] else 0.0
    print(f"{'TOTAL (13 blue sessions)':<28} {grand['N']:>4} {grand['bad']:>5} "
          f"{grand['borderline']:>5} {grand['ok']:>5}  {pct_g:5.1f}%")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
