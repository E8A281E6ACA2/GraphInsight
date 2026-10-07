#!/usr/bin/env python3
r"""
M5 §16.3 跨语言 targets_hash 向量自检（纯计算，不建引擎、不触库）

`go-backend/internal/adminstore/testdata/targets_hash_vectors.json` 是 Python
`services.reindex_queue.canonical_targets_hash` 与 Go `adminstore.CanonicalTargetsHash`
的字节对等契约。契约只有在"向量确实由实现产出、且集合没有被削薄"时才成立，所以这里逐条复算：

1. 每条向量的 `hash` 必须等于 `canonical_targets_hash(input)` 现算结果 —— 挡住手改期望值；
2. `hash` 必须等于 `sha256(canonical)` —— 挡住 canonical 文本与 hash 脱钩；
3. Wave 9 补齐的边界变体名字必须在场 —— 挡住用例被静默删除；
4. `basic_2` 与 `order_swapped` 必须同 hash —— 排序收敛是这个契约的核心承诺；
5. 向量文件除换行外不得含 C0 控制字节 —— 控制符必须以 `\uXXXX` 文本形态存在，
   落成裸字节会让 Go 读到不同输入，对等测试就成了假绿。

Go 侧的字节对等由 `go test ./internal/adminstore -run TargetsHash` 消费同一份文件，
不在本门禁里调 Go 工具链（CI 的 Python 门禁无 Go）。

运行：python backend/tests/check_m5_targets_hash_vectors.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
VECTORS_FILE = BACKEND.parent / "go-backend" / "internal" / "adminstore" / "testdata" / "targets_hash_vectors.json"

REQUIRED_NAMES = [
    "basic_2",
    "order_swapped",
    "same_chunk_multi_rev",
    "single",
    "unicode_id",
    "html_chars",
    "quote_backslash",
    "control_nl",
    "big_rev",
    "dup_exact",
    "emptyish_min",
    "empty_list",
    "numeric_string_order",
    "case_and_prefix_order",
    "cjk_vs_latin_order",
    "ascii_boundary_tilde_del",
    "nul_and_low_controls",
    "short_escapes",
    "star_plane_emoji",
    "nfc_vs_nfd_acutes",
    "hebrew_rtl",
    "slash_lt_gt_amp",
    "quote_backslash_mix",
    "line_separators_2028",
    "negative_revision",
    "i64_max_revision",
    "same_chunk_rev_tie",
    "fifty_targets_unpadded",
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(BACKEND))
from services.reindex_queue import canonical_targets_hash  # noqa: E402

FAILURES: list = []


def check(condition: bool, label: str) -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(label)


def main() -> int:
    if not VECTORS_FILE.exists():
        print(f"FAIL 向量文件缺失: {VECTORS_FILE}")
        return 1
    raw = VECTORS_FILE.read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL 向量文件不是合法 UTF-8 JSON: {exc}")
        return 1

    vectors = doc.get("vectors") or []
    check(len(vectors) >= len(REQUIRED_NAMES), f"向量条数不少于 {len(REQUIRED_NAMES)}（实测 {len(vectors)}）")

    by_name = {v.get("name"): v for v in vectors}
    missing = [name for name in REQUIRED_NAMES if name not in by_name]
    check(not missing, "Wave 9 边界变体全部在场" + (f"（缺失 {missing}）" if missing else ""))

    control_bytes = sorted({b for b in raw if b < 0x20 and b != 0x0A})
    check(not control_bytes, f"文件内无裸 C0 控制字节（实测 {control_bytes}）")

    for vector in vectors:
        name = vector.get("name")
        payload = [{"chunk_id": item["chunk_id"], "target_revision": item["target_revision"]} for item in vector.get("input", [])]
        canonical = vector.get("canonical", "")
        recorded = vector.get("hash", "")
        check(recorded == canonical_targets_hash(payload), f"{name}: hash 与实现现算一致")
        check(hashlib.sha256(canonical.encode("utf-8")).hexdigest() == recorded, f"{name}: canonical 文本确实产出该 hash")

    basic = by_name.get("basic_2", {}).get("hash")
    swapped = by_name.get("order_swapped", {}).get("hash")
    check(bool(basic) and basic == swapped, "乱序输入收敛到同一 hash（basic_2 == order_swapped）")

    empty_hash = by_name.get("empty_list", {}).get("hash")
    check(
        empty_hash == hashlib.sha256(b"[]").hexdigest(),
        "空 targets 规范化文本就是字面 []",
    )

    print(f"SUMMARY total={len(vectors)} failed={len(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
