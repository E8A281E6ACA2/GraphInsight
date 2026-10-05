import hashlib
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
    # Wave 9 补齐：编码转义与排序规则的边界，逐条对应 Python/Go 可能分叉的地方
    "empty_list": [],
    "numeric_string_order": [("2", 1), ("10", 2), ("1", 3)],
    "case_and_prefix_order": [("B", 1), ("a", 2), ("ab", 3), ("A", 4)],
    "cjk_vs_latin_order": [("z", 1), ("中", 2), ("A", 3)],
    "ascii_boundary_tilde_del": [("~", 1), ("\x7f", 2), (" ", 3)],
    "nul_and_low_controls": [("\x00\x01\x1f", 4)],
    "short_escapes": [("\b", 1), ("\f", 2), ("\t", 3), ("\r", 4)],
    "star_plane_emoji": [("\U0001F600", 1), ("\U0002000B", 2)],
    # NFC 单码点 vs NFD 基字母+组合符：两条写入路径必须把它们当成不同 chunk
    "nfc_vs_nfd_acutes": [("é", 1), ("é", 2)],
    "hebrew_rtl": [("א", 1), ("ב", 2)],
    "slash_lt_gt_amp": [("/", 1), ("<", 2), (">", 3), ("&", 4)],
    "quote_backslash_mix": [("\\", 1), ('\\"', 2), ("\\\\", 3), ("'", 4)],
    "line_separators_2028": [("\u2028", 1), ("\u2029", 2)],
    "negative_revision": [("n", -5)],
    "i64_max_revision": [("m", 9223372036854775807)],
    "same_chunk_rev_tie": [("t1", 0), ("t1", -1), ("t1", 0)],
    "fifty_targets_unpadded": [(f"c{i}", i % 5) for i in range(49, -1, -1)],
}

out = {"vectors": []}
for name, items in cases.items():
    payload = [{"chunk_id": c, "target_revision": r} for c, r in items]
    canon = ordered_canon(items)
    vector = {
        "name": name,
        "input": payload,
        "canonical": canon,
        "hash": canonical_targets_hash(payload),
    }
    # 自证：canonical 必须真是 hash 的输入字节，否则向量文件与实现脱钩
    if hashlib.sha256(canon.encode("utf-8")).hexdigest() != vector["hash"]:
        raise SystemExit(f"{name}: canonical 与 hash 不匹配")
    out["vectors"].append(vector)

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
