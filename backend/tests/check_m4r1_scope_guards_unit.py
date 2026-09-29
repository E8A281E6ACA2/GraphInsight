"""M4-R1 修复批次：作用域安全护栏单元测试（无需外部服务）。

覆盖审计 P0-4（检索后置过滤 fail-closed）与 P0-5（NL2Cypher 无法证明注入即拒绝）。
两个被测函数都是纯逻辑，不触达 Neo4j/Milvus/LLM，可用系统 Python 直接运行：

    python backend/tests/check_m4r1_scope_guards_unit.py
"""
import os
import sys

# 允许以 `python backend/tests/...` 直接运行（backend 在路径上）。
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from services.nl2cypher_service import NL2CypherService  # noqa: E402
from services.retrieval_orchestrator import RetrievalOrchestrator  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, condition):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}")


def _inject(cypher, kb_ids):
    # _inject_kb_scope 不依赖实例状态，用 __new__ 跳过需要外部配置的 __init__。
    svc = NL2CypherService.__new__(NL2CypherService)
    return svc._inject_kb_scope(cypher, kb_ids)


def _raises(fn, *args):
    try:
        fn(*args)
        return False
    except ValueError:
        return True


def test_nl2cypher_inject():
    # 1) 简单单 MATCH：注入成功，两个节点变量都被覆盖。
    out = _inject("MATCH (n {name:'x'})-[r]-(m) RETURN n, r, m LIMIT 50", ["kb-a"])
    check("inject: single match adds n.kb_id predicate", "n.kb_id IN $authorized_kb_ids" in out)
    check("inject: single match adds m.kb_id predicate", "m.kb_id IN $authorized_kb_ids" in out)

    # 2) 已有 WHERE：以 AND 追加而非覆盖。
    out = _inject("MATCH (n) WHERE n.age > 5 RETURN n LIMIT 10", ["kb-a"])
    check("inject: existing WHERE keeps original predicate", "n.age > 5" in out)
    check("inject: existing WHERE appends kb predicate", "n.kb_id IN $authorized_kb_ids" in out)

    # 3) 复杂结构一律拒绝（无法证明完整作用域注入）。
    check("reject: UNION", _raises(_inject, "MATCH (n) RETURN n UNION MATCH (m) RETURN m", ["kb-a"]))
    check("reject: OPTIONAL MATCH", _raises(_inject, "MATCH (n) OPTIONAL MATCH (n)-[r]->(m) RETURN n, m", ["kb-a"]))
    check("reject: multiple MATCH", _raises(_inject, "MATCH (n) MATCH (m) RETURN n, m", ["kb-a"]))
    check("reject: CALL subquery", _raises(_inject, "CALL { MATCH (n) RETURN n } MATCH (x) RETURN x LIMIT 10", ["kb-a"]))
    check("reject: pattern comprehension", _raises(_inject, "MATCH (n) RETURN [(n)-[:R]->(m) | m] AS rels LIMIT 10", ["kb-a"]))

    # 4) 空授权作用域直接拒绝（不生成无 kb 限定的查询）。
    check("reject: empty authorized kb", _raises(_inject, "MATCH (n) RETURN n LIMIT 10", []))


def test_scope_post_filter():
    post = RetrievalOrchestrator._apply_scope_post_filter

    items = [
        {"kb_id": "kb-a", "doc_id": "d1"},
        {"kb_id": "kb-b", "doc_id": "d1"},
        {"doc_id": "d1"},  # 缺少 kb_id
    ]

    # 1) 缺少 kb_id 必须被丢弃（旧逻辑会保留，属于 fail-open）。
    kept = post(items, ["kb-a"])
    check("filter: keeps in-scope item", {"kb_id": "kb-a", "doc_id": "d1"} in kept)
    check("filter: drops out-of-scope kb", all(i.get("kb_id") != "kb-b" for i in kept))
    check("filter: drops item missing kb_id (fail-closed)", all(str(i.get("kb_id") or "") != "" for i in kept))

    # 2) 文档过滤时缺少 doc_id 也必须丢弃。
    doc_items = [
        {"kb_id": "kb-a", "doc_id": "d1"},
        {"kb_id": "kb-a"},          # 缺少 doc_id
        {"kb_id": "kb-a", "doc_id": "d2"},  # 越权文档
    ]
    kept = post(doc_items, ["kb-a"], ["d1"])
    check("filter: doc scope keeps matching doc", kept == [{"kb_id": "kb-a", "doc_id": "d1"}])

    # 3) 授权集合为空时全部丢弃（纵深防御，正常上游由 require_kb_scope 保证非空）。
    check("filter: empty allowed kb drops everything", post(items, []) == [])


def main():
    test_nl2cypher_inject()
    test_scope_post_filter()
    print(f"\nM4-R1 scope guards unit: passed={PASSED} failed={FAILED}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
