#!/usr/bin/env python3
"""
M5-B0 reindex_chunks worker 验收测试（临时 SQLite 隔离 + UTF-8 子进程捕获）

覆盖用户开工令 B0-1 的五个必证点：
1. job type 接线：reindex_chunks 进入 Python worker 可执行集合且属知识数据类（scope 必填）
2. scope 校验：payload 作用域缺失 / 与 knowledge_bases 登记冲突 / current 行作用域冲突 → 零写入 fail-closed
3. target revision 复核 + 过期任务保护：target != current、无 current 行、写索引后 current 被移动
4. 失败重试语义：targets/作用域非法与 collection 缺显式字段 → ValidationException（不重试）；
   投影写入失败 → RuntimeError（按 max_retries 退避重试）
5. 不复用旧 reindex：execute_job 分发到两个不同分支；Milvus v2 无 content_revision 字段时拒写向量

真实代码路径覆盖：作用域校验、四道复核、CAS 回写、文档级聚合（§6.2）、
`vector_store._existing_milvus_fields` 整行替换前读回合并、`upsert_chunks` 的 §8.5 边界守卫、
backfill 入队 → worker 消费 → 复跑 inventory 得到 CLOSED 的闭环。
Neo4j/Milvus 网络写入用假投影库替代（本轮禁止触碰共享 dev 活栈）。

隔离与 UTF-8 铁律同 check_m5a_revision_backfill.py：
GRAPHINSIGHT_BACKEND_ENV_FILE 指向含 ADMIN_DATABASE_URL=sqlite 的临时 env 文件；
每个场景独立库；子进程传 PYTHONUTF8/PYTHONIOENCODING 并断言真实 returncode；
不使用手工 -X utf8 参数。

运行：python backend/tests/check_b0_reindex_chunks.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

backend_dir = Path(__file__).parent.parent
FAILURES: list = []
DRIVER = str(Path("tests") / "b0_reindex_chunks_driver.py")
KB = "kb-b0"
DOC = "doc-1"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _utf8_env(base: dict) -> dict:
    env = base.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.env_file = tmp / "b0_test.env"

    def use_db(self, name: str) -> str:
        url = f"sqlite:///{(self.tmp / name).as_posix()}"
        self.env_file.write_text(f"ADMIN_DATABASE_URL={url}\n", encoding="utf-8")
        return url

    def _base_env(self, extra_env: dict = None) -> dict:
        env = _utf8_env(os.environ)
        env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(self.env_file)
        env["PYTHONPATH"] = str(backend_dir)
        if extra_env:
            env.update(extra_env)
        return env

    def _spawn(self, cmd: list) -> tuple:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.tmp),
            env=self._base_env(),
            timeout=300,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def run(self, script: str, args: list = None) -> tuple:
        return self._spawn([sys.executable, str(backend_dir / script)] + (args or []))

    def run_python_code(self, code: str) -> tuple:
        return self._spawn([sys.executable, "-c", code])

    def guard_sqlite(self) -> None:
        code, out = self.run_python_code(
            "from admin.database import engine; print('DIALECT', engine.dialect.name)"
        )
        step("引擎隔离守卫（必须 sqlite）", code == 0 and "DIALECT sqlite" in out, f"exit={code} " + out[-300:])


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def parse_marker(out: str, marker: str, default=None):
    for line in out.splitlines():
        if line.startswith(marker):
            try:
                return json.loads(line[len(marker):])
            except json.JSONDecodeError:
                return default
    return default


def marker_lines(out: str, marker: str) -> list:
    return [line[len(marker):] for line in out.splitlines() if line.startswith(marker)]


def obj_marker(out: str, marker: str) -> dict:
    """标记缺失/形状不对必须显式判红，不能让 == {} 类断言假绿。"""
    value = parse_marker(out, marker, None)
    if not isinstance(value, dict):
        step(f"标记 {marker} 缺失或非对象", False, f"got={str(value)[:160]}")
        return {}
    return value


def arr_marker(out: str, marker: str) -> list:
    value = parse_marker(out, marker, None)
    if not isinstance(value, list):
        step(f"标记 {marker} 缺失或非数组", False, f"got={str(value)[:160]}")
        return []
    return value


def rev_map(out: str) -> dict:
    rows = arr_marker(out, "__REVISIONS__")
    return {
        str(row[1]): {
            "kb_id": row[0],
            "content_revision": row[2],
            "graph_status": row[3],
            "graph_content_revision": row[4],
            "vector_status": row[5],
            "vector_content_revision": row[6],
        }
        for row in rows
    }


def doc_map(out: str) -> dict:
    return {str(row[0]): {"graph_status": row[1], "vector_status": row[2]} for row in arr_marker(out, "__DOCUMENTS__")}


def result_of(out: str) -> dict:
    return obj_marker(out, "__RESULT__")


def counts_of(out: str) -> dict:
    """成功路径取 result.counts；抛错路径取异常 details 里的 __COUNTS__。"""
    res = parse_marker(out, "__RESULT__", None)
    if isinstance(res, dict):
        return res.get("counts", {})
    return obj_marker(out, "__COUNTS__")


def exception_of(out: str) -> dict:
    return obj_marker(out, "__EXCEPTION__")


def calls_of(out: str) -> dict:
    value = obj_marker(out, "__CALLS__")
    return {"graph": sorted(value.get("graph", [])), "vector": sorted(value.get("vector", []))}


def bootstrap_admin_jobs(h: Harness) -> tuple:
    return h.run_python_code(
        "from admin.database import Base, engine;"
        "from admin.models import AdminJob;"
        "Base.metadata.create_all(bind=engine, tables=[AdminJob.__table__]);"
        "engine.dispose(); print('bootstrap ok')"
    )


def prep_db(h: Harness, name: str) -> None:
    """独立 SQLite 库 + 三件套建表（chunk_revisions / admin_jobs / targets_hash）。

    每场景独立库：backfill 的 universe 包含该 kb 全部 current 行，共库会让前一场景的
    行被后续场景判为孤儿/needs_reindex，污染断言（M5-A 套件同口径）。
    """
    h.use_db(name)
    code, out = h.run(str(Path("admin") / "migrate_chunk_revisions.py"), ["--action", "migrate"])
    step(f"{name}：迁移 chunk_revisions", code == 0, f"exit={code} " + out[-300:])
    code, out = bootstrap_admin_jobs(h)
    step(f"{name}：引导 admin_jobs", code == 0 and "bootstrap ok" in out, f"exit={code} " + out[-300:])
    code, out = h.run(str(Path("admin") / "migrate_jobs_targets_hash.py"), ["--action", "migrate"])
    step(f"{name}：迁移 targets_hash", code == 0, f"exit={code} " + out[-300:])


def scenario(h: Harness, db: str, name: str) -> str:
    prep_db(h, db)
    code, out = h.run(DRIVER, ["--scenario", name])
    step(f"{name}：driver 子进程 exit 0（UTF-8 捕获，无 -X utf8）", code == 0, f"exit={code}\n{out[-1500:]}")
    return out


# ---------------------------------------------------------------------------
# S. worker 接线对账
# ---------------------------------------------------------------------------


def section_s(h: Harness) -> None:
    print("[S] reindex_chunks 接线（job type 白名单 / 分发 / 旧 reindex 未复用）")
    h.use_db("wiring.db")
    code, out = h.run_python_code(
        "from admin.services.job_service import SUPPORTED_JOB_TYPES, RUNNABLE_JOB_TYPES, KB_SCOPED_JOB_TYPES;"
        "import services.job_runtime as rt;"
        "import inspect;"
        "src=inspect.getsource(rt.execute_job);"
        "print('SUPPORTED', sorted(SUPPORTED_JOB_TYPES));"
        "print('RUNNABLE', sorted(RUNNABLE_JOB_TYPES));"
        "print('KBSCOPED', sorted(KB_SCOPED_JOB_TYPES));"
        "print('BRANCH', 'reindex_chunks' in src, 'execute_reindex_chunks' in src);"
        "print('HAS_EXEC', hasattr(rt,'execute_reindex_chunks'))"
    )
    step("job_type 进入 SUPPORTED/RUNNABLE 集合", code == 0 and "reindex_chunks" in out, f"exit={code} " + out[-400:])
    step(
        "reindex_chunks 属知识数据类（创建时必须冻结 kb scope）",
        code == 0 and "KBSCOPED ['build_graph', 'clear_kb', 'reindex_chunks']" in out,
        f"exit={code} " + out[-400:],
    )
    step("execute_job 有 reindex_chunks 独立分支", code == 0 and "BRANCH True True" in out, f"exit={code} " + out[-400:])
    step("worker 执行函数存在", code == 0 and "HAS_EXEC True" in out, f"exit={code} " + out[-400:])
    code, out = h.run_python_code(
        "import services.runtime_config as rc, services.chunk_projection_reindex as w;"
        "print('CAPS', hasattr(rc,'get_projection_capabilities'));"
        "caps=rc.get_projection_capabilities();"
        "print('CAPS_SHAPE', sorted(caps) == ['graph','vector'], all(isinstance(v, bool) for v in caps.values()))"
    )
    step("get_projection_capabilities 存在且返回 graph/vector 布尔", code == 0 and "CAPS True" in out and "CAPS_SHAPE True" in out, f"exit={code} " + out[-300:])

    out = scenario(h, "dispatch.db", "dispatch_separation")
    dispatch = obj_marker(out, "__DISPATCH__")
    step("旧 reindex 与 reindex_chunks 各走各的分支（未复用）", dispatch == {"chunk": 1, "fulltext": 2}, f"got={dispatch}")
    step("未知 job_type 被拒绝", marker_lines(out, "__UNKNOWN__") == ["ValidationException"], str(marker_lines(out, "__UNKNOWN__")))


# ---------------------------------------------------------------------------
# A. 正常路径与幂等
# ---------------------------------------------------------------------------


def section_a(h: Harness) -> None:
    print("[A] 正常消费、内容源、幂等重跑")
    out = scenario(h, "happy.db", "happy")
    res = result_of(out)
    step("返回 reindex_chunks 结果 dict（worker 判 succeeded）", res.get("job_type") == "reindex_chunks" and res.get("execution_status") == "completed", f"res={str(res)[:400]}")
    step("作用域来自 payload 冻结值", res.get("kb_id") == KB and res.get("tenant_id") == "t1" and res.get("project_id") == "p1", f"res={str(res)[:300]}")
    counts = counts_of(out)
    step(
        "两个 target 全部两侧 indexed 且版本=target",
        counts.get("targets") == 2 and counts.get("written") == 2 and counts.get("graph_indexed") == 2 and counts.get("vector_indexed") == 2,
        f"counts={counts}",
    )
    step("失败计数为零", counts.get("failed_chunks") == 0 and counts.get("outdated") == 0 and counts.get("scope_mismatch") == 0, f"counts={counts}")
    step("全收敛时 message 才写「投影重建完成」", res.get("message") == "投影重建完成", f"message={res.get('message')}")
    revs = rev_map(out)
    step(
        "current 行回写 indexed + *_content_revision=target（非硬编码）",
        all(
            revs.get(chunk, {}).get("graph_status") == "indexed"
            and revs.get(chunk, {}).get("graph_content_revision") == version
            and revs.get(chunk, {}).get("vector_status") == "indexed"
            and revs.get(chunk, {}).get("vector_content_revision") == version
            for chunk, version in (("c-1", 1), ("c-2", 3))
        ),
        f"revs={revs}",
    )
    neo = obj_marker(out, "__NEO4J__")
    step("Neo4j 投影内容 = current 行 content（唯一内容源）", neo.get(f"{KB}|c-1", {}).get("text") == "第一段", f"neo={str(neo)[:300]}")
    step("Neo4j 投影版本 = target_revision（c-2 写 3 不写 1）", neo.get(f"{KB}|c-2", {}).get("content_revision") == 3, f"neo={str(neo)[:300]}")
    step("Milvus 投影带显式版本与作用域", obj_marker(out, "__MILVUS__").get(f"{KB}|c-1", {}).get("content_revision") == 1, out[-200:])
    step("文档级聚合落到 indexed/indexed", doc_map(out).get(DOC) == {"graph_status": "indexed", "vector_status": "indexed"}, f"docs={doc_map(out)}")
    step(
        "如实声明实体/关系抽取缺口（不伪装完整重建）",
        any("实体与关系抽取" in str(item) for item in res.get("notes", [])),
        f"notes={res.get('notes')}",
    )

    out = scenario(h, "idempotent.db", "idempotent_rerun")
    step("幂等重跑不写索引", calls_of(out) == {"graph": [], "vector": []}, f"calls={calls_of(out)}")
    step("幂等重跑不虚报 indexed 增量", counts_of(out).get("graph_indexed") == 0, f"counts={counts_of(out)}")
    step(
        "已收敛行的投影状态保持不变",
        rev_map(out).get("c-1", {}).get("graph_content_revision") == 1 and rev_map(out).get("c-1", {}).get("vector_status") == "indexed",
        f"revs={rev_map(out)}",
    )


# ---------------------------------------------------------------------------
# B. target revision 复核与过期任务保护（§8.2 步骤1 / §8.3）
# ---------------------------------------------------------------------------


def section_b(h: Harness) -> None:
    print("[B] 过期任务保护与四道复核")
    out = scenario(h, "outdated.db", "outdated_revision")
    step("target != current → OUTDATED_SKIPPED 且零写入", calls_of(out) == {"graph": [], "vector": []}, f"calls={calls_of(out)}")
    step("execution_status=no_write", result_of(out).get("execution_status") == "no_write", f"res={str(result_of(out))[:200]}")
    outdated = result_of(out).get("outdated_skipped", [])
    step("过期原因=revision_moved 并记录 current_revision", outdated and outdated[0].get("reason") == "revision_moved" and outdated[0].get("current_revision") == 2, f"outdated={outdated}")
    step("DB 投影状态未被触碰", rev_map(out).get("c-1", {}).get("graph_status") == "pending", f"revs={rev_map(out)}")

    out = scenario(h, "no_current.db", "no_current_row")
    outdated = result_of(out).get("outdated_skipped", [])
    step("无 current 行 → no_current_revision 且零写入", calls_of(out) == {"graph": [], "vector": []} and outdated[0].get("reason") == "no_current_revision", f"res={str(outdated)}")

    out = scenario(h, "cross_kb.db", "cross_kb_isolation")
    step("chunk_id 跨 KB 不串写（本 KB 无行 → 过期）", calls_of(out) == {"graph": [], "vector": []}, f"calls={calls_of(out)}")
    step("另一 KB 的行未被改动", rev_map(out).get("c-shared", {}).get("graph_status") == "pending", f"revs={rev_map(out)}")

    out = scenario(h, "moved.db", "current_moved")
    counts = counts_of(out)
    step("复核3 发现 current 已移动 → current_moved", counts.get("current_moved") == 1, f"counts={counts}")
    step("索引侧已写入但状态回写不虚报 indexed", counts.get("graph_indexed") == 0 and counts.get("vector_indexed") == 0, f"counts={counts}")
    revs = rev_map(out)
    step("current 已移动时 DB 投影状态与版本保持未收敛", revs.get("c-1", {}).get("graph_status") == "pending" and revs.get("c-1", {}).get("graph_content_revision") is None, f"revs={revs}")
    step("写索引动作确实发生过（旧任务保护只拦状态回写）", calls_of(out)["graph"] == ["c-1"], f"calls={calls_of(out)}")
    step("存在过期目标时 message 不宣布完成", "未完全收敛" in str(result_of(out).get("message")), f"message={result_of(out).get('message')}")


# ---------------------------------------------------------------------------
# C. scope 校验（fail-closed 零写入）
# ---------------------------------------------------------------------------


def section_b_deleted_race(h: Harness) -> None:
    print("[B2] current delete race")
    out = scenario(h, "deleted_race.db", "current_deleted_race")
    step("second recheck missing current becomes current_moved", counts_of(out).get("current_moved") == 1, f"counts={counts_of(out)}")
    step(
        "deleted current race performs no Neo4j/Milvus writes",
        calls_of(out) == {"graph": [], "vector": []}
        and obj_marker(out, "__NEO4J__") == {}
        and obj_marker(out, "__MILVUS__") == {},
        f"calls={calls_of(out)} neo={obj_marker(out, '__NEO4J__')} mil={obj_marker(out, '__MILVUS__')}",
    )
    step(
        "deleted current race does not invent a document aggregate after row deletion",
        result_of(out).get("document_states") == [],
        f"docs={result_of(out).get('document_states')}",
    )
    out = scenario(h, "excluded_aggregate.db", "outdated_revision")
    step(
        "all targets excluded still aggregates existing document",
        result_of(out).get("document_states") == [{"doc_id": DOC, "graph_status": "pending", "vector_status": "pending"}],
        f"docs={result_of(out).get('document_states')}",
    )


def section_c(h: Harness) -> None:
    print("[C] 作用域校验 fail-closed")
    out = scenario(h, "empty_targets.db", "empty_targets")
    exc = exception_of(out)
    step("空 targets → ValidationException/REINDEX_SCOPE_REQUIRED", exc.get("type") == "ValidationException" and exc.get("code") == "REINDEX_SCOPE_REQUIRED", f"exc={exc}")
    step("空 targets 不写任何索引", calls_of(out) == {"graph": [], "vector": []}, f"calls={calls_of(out)}")

    out = scenario(h, "illegal_target.db", "illegal_target")
    exc = exception_of(out)
    step("缺 target_revision 的非法元素 → REINDEX_SCOPE_REQUIRED", exc.get("code") == "REINDEX_SCOPE_REQUIRED", f"exc={exc}")

    out = scenario(h, "missing_scope.db", "missing_scope")
    exc = exception_of(out)
    step("payload 缺 kb/tenant/project → KB_SCOPE_REQUIRED（执行前拒绝）", exc.get("type") == "ValidationException" and exc.get("code") == "KB_SCOPE_REQUIRED", f"exc={exc}")
    step("缺作用域时零索引写入、零状态回写", calls_of(out) == {"graph": [], "vector": []} and rev_map(out).get("c-1", {}).get("graph_status") == "pending", f"revs={rev_map(out)}")

    out = scenario(h, "registry_conflict.db", "registry_scope_conflict")
    exc = exception_of(out)
    step("payload 作用域与 knowledge_bases 登记冲突 → KB_CROSS_SCOPE", exc.get("code") == "KB_CROSS_SCOPE", f"exc={exc}")
    step("登记冲突零写入", calls_of(out) == {"graph": [], "vector": []} and obj_marker(out, "__NEO4J__") == {}, f"neo={obj_marker(out, '__NEO4J__')}")

    out = scenario(h, "row_conflict.db", "row_scope_conflict")
    counts = counts_of(out)
    res = result_of(out)
    step("current 行作用域冲突计入 scope_mismatches 且不写该 chunk", counts.get("scope_mismatch") == 1 and counts.get("graph_indexed") == 1, f"counts={counts}")
    mismatch = (res.get("scope_mismatches") or [{}])[0]
    step("冲突明细标注 revision.tenant_id", "revision.tenant_id" in json.dumps(mismatch, ensure_ascii=False), f"mismatch={mismatch}")
    revs = rev_map(out)
    step("作用域冲突行保持未收敛（不越权改写）", revs.get("c-1", {}).get("graph_status") == "pending" and revs.get("c-2", {}).get("graph_status") == "indexed", f"revs={revs}")
    step("文档级聚合不因单块被阻断而放大成 indexed", doc_map(out).get(DOC, {}).get("graph_status") == "pending", f"docs={doc_map(out)}")


# ---------------------------------------------------------------------------
# D. 能力开关与 §8.5 字段缺失
# ---------------------------------------------------------------------------


def section_d(h: Harness) -> None:
    print("[D] 能力关闭与 Milvus 显式字段缺失")
    out = scenario(h, "capability_off.db", "capability_off")
    step("能力关闭不写索引", calls_of(out) == {"graph": [], "vector": []}, f"calls={calls_of(out)}")
    counts = counts_of(out)
    step("两侧投影置 skipped", counts.get("graph_skipped") == 1 and counts.get("vector_skipped") == 1, f"counts={counts}")
    revs = rev_map(out)
    step(
        "skipped 必须把 *_content_revision 置 NULL（§8.4 不伪装 indexed）",
        revs.get("c-1", {}).get("graph_status") == "skipped" and revs.get("c-1", {}).get("graph_content_revision") is None
        and revs.get("c-1", {}).get("vector_status") == "skipped" and revs.get("c-1", {}).get("vector_content_revision") is None,
        f"revs={revs}",
    )
    step("文档级 skipped → stale", doc_map(out).get(DOC) == {"graph_status": "stale", "vector_status": "stale"}, f"docs={doc_map(out)}")

    out = scenario(h, "rev_field_absent.db", "revision_field_absent")
    exc = exception_of(out)
    step("collection 缺显式字段 → ValidationException/INDEX_UNAVAILABLE（不自动重试）", exc.get("type") == "ValidationException" and exc.get("code") == "INDEX_UNAVAILABLE", f"exc={exc}")
    step("向量腿零写入", calls_of(out)["vector"] == [] and obj_marker(out, "__MILVUS__") == {}, f"mil={obj_marker(out, '__MILVUS__')}")
    step("graph 腿正常落库", calls_of(out)["graph"] == ["c-1"] and counts_of(out).get("graph_indexed") == 1, f"counts={counts_of(out)}")
    revs = rev_map(out)
    step("vector 保持 pending + 版本 NULL，转交 v3 迁移后重排", revs.get("c-1", {}).get("vector_status") == "pending" and revs.get("c-1", {}).get("vector_content_revision") is None, f"revs={revs}")
    step("counts 记录 vector_blocked", counts_of(out).get("vector_blocked") == 1, f"counts={counts_of(out)}")


# ---------------------------------------------------------------------------
# E. 失败语义与文档级聚合（§6.2）
# ---------------------------------------------------------------------------


def section_e(h: Harness) -> None:
    print("[E] 写入失败 → 抛错重试 + 文档级聚合")
    out = scenario(h, "graph_fail.db", "graph_write_failed")
    exc = exception_of(out)
    step("投影写入失败 → RuntimeError（按 max_retries 退避重试）", exc.get("type") == "RuntimeError", f"exc={exc}")
    step("失败信息含 kb 与 chunk", "kb-b0" in str(exc.get("code")) and "c-1" in str(exc.get("code")), f"exc={exc}")
    revs = rev_map(out)
    step("graph 落 failed 且版本 NULL，vector 仍如实 indexed", revs.get("c-1", {}).get("graph_status") == "failed" and revs.get("c-1", {}).get("graph_content_revision") is None and revs.get("c-1", {}).get("vector_status") == "indexed", f"revs={revs}")
    step("文档级聚合取最差（failed > indexed）", doc_map(out).get(DOC, {}).get("graph_status") == "failed", f"docs={doc_map(out)}")

    out = scenario(h, "doc_agg.db", "doc_aggregation")
    docs = doc_map(out)
    step("干净文档聚合为 failed/indexed", docs.get("doc-ok") == {"graph_status": "failed", "vector_status": "indexed"}, f"docs={docs}")
    step("含未涉及 chunk 的文档 graph=failed、vector=pending", docs.get("doc-mixed") == {"graph_status": "failed", "vector_status": "pending"}, f"docs={docs}")
    step("跨 doc_id 的 targets 都按行处理（未涉及块不被牵连）", rev_map(out).get("b-2", {}).get("graph_status") == "pending" and rev_map(out).get("b-1", {}).get("graph_status") == "failed" and rev_map(out).get("a-1", {}).get("graph_status") == "failed", f"revs={rev_map(out)}")


# ---------------------------------------------------------------------------
# F. 索引侧真实代码路径（非 mock）
# ---------------------------------------------------------------------------


def section_f(h: Harness) -> None:
    print("[F] vector_store 真实路径：整行替换读回 + §8.5 拒写守卫")
    out = scenario(h, "merge.db", "existing_fields_merge")
    merged = obj_marker(out, "__MERGE__")
    row = (merged.get("merged") or {}).get("c-1", {})
    step("读回已有 title/location/embedding_model（不被整行替换洗掉）", row.get("title") == "标题" and row.get("location") == "p.1" and row.get("embedding_model") == "m-1", f"merged={str(merged)[:400]}")
    step("entities_json 解析并剔除空实体", row.get("entities") == ["张三"], f"row={row}")
    call = merged.get("call") or {}
    step("output_fields 按 collection 实际 schema 过滤", {"chunk_id", "title", "location", "entities_json", "embedding_model"} <= set(call.get("output_fields") or []), f"call={call}")
    step("查询表达式带 kb 作用域过滤", 'kb_id in ["kb-b0"]' in str(call.get("filter")), f"filter={call.get('filter')}")

    out = scenario(h, "guard.db2", "upsert_guard")
    guard = obj_marker(out, "__GUARD__")
    step("无显式字段时 upsert_chunks 拒写", guard.get("raised") is True, f"guard={guard}")
    step("缺显式字段抛 VectorStoreSchemaError", guard.get("type") == "VectorStoreSchemaError", f"guard={guard}")
    step("拒写理由指向 v3 迁移与 dynamic metadata 禁令", "dynamic metadata" in str(guard.get("error")) and "content_revision" in str(guard.get("error")), f"guard={guard}")


# ---------------------------------------------------------------------------
# G. 闭环：backfill 入队 → worker 消费 → 复跑得到 CLOSED
# ---------------------------------------------------------------------------


def section_f2(h: Harness) -> None:
    out = scenario(h, "schema_contract.db", "schema_and_upsert_contract")
    contract = obj_marker(out, "__SCHEMA_CONTRACT__")
    step("content_revision 只有显式 INT64 才通过", contract.get("schema") == {"int64": True, "missing_type": False, "varchar": False}, f"contract={contract}")
    step("Milvus upsert 数量不符进入 VectorStoreUpsertError", contract.get("mutation", {}).get("type") == "VectorStoreUpsertError", f"contract={contract}")


def section_g(h: Harness) -> None:
    print("[G] 闭环验收（B0 必证：入队 → 消费 → 投影更新 → 复跑 CLOSED）")
    out = scenario(h, "closed_loop.db", "closed_loop")
    enqueue = obj_marker(out, "__ENQUEUE__")
    step("backfill 入队 reindex_chunks 任务", enqueue.get("enqueued") == 1 and enqueue.get("has_job") is True, f"enqueue={enqueue}")
    consumed = obj_marker(out, "__CONSUMED__")
    step("worker 真实消费同一 payload 并两侧 indexed", consumed.get("graph_indexed") == 1 and consumed.get("vector_indexed") == 1, f"consumed={consumed}")
    step("Neo4j/Milvus 假投影库确实被更新", obj_marker(out, "__NEO4J__") != {} and obj_marker(out, "__MILVUS__") != {}, out[-300:])
    gate = obj_marker(out, "__GATE__")
    step("复跑 backfill inventory → CLOSED", gate.get("closed") is True, f"gate={gate}")
    step("needs_reindex/blocked/unrecoverable 全部归零", gate.get("needs_reindex") == 0 and gate.get("blocked") == 0 and gate.get("unrecoverable") == 0, f"gate={gate}")
    step("未降级（两侧能力均配置，不是 CLOSED_DEGRADED）", gate.get("degraded_skipped") is False, f"gate={gate}")
    reenqueue = obj_marker(out, "__REENQUEUE__")
    step("收敛后不再产生新任务", reenqueue.get("enqueued") == 0 and reenqueue.get("targets") == 0, f"reenqueue={reenqueue}")
    jobs = arr_marker(out, "__JOBS__")
    step("admin_jobs 只有一条 reindex_chunks 入队记录（targets_hash 去重）", len([j for j in jobs if j[0] == "reindex_chunks"]) == 1, f"jobs={jobs}")
    step("current 行终态 indexed/1 双侧一致", rev_map(out).get("c-1", {}).get("graph_status") == "indexed" and rev_map(out).get("c-1", {}).get("vector_content_revision") == 1, f"revs={rev_map(out)}")


def main() -> int:
    print("=" * 60)
    print("GraphInsight M5-B0 reindex_chunks worker acceptance tests (sqlite)")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        h.use_db("guard.db")
        h.guard_sqlite()
        section_s(h)
        section_a(h)
        section_b(h)
        section_b_deleted_race(h)
        section_c(h)
        section_d(h)
        section_e(h)
        section_f(h)
        section_f2(h)
        section_g(h)
    print("-" * 60)
    if FAILURES:
        for item in FAILURES:
            print(f"  FAILED: {item}")
        print(f"✗ {len(FAILURES)} checks failed")
        return 1
    print("✓ all M5-B0 reindex_chunks checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
