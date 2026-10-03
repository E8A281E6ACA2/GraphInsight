#!/usr/bin/env python3
"""
M5-A 活栈执行态取证（真实写入分支，dev 授权窗口专用）。

审计前置条件："未完成真实 PostgreSQL、Neo4j、Milvus 执行态验证前，不得宣布 M5-A 通过"。
本脚本在同一台 dev 栈上跑真实 backfill 写入分支，但写入面被严格限制在一个专用合成
kb_id 命名空间内，绝不触碰现有真实知识库：

  * PG：knowledge_bases / chunk_revisions / admin_jobs 仅 WHERE kb_id = <合成 kb>
  * Neo4j：仅 MATCH (c:Chunk {kb_id: <合成 kb>})
  * Milvus：本脚本不写向量（活栈 collection 无 content_revision 字段，backfill 按 §8.5
    自行拒绝写入），只做读回自证"该 kb 在 collection 里 0 行"
  * 文件系统：仅 backend/parsed_documents/<合成 kb>/（该目录已 gitignore）

覆盖腿：
1. 真实作用域冲突 fail-closed（预置 revision 行 project 与 KB 登记不一致 → exit 2、零新增写入）
2. 真实写入分支：revision 1 行落 PG + Neo4j 真实 MERGE content_revision=1 + 读回核对
3. §8.5 真实 Milvus 判定：v2 无 content_revision → vector 保持 pending，向量侧零写入
4. needs_reindex 前置门：真实入队 admin_jobs（targets_hash 64 位）、门 OPEN exit 3、重跑复用不新建
5. 幂等重跑：rows_new=0、行数与 Neo4j 节点数不变
6. 收尾清理：按 kb_id 整块回收，复核回到基线计数（--keep 可保留）

安全：写动作必须显式 --confirm；无论断言成败，finally 都执行清理。
引擎方言非 postgresql 直接退出（防止把隔离库当活栈出具执行态证据）。

运行：
    cd backend && PYTHONPATH=. python tests/check_m5a_live_execution.py --confirm
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _utf8_env() -> dict:
    """子进程必须自带 UTF-8：backfill CLI 在 Windows 默认码下 print 中文会
    UnicodeEncodeError 崩掉，真实退出码会被 1 顶掉（同轮 readonly 脚本实测）。
    脚本自身契约，不靠命令行 `-X utf8`。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env

SYNTHETIC_PREFIX = "m5a-live-"
DOC_ID = "m5alive-doc1"
CHUNK_IDS = ["m5alive-c000", "m5alive-c001", "m5alive-c002"]
TEXTS = {
    "m5alive-c000": "第一块内容：作用域冲突预置行",
    "m5alive-c001": "第二块内容：真实 backfill 落库",
    "m5alive-c002": "第三块内容：中文 UTF-8 校验",
}

FAILURES: list = []


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def note(text: str) -> None:
    print(f"  · {text}")


# ---------------------------------------------------------------------------
# 真实数据源读写 helpers
# ---------------------------------------------------------------------------


def pg_state(kb: str) -> dict:
    from admin.database import engine
    from sqlalchemy import text

    with engine.connect() as conn:
        revisions = conn.execute(
            text(
                "SELECT chunk_id, content_revision, revision_status, graph_status, graph_content_revision, "
                "vector_status, vector_content_revision, content, tenant_id, project_id, doc_id "
                "FROM chunk_revisions WHERE kb_id = :kb ORDER BY chunk_id"
            ),
            {"kb": kb},
        ).fetchall()
        jobs = conn.execute(
            text(
                "SELECT job_type, status, targets_hash, payload FROM admin_jobs "
                "WHERE kb_id = :kb ORDER BY id"
            ),
            {"kb": kb},
        ).fetchall()
        kb_rows = conn.execute(text("SELECT count(*) FROM knowledge_bases WHERE id = :kb"), {"kb": kb}).scalar()
        total_revisions = conn.execute(text("SELECT count(*) FROM chunk_revisions")).scalar()
        total_jobs = conn.execute(text("SELECT count(*) FROM admin_jobs")).scalar()
    cols = ["chunk_id", "content_revision", "revision_status", "graph_status", "graph_content_revision",
            "vector_status", "vector_content_revision", "content", "tenant_id", "project_id", "doc_id"]
    return {
        "revisions": [dict(zip(cols, [str(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], str(r[10])])) for r in revisions],
        "jobs": [dict(job_type=j[0], status=j[1], targets_hash=j[2], payload=j[3]) for j in jobs],
        "kb_registered": int(kb_rows or 0),
        "total_revisions": int(total_revisions or 0),
        "total_jobs": int(total_jobs or 0),
    }


