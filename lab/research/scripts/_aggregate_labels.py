"""Aggregate per-session __labels.json into one manual_labels.json + summary."""
from __future__ import annotations
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import OUT

AUDIT = OUT / "_mask_audit"


def main():
    files = sorted(AUDIT.glob("*__labels.json"))
    agg = {}
    summary_rows = []
    grand = {"bad": 0, "borderline": 0, "ok": 0}
    for fp in files:
        d = json.loads(fp.read_text())
        slug = d["slug"]
        labels = d["labels"]
        per_slug = {}
        c = {"bad": 0, "borderline": 0, "ok": 0}
        for entry in labels:
            src = int(entry["src"])
            lab = entry["label"]
            note = entry.get("note", "")
            per_slug[str(src)] = {"label": lab, "note": note}
            c[lab] = c.get(lab, 0) + 1
            grand[lab] = grand.get(lab, 0) + 1
        agg[slug] = per_slug
        summary_rows.append((slug, len(labels), c["bad"], c["borderline"], c["ok"]))

    out_path = AUDIT / "manual_labels.json"
    out_path.write_text(json.dumps(agg, indent=2))

    # Bad-rate by reason (cross-reference sidecar)
    by_reason = {}
    for fp in files:
        slug = fp.stem.replace("__labels", "")
        sidecar_fp = AUDIT / f"{slug}__mixed.json"
        if not sidecar_fp.exists():
            continue
        sidecar = json.loads(sidecar_fp.read_text())
        reason_by_src = {int(c["src"]): c["reason"] for c in sidecar["candidates"]}
        labels_by_src = {int(e["src"]): e["label"] for e in
                         json.loads(fp.read_text())["labels"]}
        for src, lab in labels_by_src.items():
            r = reason_by_src.get(src, "?")
            by_reason.setdefault(r, {"bad": 0, "borderline": 0, "ok": 0, "n": 0})
            by_reason[r][lab] += 1
            by_reason[r]["n"] += 1

    print("\nBad-rate by selection reason (precision of each signal):")
    print(f"{'reason':<8} {'N':>4} {'bad':>5} {'bord':>5} {'ok':>5}  bad%")
    for r in ("WORST", "DRIFT", "TEMP", "RAND"):
        if r not in by_reason:
            continue
        c = by_reason[r]
        pct = 100.0 * c["bad"] / c["n"] if c["n"] else 0.0
        print(f"{r:<8} {c['n']:>4} {c['bad']:>5} {c['borderline']:>5} {c['ok']:>5}  {pct:5.1f}%")

    # Summary print
    print(f"{'slug':<28} {'N':>4} {'bad':>5} {'bord':>5} {'ok':>5}  bad%")
    print("-" * 60)
    for slug, n, b, bo, o in sorted(summary_rows, key=lambda r: -r[2]):
        pct = 100.0 * b / n if n else 0.0
        print(f"{slug:<28} {n:>4} {b:>5} {bo:>5} {o:>5}  {pct:5.1f}%")
    total = sum(r[1] for r in summary_rows)
    print("-" * 60)
    print(f"{'TOTAL':<28} {total:>4} {grand['bad']:>5} {grand['borderline']:>5} {grand['ok']:>5}  "
          f"{100.0 * grand['bad'] / total:5.1f}%")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
