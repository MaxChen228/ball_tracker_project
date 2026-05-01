# algo/ — frozen snapshots

`segmenter.py` here is a frozen copy of `server/segmenter.py` at commit
`397577cffb8d2522f9eb3a5c0685ecf10c5a691a` (2026-04-30).

**Why frozen instead of `from server.segmenter import ...`?** So lab
analyses can hold the algorithm constant while iterating on metrics, and
so we can A/B different algorithm variants on the same data without
touching production.

To resync with current production:

```bash
cp ../../server/segmenter.py segmenter.py
# update the commit sha in __init__.py
```

To experiment with a variant: copy to `segmenter_v2.py` (or whatever) and
import explicitly per analysis.
