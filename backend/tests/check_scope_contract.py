"""
知识库作用域契约负向测试（M1 验收门）

覆盖契约 §2.1/§2.4/§2.9/§2.2 的强制规则：
- 缺少 kb 作用域 -> KB_SCOPE_REQUIRED
- header/query/body 作用域不一致 -> KB_CROSS_SCOPE
- 作用域格式非法 -> SCOPE_INVALID
- 请求范围与授权交集为空 -> KB_ACCESS_DENIED
- 同名实体不同 kb_id 不得产生相同 entity_key
- 无作用域的 Milvus filter / Neo4j 查询参数直接拒绝

运行：backend/.venv/bin/python backend/tests/check_scope_contract.py
（纯单元测试，不依赖数据库/Neo4j/Milvus）
"""
from __future__ import annotations

import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from core.exceptions import ErrorCode, KnowledgeScopeError  # noqa: E402
from services.scope_contract import (  # noqa: E402
    entity_key,
    milvus_kb_filter,
    relation_key,
    require_kb_scope,
    resolve_search_target,
    SearchTarget,
)

PASS: list = []
FAIL: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  ✗ {name} {detail}")


def expect_error(name: str, fn, code: str) -> None:
    try:
        fn()
    except KnowledgeScopeError as exc:
        check(name, exc.error_code == code, f"(got {exc.error_code})")
        return
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"(unexpected {type(exc).__name__}: {exc})")
        return
    check(name, False, "(no error raised)")


def test_missing_scope() -> None:
    print("[1] 缺少 kb 作用域 -> KB_SCOPE_REQUIRED")
    expect_error("仅 header 无 kb", lambda: resolve_search_target(header={"tenant_id": "tenant-a"}), ErrorCode.KB_SCOPE_REQUIRED)
    expect_error("完全为空", lambda: resolve_search_target(), ErrorCode.KB_SCOPE_REQUIRED)
    expect_error("仅有 project", lambda: resolve_search_target(query={"project_id": "project-a"}), ErrorCode.KB_SCOPE_REQUIRED)


def test_cross_scope() -> None:
    print("[2] 作用域不一致 -> KB_CROSS_SCOPE")
    expect_error(
        "header 与 query 的 kb_id 不同",
        lambda: resolve_search_target(header={"kb_id": "kb-a"}, query={"kb_id": "kb-b"}),
        ErrorCode.KB_CROSS_SCOPE,
    )
    expect_error(
        "header kb_id 与 body kb_ids 不同",
        lambda: resolve_search_target(header={"kb_id": "kb-a"}, body={"kb_ids": ["kb-b"]}),
        ErrorCode.KB_CROSS_SCOPE,
    )
    target = resolve_search_target(header={"kb_id": "KB-A"}, query={"kb_id": "kb-a"})
    check("相同值大小写归一后一致不报错", target.kb_ids == ["kb-a"], f"(got {target.kb_ids})")


def test_invalid_format() -> None:
    print("[3] 作用域格式非法 -> SCOPE_INVALID")
    ok = resolve_search_target(header={"kb_id": "KB-A"})
    check("大写自动归一为小写（先归一再校验）", ok.kb_ids == ["kb-a"], f"(got {ok.kb_ids})")
    expect_error("连字符开头", lambda: resolve_search_target(header={"kb_id": "-abc"}), ErrorCode.SCOPE_INVALID)
    expect_error("单字符过短", lambda: resolve_search_target(header={"kb_id": "a"}), ErrorCode.SCOPE_INVALID)
    expect_error("含路径逃逸", lambda: resolve_search_target(header={"kb_id": "../etc"}), ErrorCode.SCOPE_INVALID)
    expect_error("超长", lambda: resolve_search_target(header={"kb_id": "a" * 101}), ErrorCode.SCOPE_INVALID)
    expect_error("特殊字符", lambda: resolve_search_target(header={"kb_id": "kb a"}), ErrorCode.SCOPE_INVALID)
    ok = resolve_search_target(header={"kb_id": "kb_a-001"})
    check("合法格式（小写字母数字_-）", ok.kb_ids == ["kb_a-001"])


def test_effective_intersection() -> None:
    print("[4] 授权交集规则")
    target = SearchTarget(kb_ids=["kb-a", "kb-b"])
    effective = target.effective_kb_ids(["kb-b", "kb-c"])
    check("交集正确", effective == ["kb-b"], f"(got {effective})")
    expect_error(
        "交集为空 -> KB_ACCESS_DENIED",
        lambda: SearchTarget(kb_ids=["kb-x"]).effective_kb_ids(["kb-a"]),
        ErrorCode.KB_ACCESS_DENIED,
    )


def test_entity_relation_keys() -> None:
    print("[5] 同名实体不同 kb 不合并")
    key_a = entity_key("kb-a", "品种A", "品种")
    key_b = entity_key("kb-b", "品种A", "品种")
    check("不同 kb 同名实体 key 不同", key_a != key_b)
    check("同 kb 同名实体 key 稳定", entity_key("kb-a", "品种A", "品种") == key_a)
    check("大小写归一", entity_key("kb-a", "品种a", "品种") == key_a)
    rel_a = relation_key("kb-a", key_a, "表现出", key_b, "chunk-1")
    rel_b = relation_key("kb-b", key_a, "表现出", key_b, "chunk-1")
    check("不同 kb 同键关系不同", rel_a != rel_b)


def test_milvus_and_require() -> None:
    print("[6] Milvus filter / 无作用域拒绝")
    expr = milvus_kb_filter(["kb-a", "kb-b"])
    check("filter 表达式正确", expr == 'kb_id in ["kb-a", "kb-b"]', f"(got {expr})")
    expect_error("空 kb_ids filter 拒绝", lambda: milvus_kb_filter([]), ErrorCode.KB_SCOPE_REQUIRED)
    expect_error("require_kb_scope 空拒绝", lambda: require_kb_scope(None), ErrorCode.KB_SCOPE_REQUIRED)
    check("require_kb_scope 归一输出", require_kb_scope(["KB-A"]) == ["kb-a"])


def main() -> int:
    print("=" * 60)
    print("GraphInsight KB scope contract negative tests")
    print("=" * 60)
    test_missing_scope()
    test_cross_scope()
    test_invalid_format()
    test_effective_intersection()
    test_entity_relation_keys()
    test_milvus_and_require()
    print("-" * 60)
    print(f"passed={len(PASS)} failed={len(FAIL)}")
    if FAIL:
        for item in FAIL:
            print(f"  FAILED: {item}")
        return 1
    print("✓ all scope contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