def neo4j_chunks(kb: str) -> list:
    from neo4j import GraphDatabase

    from config import get_settings

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=getattr(settings, "neo4j_connection_timeout_seconds", 5.0),
    )
    try:
        with driver.session(database=getattr(settings, "neo4j_database", None) or None) as session:
            rows = session.run(
                "MATCH (c:Chunk) WHERE c.kb_id = $kb "
                "RETURN c.chunk_id AS chunk_id, c.content_revision AS rev, c.text AS text, "
                "c.tenant_id AS tenant_id, c.project_id AS project_id",
                {"kb": kb},
            ).data()
            return [
                {
                    "chunk_id": str(r["chunk_id"] or ""),
                    "content_revision": r["rev"],
                    "text": str(r["text"] or ""),
                    "tenant_id": str(r["tenant_id"] or ""),
                    "project_id": str(r["project_id"] or ""),
                }
                for r in rows
            ]
    finally:
        driver.close()


def milvus_rows_for_kb(kb: str) -> int:
    import admin.backfill_chunk_revisions as bf
    from services.scope_contract import milvus_kb_filter

    client, collection = bf._milvus_client()
    if not client.has_collection(collection):
        return -1
    fields = bf._milvus_query_output_fields(client, collection) or ["chunk_id"]
    page = client.query(collection_name=collection, filter=milvus_kb_filter([kb]), output_fields=fields, limit=100, offset=0)
    return len(list(page or []))


def parsed_root(kb: str) -> Path:
    from config import get_settings

    return Path(get_settings().parsed_document_storage_path) / kb


def seed_parsed(kb: str) -> None:
    root = parsed_root(kb)
    doc_dir = root / DOC_ID
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "manifest.json").write_text(
        json.dumps({"parser_version": "m5a-live-parser", "content_hash": "m5a-live-source-version-1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    lines = [
        json.dumps({"chunk_id": cid, "doc_id": DOC_ID, "text": TEXTS[cid], "parser_version": "m5a-live-parser"}, ensure_ascii=False)
        for cid in CHUNK_IDS
    ]
    (doc_dir / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def seed_kb_row(kb: str, tenant: str, project: str) -> None:
    from admin.database import engine
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, project_id, name, status, storage_prefix) "
                "VALUES (:id, :tenant, :project, :name, 'active', :prefix) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": kb, "tenant": tenant, "project": project, "name": "M5-A live evidence KB", "prefix": kb},
        )


def seed_conflicting_revision(kb: str, tenant: str, bad_project: str) -> None:
    import hashlib

    from admin.database import engine
    from sqlalchemy import text

    cid = CHUNK_IDS[0]
    content = TEXTS[cid]
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
                "source_content, source_content_hash, content, content_hash, content_revision, "
                "revision_status, graph_status, vector_status, revision_source, reason, trace_id) "
                "VALUES (:kb, :tenant, :bad_project, :doc, :chunk, :content, :h, :content, :h, 1, "
                "'current', 'pending', 'pending', 'system_reparse', 'm5a_live_fixture', 'm5a-live')"
            ),
            {"kb": kb, "tenant": tenant, "bad_project": bad_project, "doc": DOC_ID, "chunk": cid, "content": content, "h": digest},
        )


def fix_revision_scope(kb: str, project: str) -> None:
    from admin.database import engine
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE chunk_revisions SET project_id = :project, graph_status = 'stale', vector_status = 'pending' "
                "WHERE kb_id = :kb AND chunk_id = :chunk AND revision_status = 'current'"
            ),
            {"project": project, "kb": kb, "chunk": CHUNK_IDS[0]},
        )


