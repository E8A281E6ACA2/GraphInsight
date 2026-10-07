#!/usr/bin/env python3
"""`probe_c3_sources.py` 四来源判定的**负向回归**（纯函数，零真实服务连接）。

本轮修复的 fail-open：
  - Milvus 业务错误 JSON（`code != 0`，但容器进程退出码 0）此前会被标 OK；
  - Neo4j 的分组查询 / 目标 KB 查询失败此前也会被标 OK（只看总数查询的 rc）。

合同：**任一子查询失败 → 整来源 UNVERIFIED**（不得折成 0，不得断言 KB 全空）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import probe_c3_sources as p  # noqa: E402

PASSES = []
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSES if cond else FAILURES).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" ({detail})" if detail and not cond else ""))


OK = (0, "0")


def test_pg() -> None:
    res = p.classify_pg(OK, (0, "kb-a|3"), (0, "1"))
    check("PG 三支全成功 → OK", res["status"] == "OK", str(res))
    for label, trip in (
        ("total 失败", ((1, "boom"), OK, OK)),
        ("per_kb 失败", (OK, (1, "boom"), OK)),
        ("current 失败", (OK, OK, (1, "boom"))),
    ):
        res = p.classify_pg(*trip)
        check(f"PG {label} → UNVERIFIED（不看其它支）", res["status"] == "UNVERIFIED", str(res))


def test_neo4j() -> None:
    res = p.classify_neo4j((0, "n\n0"), OK, OK)
    check("Neo4j 三支全成功 → OK", res["status"] == "OK", str(res))
    for label, trip in (
        ("total 失败", ((1, "boom"), OK, OK)),
        ("分组查询失败", (OK, (1, "boom"), OK)),
        ("目标 KB 查询失败", (OK, OK, (1, "boom"))),
    ):
        res = p.classify_neo4j(*trip)
        check(f"Neo4j {label} → UNVERIFIED", res["status"] == "UNVERIFIED", str(res))


def test_milvus() -> None:
    good = {"collections": {"code": 0, "data": ["graphinsight_chunks_v2"]},
            "stats": {"graphinsight_chunks_v2": {"code": 0, "data": {"rowCount": 0}}},
            "by_kb": {"graphinsight_chunks_v2": {"code": 0, "data": []}}}
    res = p.classify_milvus(good)
    check("Milvus 全 code=0 → OK", res["status"] == "OK", str(res))
    check("Milvus v3 不存在被识别", res.get("v3_exists") is False, str(res))

    # fail-open 反例：业务错误 JSON（进程退出码仍是 0）
    bad_collections = dict(good, collections={"code": 1, "message": "collection not found"})
    check("Milvus collections code!=0 → UNVERIFIED",
          p.classify_milvus(bad_collections)["status"] == "UNVERIFIED")
    bad_stats = dict(good, stats={"graphinsight_chunks_v2": {"code": 65535, "message": "fail"}})
    check("Milvus stats code!=0 → UNVERIFIED",
          p.classify_milvus(bad_stats)["status"] == "UNVERIFIED")
    bad_bykb = dict(good, by_kb={"graphinsight_chunks_v2": {"code": 2, "message": "fail"}})
    check("Milvus by_kb code!=0 → UNVERIFIED",
          p.classify_milvus(bad_bykb)["status"] == "UNVERIFIED")
    err_resp = dict(good, stats={"c": {"error": "urllib.error.URLError: ..."}})
    check("Milvus 子响应含 error → UNVERIFIED",
          p.classify_milvus(err_resp)["status"] == "UNVERIFIED")
    check("Milvus 非 dict → UNVERIFIED",
          p.classify_milvus("not-a-dict")["status"] == "UNVERIFIED")
    check("Milvus 缺失 code → UNVERIFIED",
          p.classify_milvus({"collections": {"data": []}})["status"] == "UNVERIFIED")
    check("Milvus 缺失 stats/by_kb 子查询 → UNVERIFIED",
          p.classify_milvus({"collections": {"code": 0, "data": ["c"]},
                             "stats": {}, "by_kb": {}})["status"] == "UNVERIFIED")
    check("Milvus 非整数 code → UNVERIFIED",
          p.classify_milvus({"collections": {"code": 0.5, "data": []},
                             "stats": {}, "by_kb": {}})["status"] == "UNVERIFIED")
    for label, payload in (
        ("缺少响应映射", {"collections": good["collections"]}),
        ("缺少单个 stats", dict(good, stats={})),
        ("缺少单个 by_kb", dict(good, by_kb={})),
        ("缺少 collections data", dict(good, collections={"code": 0})),
        ("布尔 code", dict(good, collections={"code": False, "data": []})),
        ("字符串 code", dict(good, collections={"code": "0", "data": []})),
    ):
        check(f"Milvus {label} → UNVERIFIED",
              p.classify_milvus(payload)["status"] == "UNVERIFIED")
    check("Milvus 明确空集合清单与空响应映射 → OK",
          p.classify_milvus({"collections": {"code": 0, "data": []},
                             "stats": {}, "by_kb": {}})["status"] == "OK")


def test_parsed() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        res = p.parsed_stats("kb-x", root)
        check("解析产物：根存在但无 KB 目录 → OK 且 0 条",
              res["status"] == "OK" and res["target_kb_chunks_total"] == 0, str(res))
        kb = root / "kb-x"
        doc = kb / "d1"
        doc.mkdir(parents=True)
        (doc / "chunks.jsonl").write_text(
            '{"chunk_id":"c1","doc_id":"d1","text":"t"}\n'
            '{"chunk_id":"c2","doc_id":"d1","text":"t2"}\n', encoding="utf-8")
        res = p.parsed_stats("kb-x", root)
        check("解析产物：2 条有效 → chunks_total=2",
              res["status"] == "OK" and res["target_kb_chunks_total"] == 2, str(res))
        # 无效行必须被计数（不得静默吞掉）
        (doc / "chunks.jsonl").write_text(
            '{"chunk_id":"c1"}\n{bad json\n', encoding="utf-8")
        res = p.parsed_stats("kb-x", root)
        check("解析产物：坏行计入 invalid_lines（不静默）",
              res["status"] == "OK" and res["target_kb_docs"][0]["invalid_lines"] == 1, str(res))
    check("解析产物：根不存在 → UNVERIFIED",
          p.parsed_stats("kb-x", Path("/definitely/not/here"))["status"] == "UNVERIFIED")


def main() -> int:
    test_pg()
    test_neo4j()
    test_milvus()
    test_parsed()
    print()
    print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for name in FAILURES:
            print("  FAILED: " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
