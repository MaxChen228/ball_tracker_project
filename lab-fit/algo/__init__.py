"""Frozen algorithm snapshot for lab analyses.

Snapshot originally taken from server/ at commit
397577cffb8d2522f9eb3a5c0685ecf10c5a691a. Locally patched 2026-05-27
with a `dedupe_rank_by` kwarg + chord-based dedupe so the sweep script
can A/B "legacy" vs "chord" before the same change lands in server/.
Re-copy from server/ once that lands and drop the kwarg.
"""