def cleanup(kb: str) -> dict:
    from admin.database import engine
    from neo4j import GraphDatabase
    from sqlalchemy import text

    from config import get_settings

    report = {}
    settings = get_settings()
    with engine.begin() as conn:
        report["chunk_revisions"] = conn.execute(text("DELETE FROM chunk_revisions WHERE kb_id = :kb"), {"kb": kb}).rowcount
        report["admin_jobs"] = conn.execute(text("DELETE FROM admin_jobs WHERE kb_id = :kb"), {"kb": kb}).rowcount
        report["knowledge_bases"] = conn.execute(text("DELETE FROM knowledge_bases WHERE id = :kb"), {"kb": kb}).rowcount
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=getattr(settings, "neo4j_connection_timeout_seconds", 5.0),
    )
    try:
        with driver.session(database=getattr(settings, "neo4j_database", None) or None) as session:
            result = session.run(
                "MATCH (c:Chunk) WHERE c.kb_id = $kb DETACH DELETE c RETURN count(c) AS deleted", {"kb": kb}
            ).single()
            report["neo4j_chunks"] = int(result["deleted"]) if result else 0
    finally:
        driver.close()
    root = parsed_root(kb)
    if root.exists():
        shutil.rmtree(root)
        report["parsed_dir"] = "removed"
    else:
        report["parsed_dir"] = "absent"
    return report


