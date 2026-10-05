import json
import sys

sys.path.insert(0, ".")
from services.reindex_queue import canonical_targets_hash


def ordered_canon(items):
    ordered = sorted(
        [{"chunk_id": str(c), "target_revision": int(r)} for c, r in items],
        key=lambda i: (i["chunk_id"], i["target_revision"]),
    )
    return json.dumps(ordered, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


cases = {
    "basic_2": [("c1", 1), ("c2", 2)],
    "order_swapped": [("c2", 2), ("c1", 1)],
    "same_chunk_multi_rev": [("c1", 2), ("c1", 1)],
    "single": [("x", 7)],
    "unicode_id": [("区块A", 3), ("chunk", 9)],
    "html_chars": [("a<b>c&d", 5), ("e/f", 1)],
    "quote_backslash": [('q"z', 2), ("bs\\", 3)],
    "control_nl": [("l\nm", 4)],
    "big_rev": [("z", 123456789)],
    "dup_exact": [("d1", 1), ("d1", 1)],
    "emptyish_min": [("0", 0)],
}

out = {"vectors": []}
for name, items in cases.items():
    out["vectors"].append(
        {
            "name": name,
            "input": [{"chunk_id": c, "target_revision": r} for c, r in items],
            "canonical": ordered_canon(items),
            "hash": canonical_targets_hash([{"chunk_id": c, "target_revision": r} for c, r in items]),
        }
    )

import os

candidates = [
    "../go-backend/internal/adminstore/testdata/targets_hash_vectors.json",
    os.path.join("go-backend", "internal", "adminstore", "testdata", "targets_hash_vectors.json"),
]
target = next((p for p in candidates if os.path.isdir(os.path.dirname(p))), candidates[0])
os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("wrote", len(out["vectors"]), "vectors ->", os.path.abspath(target))
