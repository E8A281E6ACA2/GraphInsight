#!/usr/bin/env python3
"""
M5-A 活栈只读取证（非 mock）：真实 PostgreSQL / Neo4j / Milvus 读取路径。

与 check_m5a_revision_backfill.py（临时 SQLite + 假证据源）互补：本脚本不打任何桩，
直接调用 backfill 的真实证据源函数，对活栈做只读探测，用于审计修复 #3
"v2 Milvus 缺 content_revision 字段时的真实查询路径" 的非 mock 取证。

铁律：
1. 全程零写入。脚本结束时必须复核 chunk_revisions / admin_jobs 行数与开始时一致，
   不一致直接判失败（防止把取证跑成真实 backfill）。
2. 只读 backfill --dry-run 分支（dry-run 在写库前返回），不跑写入分支。
3. 引擎方言必须是 postgresql，否则判定"这不是活栈"，直接失败退出，
   避免在误连 SQLite 时把隔离库当活栈出具证据。
4. 不打印任何连接串、token、密码；只打印名称、行数、字段名。

运行（需要活栈可用；无活栈时不要跑，不要把它当成通过）：
    cd backend && PYTHONPATH=. python tests/check_m5a_live_stack_readonly.py
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FAILURES: list = []


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def counts() -> tuple:
    from admin.database import engine
    from sqlalchemy import text

    with engine.connect() as conn:
        rev = int(conn.execute(text("SELECT count(*) FROM chunk_revisions")).scalar() or 0)
        jobs = int(conn.execute(text("SELECT count(*) FROM admin_jobs")).scalar() or 0)
    return rev, jobs


def kb_ids() -> list:
    from admin.database import engine
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id FROM knowledge_bases WHERE status <> 'deleting' ORDER BY id")
        ).fetchall()
    return [str(r[0]) for r in rows]


def unregistered_parsed_kbs() -> list:
    """有解析产物 chunks.jsonl、但 knowledge_bases 无登记行的 kb 目录（真实 SCOPE_UNRESOLVED 来源）。"""
    from config import get_settings

    root = Path(str(get_settings().parsed_document_storage_path))
    if not root.exists():
        return []
    registered = set(kb_ids())
    found: list = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name in registered:
            continue
        has_chunks = any(sub.is_dir() and (sub / "chunks.jsonl").exists() for sub in entry.iterdir())
        if has_chunks:
            found.append(entry.name)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="M5-A live-stack read-only evidence (non-mock)")
    parser.add_argument("--kb", default="", help="只探测指定 kb_id（默认遍历 knowledge_bases 全量）")
    args = parser.parse_args()

    print("=" * 60)
    print("GraphInsight M5-A live-stack read-only evidence (non-mock)")
    print("=" * 60)

    import admin.backfill_chunk_revisions as bf
    from admin.database import engine
    from sqlalchemy import text

    step("引擎是真实 PostgreSQL（非隔离 sqlite）", engine.dialect.name == "postgresql", f"dialect={engine.dialect.name}")
    if FAILURES:
        print("✗ 当前连接的不是活栈 PostgreSQL，取证无效，先修环境")
        return 1

    rev_before, jobs_before = counts()
    print(f"[baseline] chunk_revisions rows={rev_before} admin_jobs rows={jobs_before}")

    # ---------------- Milvus：真实 collection 解析与真实 query ----------------
    print("[Milvus] 真实 collection 解析 + 缺字段动态 output_fields")
    resolved = bf._milvus_collection_name()
    step("解析出 collection 名", bool(resolved), f"resolved={resolved!r}")

    client, collection = bf._milvus_client()
    step("_milvus_client 与 _milvus_collection_name 一致", collection == resolved, f"{collection!r} != {resolved!r}")

    names = sorted(client.list_collections())
    print(f"  live collections: {names}")
    step(
        "解析到的 collection 真实存在（旧 bug：配置名 graphinsight_chunks 指向不存在的库）",
        collection in names,
        f"{collection!r} not in {names}",
    )

    exists = client.has_collection(collection)
    fields = bf._milvus_query_output_fields(client, collection) if exists else None
    if exists:
        description = client.describe_collection(collection)
        actual = {f.get("name") for f in (description.get("fields") or []) if isinstance(f, dict)}
        print(f"  actual schema fields: {sorted(actual)}")
        step("探测到的字段集合非空", bool(actual), "")
        step("动态 output_fields 是实际字段的子集（不会请求不存在字段）", fields is not None and set(fields) <= actual, f"fields={fields}")
        if "content_revision" not in actual:
            step("v2 缺 content_revision：动态 output_fields 已剔除", "content_revision" not in (fields or []), f"fields={fields}")
            step("§8.5 判定：milvus_revision_field=False（不伪标）", bf._milvus_has_revision_field(client, collection) is False, "")
        else:
            print("  注：该 collection 已含 content_revision 字段，走带版本字段的真实查询")
        kb_sample = (kb_ids() or ["__no_kb__"])[0]
        from services.scope_contract import milvus_kb_filter

        try:
            page = list(
                client.query(
                    collection_name=collection,
                    filter=milvus_kb_filter([kb_sample]),
                    output_fields=fields or (bf.MILVUS_BASE_OUTPUT_FIELDS + ["content_revision"]),
                    limit=5,
                    offset=0,
                )
                or []
            )
            step("真实 query（动态 output_fields，限定 kb）未报错", True, "")
            print(f"  query rows for sample kb: {len(page)}")
        except Exception as exc:  # noqa: BLE001
            step("真实 query（动态 output_fields，限定 kb）未报错", False, f"{type(exc).__name__}: {str(exc)[:160]}")

        mil = bf._load_milvus_chunks(kb_sample)
        step("_load_milvus_chunks 真实读取未抛异常", isinstance(mil, dict), "")
        print(f"  _load_milvus_chunks sample kb rows: {len(mil)}")
    else:
        print(f"  (活栈没有 collection {collection!r}，Milvus 真实查询取证无法执行)")
        FAILURES.append("活栈缺少 Milvus collection，#3 非 mock 查询取证无法执行")

    # ---------------- 真实证据源读取 + inventory ----------------
    print("[Readonly] 真实证据源与 inventory/前置门")
    targets = [args.kb] if args.kb else kb_ids()
    step("knowledge_bases 有可探测的 KB", bool(targets), "")
    for kb in targets:
        neo = bf._load_neo4j_chunks(kb)
        mil = bf._load_milvus_chunks(kb)
        parsed = bf._load_parsed_chunks(kb)
        existing = bf._load_current_revisions(kb)
        scope = bf._load_kb_scope(kb)
        print(f"  kb={kb} neo4j={len(neo)} milvus={len(mil)} parsed={len(parsed)} revisions={len(existing)} scope={scope}")
        step(f"kb={kb} 真实读取四源未抛异常", True, "")
        inv = bf.build_inventory(kb)
        gate = bf.evaluate_gate(inv)
        print(
            f"    inventory: neo4j={inv.neo4j_count} milvus={inv.milvus_count} parsed={inv.parsed_count} "
            f"rows_skipped_existing={inv.rows_skipped_existing} orphan={len(inv.orphan_revisions)} "
            f"unrecoverable={len(inv.unrecoverable)} blocked={inv.blocked} "
            f"scope_mismatch={len(inv.scope_mismatches)} kb_scope_missing={inv.kb_scope_missing}"
        )
        print(f"    gate: {gate}")
        step(f"kb={kb} build_inventory 作用域冲突为 0（活栈数据真实一致）", not inv.scope_mismatches, str(inv.scope_mismatches[:3]))

    # ---------------- 真实 CLI dry-run ----------------
    print("[CLI] 真实入口 backfill --dry-run（写库前返回，零写入）")
    for kb in (targets or [])[:1]:
        proc = subprocess.run(
            [sys.executable, str(backend_dir / "admin" / "backfill_chunk_revisions.py"), "--kb", kb, "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(backend_dir),
            timeout=600,
        )
        out = proc.stdout + proc.stderr
        print("\n".join(f"    {line}" for line in out.splitlines()[:25]))
        step(f"CLI dry-run 有结构化输出（exit={proc.returncode}）", "[capabilities]" in out and "[inventory]" in out, "")

    # ---------------- 未登记 KB 的真实 fail-closed（审计修复 #4 的活栈腿） ----------------
    print("[Readonly] 未登记 KB 真实 dry-run：SCOPE_UNRESOLVED 必须 exit 2 且零写入")
    unregistered = unregistered_parsed_kbs()
    if not unregistered:
        print("  · 活栈当前没有『解析产物存在但 KB 未登记』的 kb，跳过该腿（未验证，不代表通过）")
    for kb in unregistered[:2]:
        inv = bf.build_inventory(kb)
        print(
            f"  kb={kb} new_chunks={len(inv.new_chunks)} scope_unresolved={len(inv.scope_unresolved)} "
            f"unrecoverable={len(inv.unrecoverable)} kb_scope_missing={inv.kb_scope_missing}"
        )
        step(
            f"kb={kb} 三件套不全判 SCOPE_UNRESOLVED 且不误标 UNRECOVERABLE（内容仍可恢复）",
            len(inv.scope_unresolved) > 0 and len(inv.unrecoverable) == 0 and inv.kb_scope_missing,
            f"scope_unresolved={len(inv.scope_unresolved)} unrecoverable={len(inv.unrecoverable)}",
        )
        proc = subprocess.run(
            [sys.executable, str(backend_dir / "admin" / "backfill_chunk_revisions.py"), "--kb", kb, "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(backend_dir),
            timeout=600,
        )
        out = proc.stdout + proc.stderr
        print("\n".join(f"    {line}" for line in out.splitlines()[:14]))
        step(
            f"kb={kb} 真实 CLI 因 SCOPE_UNRESOLVED 拒绝（exit 2）",
            proc.returncode == 2 and "SCOPE_UNRESOLVED" in out,
            f"exit={proc.returncode}",
        )

    # ---------------- 零写入自证 ----------------
    rev_after, jobs_after = counts()
    print(f"[after] chunk_revisions rows={rev_after} admin_jobs rows={jobs_after}")
    step("零写入自证：chunk_revisions 行数不变", rev_after == rev_before, f"{rev_before} -> {rev_after}")
    step("零写入自证：admin_jobs 行数不变", jobs_after == jobs_before, f"{jobs_before} -> {jobs_after}")

    print("-" * 60)
    if FAILURES:
        for item in FAILURES:
            print(f"  FAILED: {item}")
        print(f"✗ {len(FAILURES)} live evidence checks failed")
        return 1
    print("✓ live-stack read-only evidence collected (non-mock)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