def run_cli(kb: str, extra: list, expect_code: int) -> tuple:
    proc = subprocess.run(
        [sys.executable, str(backend_dir / "admin" / "backfill_chunk_revisions.py"), "--kb", kb] + extra,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(backend_dir),
        env=_utf8_env(),
        timeout=900,
    )
    lines = [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip() and not ln.startswith("Received notification from DBMS")]
    if proc.returncode != expect_code:
        # 失败给完整 exit code 与 stderr，不截断，便于区分"真实 fail-closed"与"子进程自己崩了"
        print(f"    !! backfill CLI exit={proc.returncode}，期望 exit={expect_code}")
        for ln in (proc.stderr.splitlines() or ["<stderr 为空>"]):
            print(f"    [stderr] {ln}")
    print("\n".join(f"    {ln}" for ln in lines[:18]))
    return proc.returncode, "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="M5-A live execution-state evidence (writes confined to one synthetic kb)")
    parser.add_argument("--kb", default="m5a-live-20261001")
    parser.add_argument("--confirm", action="store_true", help="必须显式确认才会写活栈")
    parser.add_argument("--keep", action="store_true", help="取证后保留数据（默认清理）")
    args = parser.parse_args()

    kb = args.kb.strip().lower()
    print("=" * 60)
    print("GraphInsight M5-A live execution-state evidence")
    print("=" * 60)

    if not kb.startswith(SYNTHETIC_PREFIX):
        print(f"✗ 拒绝执行：kb_id 必须以 {SYNTHETIC_PREFIX} 开头，避免写入真实知识库（got {kb!r}）")
        return 2
    if not args.confirm:
        print("✗ 未带 --confirm：本脚本会写真实 PostgreSQL/Neo4j，拒绝执行。计划：")
        for line in (
            f"  1. PG 插入 knowledge_bases 行 id={kb}（tenant=default, project=default）",
            f"  2. 文件系统写解析产物 backend/parsed_documents/{kb}/{DOC_ID}/（chunks.jsonl 3 块）",
            f"  3. PG 预置 1 条 project 错误的 current revision 行 → 跑 backfill 期望 SCOPE_MISMATCH exit 2 零写入",
            f"  4. 修正该行 scope 后跑真实 backfill（非 dry-run）：PG 落 revision 行 + Neo4j MERGE content_revision=1",
            f"  5. 读回 PG/Neo4j/Milvus 核对，再跑幂等重跑与 admin_jobs 入队断言",
            f"  6. 清理：DELETE WHERE kb_id={kb} + DETACH DELETE Chunk WHERE kb_id={kb} + 删解析产物目录",
        ):
            print(line)
        return 2

    from admin.database import engine
    from config import get_settings

    if engine.dialect.name != "postgresql":
        print(f"✗ 引擎方言是 {engine.dialect.name}，不是活栈 PostgreSQL，执行态取证无效，终止")
        return 1

    settings = get_settings()
    tenant = "default"
    project = "default"
    bad_project = "m5a-wrong-project"

    note(f"dialect={engine.dialect.name} parsed_root={settings.parsed_document_storage_path}")
    baseline = pg_state(kb)
    if baseline["revisions"] or baseline["jobs"] or baseline["kb_registered"]:
        print(f"✗ 合成 kb 已有残留数据（revisions={len(baseline['revisions'])} jobs={len(baseline['jobs'])} "
              f"kb_rows={baseline['kb_registered']}），先手工清理再取证")
        return 1
    if parsed_root(kb).exists():
        print("✗ 合成 kb 的解析产物目录已存在，先手工清理再取证")
        return 1
    print(f"[baseline] chunk_revisions total={baseline['total_revisions']} admin_jobs total={baseline['total_jobs']} "
          f"neo4j_chunks={len(neo4j_chunks(kb))} milvus_rows={milvus_rows_for_kb(kb)}")

    seeded = False
    exit_code = 1
    try:
        # ---- 播种（只写合成 kb 命名空间） ----
        print("[P1] 播种：knowledge_bases 行 + 解析产物 + 1 条作用域错误的 current 行")
        seed_kb_row(kb, tenant, project)
        seed_parsed(kb)
        seed_conflicting_revision(kb, tenant, bad_project)
        seeded = True
        after_seed = pg_state(kb)
        step("播种后仅 1 条 revision 行、KB 已登记", len(after_seed["revisions"]) == 1 and after_seed["kb_registered"] == 1, str(len(after_seed["revisions"])))

        # ---- 真实作用域冲突 fail-closed（审计修复 #4 的活栈腿） ----
        print("[P2] 真实作用域冲突：backfill 必须 SCOPE_MISMATCH 拒绝且零新增写入")
        code, out = run_cli(kb, ["--dry-run"], expect_code=2)
        step("SCOPE_MISMATCH 拒绝（exit 2）", code == 2, f"exit={code}")
        step("冲突明细给出 expected/actual",
             f"revision.project_id" in out and f"expected={project}" in out and f"actual={bad_project}" in out, out[-300:])
        mid = pg_state(kb)
        step("拒绝路径零写入（仍是 1 行、0 job）", len(mid["revisions"]) == 1 and len(mid["jobs"]) == 0, f"rows={len(mid['revisions'])} jobs={len(mid['jobs'])}")
        step("拒绝路径未触碰 Neo4j", len(neo4j_chunks(kb)) == 0, str(neo4j_chunks(kb)))

        # ---- 修正作用域后跑真实写入分支 ----
        print("[P3] 修正 scope 后跑真实写入分支（非 dry-run）")
        fix_revision_scope(kb, project)
        code, out = run_cli(kb, [], expect_code=3)
        rows = pg_state(kb)["revisions"]
        step("3 个 chunk 全部落 revision 行", len(rows) == 3, f"rows={len(rows)}")
        step("新行 content_revision=1 且 revision_status=current",
             all(r["content_revision"] == 1 and r["revision_status"] == "current" for r in rows), str([(r["chunk_id"], r["content_revision"]) for r in rows]))
        step("rows_new=2（已有行不重复插入，幂等口径）", "rows_new=2" in out and "insert_conflicts_skipped=0" in out, out[-400:])
        step("rows_skipped_existing=1（决策时已存在的行）", "rows_skipped_existing=1" in out, out[-400:])
        graph_indexed = [r for r in rows if r["graph_status"] == "indexed"]
        step("graph 投影真实落库：indexed + graph_content_revision=1",
             len(graph_indexed) == 2 and all(r["graph_content_revision"] == 1 for r in graph_indexed),
             str([(r["chunk_id"], r["graph_status"], r["graph_content_revision"]) for r in rows]))

        # ---- 真实 Neo4j 读回 ----
        print("[P4] Neo4j 真实读回（非 mock）")
        nodes = neo4j_chunks(kb)
        step("Neo4j 实际写入 2 个 Chunk 节点", len(nodes) == 2, f"nodes={[n['chunk_id'] for n in nodes]}")
        step("Neo4j 节点 content_revision=1", all(n["content_revision"] == 1 for n in nodes), str(nodes))
        step("Neo4j 节点作用域与 KB 登记一致",
             all(n["tenant_id"] == tenant and n["project_id"] == project for n in nodes), str(nodes))
        step("Neo4j 节点文本与解析产物逐块一致（UTF-8）",
             all(n["text"] == TEXTS[n["chunk_id"]] for n in nodes), str([(n["chunk_id"], n["text"]) for n in nodes]))

        # ---- §8.5 真实 Milvus 判定 + 向量侧零写入 ----
        print("[P5] Milvus 真实侧：v2 无 content_revision → pending，且向量侧零写入")
        step("报告给出 MILVUS_REVISION_FIELD_ABSENT", "MILVUS_REVISION_FIELD_ABSENT" in out, out[-300:])
        vec = [r for r in rows if r["chunk_id"] != CHUNK_IDS[0]]
        step("新行 vector_status=pending 且 vector_content_revision 为 NULL",
             all(r["vector_status"] == "pending" and r["vector_content_revision"] is None for r in vec),
             str([(r["chunk_id"], r["vector_status"], r["vector_content_revision"]) for r in vec]))
        step("Milvus collection 内该合成 kb 真实 0 行（未被写入）", milvus_rows_for_kb(kb) == 0, f"rows={milvus_rows_for_kb(kb)}")

        # ---- needs_reindex 前置门 + 真实入队 ----
        print("[P6] 前置门与 admin_jobs 真实入队")
        step("前置门 OPEN（exit 3）", code == 3, f"exit={code}")
        jobs = pg_state(kb)["jobs"]
        step("reindex_chunks job 已入队", len(jobs) >= 1 and all(j["job_type"] == "reindex_chunks" for j in jobs), str(jobs))
        hashes = [str(j["targets_hash"] or "") for j in jobs]
        step("targets_hash 为 64 位十六进制", all(len(h) == 64 for h in hashes), str(hashes))
        step("payload 含真实 reindex targets（chunk_id + target_revision）",
             all(("m5alive-c" in str(j["payload"])) and ("target_revision" in str(j["payload"])) for j in jobs),
             str(jobs)[:400])

        print("[P7] 幂等重跑：不新建行、不新建 job")
        before_rerun = pg_state(kb)
        code2, out2 = run_cli(kb, [], expect_code=3)
        after_rerun = pg_state(kb)
        step("幂等重跑按契约退出（exit 3，前置门仍 OPEN）", code2 == 3, f"exit={code2}")
        step("幂等重跑：rows_new=0 且 insert_conflicts_skipped=0（本轮无待插 chunk）",
             "rows_new=0" in out2 and "insert_conflicts_skipped=0" in out2, out2[-400:])
        step("重跑后 revision 行数不变", len(after_rerun["revisions"]) == len(before_rerun["revisions"]), str(len(after_rerun["revisions"])))
        step("重跑复用 targets_hash 不新建 job",
             len(after_rerun["jobs"]) == len(before_rerun["jobs"]) and "jobs_reused=" in out2,
             f"{len(before_rerun['jobs'])} -> {len(after_rerun['jobs'])}")
        step("重跑后 Neo4j 节点数不变", len(neo4j_chunks(kb)) == 2, str(neo4j_chunks(kb)))

        # ---- 全库无副作用：其它 KB 的行不受影响 ----
        print("[P8] 副作用面核对")
        total_now = pg_state(kb)
        step("chunk_revisions 全表行数 = 基线 + 3",
             total_now["total_revisions"] == baseline["total_revisions"] + 3,
             f"{baseline['total_revisions']} -> {total_now['total_revisions']}")
        step("admin_jobs 全表增量 = 本次入队数",
             total_now["total_jobs"] - baseline["total_jobs"] == len(total_now["jobs"]),
             f"total_delta={total_now['total_jobs'] - baseline['total_jobs']} scoped={len(total_now['jobs'])}")

        exit_code = 1 if FAILURES else 0
    finally:
        if args.keep:
            note("--keep：保留合成数据（后续手工清理命令见报告）")
        else:
            print("[P9] 清理（按 kb_id 整块回收）")
            report = cleanup(kb)
            note(f"deleted: {report}")
            final = pg_state(kb)
            step("清理后该 kb 无 revision 行", len(final["revisions"]) == 0, str(len(final["revisions"])))
            step("清理后该 kb 无 job", len(final["jobs"]) == 0, str(len(final["jobs"])))
            step("清理后 KB 登记行已删除", final["kb_registered"] == 0, str(final["kb_registered"]))
            step("清理后 Neo4j 无该 kb 节点", len(neo4j_chunks(kb)) == 0, str(neo4j_chunks(kb)))
            step("清理后解析产物目录不存在", not parsed_root(kb).exists(), str(parsed_root(kb)))
            step("全表回到基线行数",
                 final["total_revisions"] == baseline["total_revisions"] and final["total_jobs"] == baseline["total_jobs"],
                 f"rev {baseline['total_revisions']}->{final['total_revisions']} jobs {baseline['total_jobs']}->{final['total_jobs']}")

    print("-" * 60)
    if FAILURES:
        for item in FAILURES:
            print(f"  FAILED: {item}")
        print(f"✗ {len(FAILURES)} live execution-state checks failed")
        return 1
    if exit_code:
        return exit_code
    print("✓ live execution-state evidence collected (real PG + Neo4j + Milvus)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
