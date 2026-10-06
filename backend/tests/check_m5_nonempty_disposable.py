#!/usr/bin/env python3
"""M5 非空 disposable 全链路脚本（真实 PG/Neo4j/Milvus，独立容器与卷）。**仅准备，不自动执行。**

与阶段 3 disposable（`gate0-evidence/2026-10-05/phase3/run_phase3_disposable.py`）的差异：
  - 三引擎**全开**（阶段 3 关闭 Milvus，`VECTOR_STORE_ENABLED=false`）；
  - 两条写路径**分别验证**：
      S1 双写   = 在线 `online_s1`（worker 消费 `reindex_chunks` → `upsert_chunks` 扇出
                  primary(legacy v2，剥版本) + shadow(v3，带版本)）；
      单向回填  = `backfill_versioned`（`admin/backfill_chunk_revisions.py` → 只补 shadow，不写 primary）；
  - 读回、失败重放、幂等、回滚。

隔离口径：
  - 一次性 network `gi-nonempty-net`、容器 `gi-nonempty-pg/neo4j/milvus`、独立卷；
  - 仅绑 127.0.0.1 独立端口（PG 55433 / Neo4j 29532 / Milvus 19532，避开共享 5434/55432/29531/19530/19531）；
  - 预检发现同名对象即拒绝执行，绝不覆盖别人；
  - 收尾 `docker rm -f` + `network rm` + `volume rm`，按名字复查零残留。

门控：默认 `--dry-run` 打印完整计划（exit 3，零连接零写入）；执行需
  `--authorize NONEMPTY-DISPOSABLE-AUTHORIZED`。

已知约束（证据必须随同声明）：
  - 一次性 Milvus standalone（内嵌 etcd）在本机可能起不来（panic: failed to create etcd client）；
    起不来则 fail-closed 报 FAIL，不假绿；真实 Milvus 写入证据仅在容器成功拉起后才有意义。
  - embedding 用**确定性本地测试向量**（dim=4），不请求外部模型；"真实"指存储引擎（PG/Neo4j/Milvus）。
  - 本脚本只准备、不自动执行；执行范围另行确认。执行后按 Gate 0 证据合同落盘
    （单 RUN_ID/EXEC_TIME、JSON `json.load` 可直过、meta 每轮覆盖写、校验失败 fail-closed exit 2）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

# ---- 一次性资源命名（前缀 gi-nonempty-，与共享 graphinsight-* 严格区分） ----
NET = "gi-nonempty-net"
PG = "gi-nonempty-pg"
NEO4J = "gi-nonempty-neo4j"
MILVUS = "gi-nonempty-milvus"
VOLUMES = ["gi-nonempty-pg-data", "gi-nonempty-neo4j-data", "gi-nonempty-milvus-data"]
CONTAINERS = [PG, NEO4J, MILVUS]

PG_IMAGE = "postgres:16-alpine"
NEO4J_IMAGE = "neo4j:5.26.26"
MILVUS_IMAGE = "milvusdb/milvus:v2.5.4"

PG_PORT = 55433          # 独立端口，避开共享 5434 / phase3 55432
NEO4J_BOLT_PORT = 29532  # 避开共享 7687 / phase3 29531
MILVUS_PORT = 19532      # 避开共享 19530 / 19531

PG_URL = f"postgresql+psycopg://gi@127.0.0.1:{PG_PORT}/gi_nonempty"
NEO4J_URI = f"bolt://127.0.0.1:{NEO4J_BOLT_PORT}"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "none"
MILVUS_URI = f"http://127.0.0.1:{MILVUS_PORT}"

PRIMARY_COLLECTION = "graphinsight_chunks_v2_nonempty"
SHADOW_COLLECTION = "graphinsight_chunks_v3_nonempty"

AUTHORIZE_TOKEN = "NONEMPTY-DISPOSABLE-AUTHORIZED"

FAILURES: List[str] = []
CHECKS: List[Tuple[str, bool]] = []
STEPS: List[Dict[str, Any]] = []
# 是否已创建一次性容器/网络（决定 cleanup 是否销毁；预检拒绝时不得误删他人同名对象）。
CREATED_RESOURCES = False
# 是否已播种 Neo4j/PG 数据（决定 cleanup 是否连 Neo4j/Milvus 清理；未播种时不得连接任何引擎）。
SEEDED = False


def say(line: str = "") -> None:
    print(line, flush=True)


def add_step(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok)))
    STEPS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:800]})
    say(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# 计划（dry-run）
# ---------------------------------------------------------------------------
def print_plan() -> None:
    say("=" * 72)
    say("M5 非空 disposable 全链路 —— 执行计划（dry-run，零连接零写入）")
    say("=" * 72)
    for title in (
        "[0] 预检：同名网络/容器/卷必须不存在（gi-nonempty-*），否则拒绝",
        "[1] 拉起一次性容器（PG 55433 + Neo4j 29532 + Milvus 19532），仅绑 127.0.0.1",
        "[2] 建表：migrate_chunk_revisions + migrate_jobs_targets_hash + bootstrap admin",
        "[3] 播种非空数据：PG knowledge_bases/chunk_revisions(current 行) + Neo4j Chunk 节点 + 解析产物",
        "[4] S1 双写验证：建 primary(v2 无版本字段)+shadow(v3 带版本) 双 collection，",
        "    配置 dual_write，execute_job(reindex_chunks) → upsert_chunks(online_s1)，",
        "    读回 primary 剥版本 + shadow 带版本",
        "[5] 单向回填验证：backfill_chunk_revisions.py → backfill_versioned 只补 shadow，",
        "    读回 shadow 更新、primary 零写入",
        "[6] 失败重放：注入（Neo4j 不可达 / Milvus 不可达）→ fail-closed 非零退出 + 清单留痕 + 零半截写入",
        "[7] 幂等：恢复后重跑 rows_new=0、总行数不翻倍",
        "[8] 回滚：rollback → 结构消失（chunk_revisions 表 + targets_hash 列）→ 再迁移再跑通",
        "[9] 清理：删除播种对象 + docker rm -f/network rm/volume rm + 复查零残留",
        "[10] 证据落盘（Gate 0 合同：单 RUN_ID、JSON 可解析、meta 覆盖写、fail-closed exit 2）",
    ):
        say(title)
    say("=" * 72)
    say(f"如需执行：python {Path(__file__).name} --authorize {AUTHORIZE_TOKEN}")
    say("执行范围与共享环境影响另行确认；本脚本默认拒绝执行。")
    say("=" * 72)


# ---------------------------------------------------------------------------
# docker 辅助
# ---------------------------------------------------------------------------
def _run(cmd: List[str], *, timeout: int = 300, label: str = "", cwd: Optional[str] = None,
         env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd or str(BACKEND),
                              env=env or os.environ)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timeout>{timeout}s ({label})"
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def _docker_names(*, all: bool = False) -> set:  # noqa: A002
    flag = "-a" if all else ""
    code, out = _run(["docker", "ps", flag, "--format", "{{.Names}}"], timeout=60)
    if code != 0:
        raise RuntimeError(f"docker ps 枚举失败（exit={code}）: {out[-200:]}")
    return set((out or "").split())


def _docker_networks() -> set:
    code, out = _run(["docker", "network", "ls", "--format", "{{.Name}}"], timeout=60)
    if code != 0:
        raise RuntimeError(f"docker network ls 枚举失败（exit={code}）: {out[-200:]}")
    return set((out or "").split())


def _docker_volumes() -> set:
    code, out = _run(["docker", "volume", "ls", "--format", "{{.Name}}"], timeout=60)
    if code != 0:
        raise RuntimeError(f"docker volume ls 枚举失败（exit={code}）: {out[-200:]}")
    return set((out or "").split())


# ---------------------------------------------------------------------------
# 环境与连接
# ---------------------------------------------------------------------------
# 受控 env：清除宿主 env 里会污染的关键变量，只让 env file（GRAPHINSIGHT_BACKEND_ENV_FILE）
# 决定这些值，避免宿主已设的共享库连接串/双写开关泄漏进一次性子进程。
_CONTROLLED_ENV_KEYS = (
    "GRAPHINSIGHT_BACKEND_ENV_FILE",
    "ADMIN_DATABASE_URL",
    "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_DATABASE", "NEO4J_CONFIG_SOURCE",
    "MILVUS_URI", "MILVUS_TOKEN", "MILVUS_DB_NAME", "MILVUS_COLLECTION",
    "MILVUS_DUAL_WRITE", "MILVUS_SHADOW_COLLECTION",
    "MILVUS_WRITE_SCOPE_KB_IDS", "MILVUS_WRITE_SCOPE_TENANT", "MILVUS_WRITE_SCOPE_PROJECT",
    "MILVUS_METRIC_TYPE", "MILVUS_INDEX_TYPE", "MILVUS_SEARCH_NPROBE",
    "VECTOR_STORE_ENABLED", "VECTOR_STORE_PROVIDER",
    "LLM_ENABLED", "EMBEDDING_ENABLED", "EMBEDDING_API_KEY", "EMBEDDING_PROVIDER", "EMBEDDING_BASE_URL",
    "AI_SERVICE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "PARSED_DOCUMENT_STORAGE_PATH", "BACKFILL_MANIFEST_DIR",
)


def base_env(tmp: Path, *, neo4j_uri: str = NEO4J_URI, milvus_uri: str = MILVUS_URI,
             dual_write: bool = True) -> Dict[str, str]:
    env_file = tmp / "nonempty.env"
    lines = [
        f"ADMIN_DATABASE_URL={PG_URL}",
        f"NEO4J_URI={neo4j_uri}",
        f"NEO4J_USER={NEO4J_USER}",
        f"NEO4J_PASSWORD={NEO4J_PASSWORD}",
        "LLM_ENABLED=true",                       # graph 能力开启
        "EMBEDDING_ENABLED=true",                 # vector 能力：embedding 已配置
        "EMBEDDING_API_KEY=nonempty-disposable-local",  # 能力判定要求非空 api_key（确定性本地向量，不外发）
        "VECTOR_STORE_ENABLED=true",              # vector 能力：vector_store 开启
        f"MILVUS_URI={milvus_uri}",
        f"MILVUS_COLLECTION={PRIMARY_COLLECTION}",
        f"MILVUS_DUAL_WRITE={'true' if dual_write else 'false'}",
        f"MILVUS_SHADOW_COLLECTION={SHADOW_COLLECTION}",
        f"PARSED_DOCUMENT_STORAGE_PATH={(tmp / 'parsed_documents').as_posix()}",
        "",
    ]
    env_file.write_text("\n".join(lines), encoding="utf-8")
    env = dict(os.environ)
    for key in _CONTROLLED_ENV_KEYS:
        env.pop(key, None)  # 清除宿主污染，只受 env file 控制
    env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(env_file)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(BACKEND)
    return env


def run_py(tmp: Path, script: str, args: List[str], *, neo4j_uri: str = NEO4J_URI,
           milvus_uri: str = MILVUS_URI, dual_write: bool = True) -> Tuple[int, str]:
    cmd = [sys.executable, script] + list(args)
    env = base_env(tmp, neo4j_uri=neo4j_uri, milvus_uri=milvus_uri, dual_write=dual_write)
    # 受控 env 必须真正传给子进程（否则子进程继承宿主 os.environ，受控隔离失效）。
    return _run(cmd, timeout=300, label=script, cwd=str(BACKEND), env=env)


def pg_connect():
    import psycopg

    return psycopg.connect(f"host=127.0.0.1 port={PG_PORT} dbname=gi_nonempty user=gi", connect_timeout=5)


def neo4j_driver(uri: str = NEO4J_URI):
    from neo4j import GraphDatabase

    return GraphDatabase.driver(uri, auth=(NEO4J_USER, NEO4J_PASSWORD), connection_timeout=5)


def table_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = %s AND table_schema = CURRENT_SCHEMA()",
            (name,),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# 编排步骤
# ---------------------------------------------------------------------------
def ensure_containers() -> None:
    global CREATED_RESOURCES
    CREATED_RESOURCES = True
    net = _docker_networks()
    if NET not in net:
        code, out = _run(["docker", "network", "create", NET], timeout=60)
        if code != 0:
            raise RuntimeError(f"docker network create {NET} 失败（exit={code}）: {out[-200:]}")
    names = _docker_names(all=True)
    if PG not in names:
        code, out = _run(["docker", "run", "-d", "--name", PG, "--network", NET,
                          "-p", f"127.0.0.1:{PG_PORT}:5432",
                          "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=gi_nonempty",
                          "-e", "POSTGRES_USER=gi", "-v", f"{VOLUMES[0]}:/var/lib/postgresql/data",
                          PG_IMAGE], timeout=180)
        if code != 0:
            raise RuntimeError(f"docker run {PG} 失败（exit={code}）: {out[-200:]}")
    if NEO4J not in names:
        code, out = _run(["docker", "run", "-d", "--name", NEO4J, "--network", NET,
                          "-p", f"127.0.0.1:{NEO4J_BOLT_PORT}:7687",
                          "-e", "NEO4J_AUTH=none", "-v", f"{VOLUMES[1]}:/data", NEO4J_IMAGE], timeout=180)
        if code != 0:
            raise RuntimeError(f"docker run {NEO4J} 失败（exit={code}）: {out[-200:]}")
    if MILVUS not in names:
        # 与项目 compose 栈同款启动方式（milvus run standalone + 内嵌 etcd + local storage）。
        # 已知约束：内嵌 etcd 在本机可能 panic 起不来，起不来由下方就绪轮询判 FAIL（fail-closed）。
        code, out = _run(["docker", "run", "-d", "--name", MILVUS, "--network", NET,
                          "-p", f"127.0.0.1:{MILVUS_PORT}:19530",
                          "-e", "ETCD_USE_EMBED=true", "-e", "ETCD_DATA_DIR=/var/lib/milvus/etcd",
                          "-e", "COMMON_STORAGETYPE=local", "-v", f"{VOLUMES[2]}:/var/lib/milvus",
                          MILVUS_IMAGE, "milvus", "run", "standalone"], timeout=180)
        if code != 0:
            raise RuntimeError(f"docker run {MILVUS} 失败（exit={code}）: {out[-200:]}")

    deadline = time.time() + 300
    pg_ready = neo_ready = mil_ready = False
    while time.time() < deadline:
        if not pg_ready:
            try:
                with pg_connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                pg_ready = True
            except Exception:  # noqa: BLE001
                pass
        if not neo_ready:
            try:
                d = neo4j_driver()
                d.verify_connectivity()
                d.close()
                neo_ready = True
            except Exception:  # noqa: BLE001
                pass
        if not mil_ready:
            try:
                from pymilvus import MilvusClient

                c = MilvusClient(uri=MILVUS_URI, timeout=3)
                c.list_collections()
                mil_ready = True
            except Exception:  # noqa: BLE001
                pass
        if pg_ready and neo_ready and mil_ready:
            break
        time.sleep(3)
    if not (pg_ready and neo_ready and mil_ready):
        # 就绪失败 fail-closed：抛异常 → main 的 except 记入失败证据并停止后续迁移/播种。
        raise RuntimeError(
            f"disposable 容器就绪失败（pg={pg_ready} neo4j={neo_ready} milvus={mil_ready}），"
            "不得继续迁移/播种"
        )
    add_step("disposable 容器就绪（PG + Neo4j + Milvus）", True,
             f"pg={pg_ready} neo4j={neo_ready} milvus={mil_ready}")


def migrate_tables(tmp: Path) -> None:
    code, out = run_py(tmp, "admin/migrate_chunk_revisions.py", ["--action", "migrate"])
    add_step("migrate chunk_revisions", code == 0, out[-200:])
    code, out = run_py(tmp, "-c", [
        "from admin.database import Base, engine;"
        "from admin.models import AdminUser, AdminJob, KnowledgeBase, KnowledgeBaseDocument;"
        "Base.metadata.create_all(bind=engine, tables=[AdminUser.__table__, AdminJob.__table__, "
        "KnowledgeBase.__table__, KnowledgeBaseDocument.__table__]);"
        "engine.dispose(); print('bootstrap ok')"
    ])
    add_step("bootstrap admin_jobs + knowledge_bases", code == 0 and "bootstrap ok" in out, out[-200:])
    code, out = run_py(tmp, "admin/migrate_jobs_targets_hash.py", ["--action", "migrate"])
    add_step("migrate targets_hash", code == 0, out[-200:])


def seed_s1(tmp: Path, kb: str, s1_chunk: str, doc: str) -> None:
    """S1 数据组：KB 登记 + chunk_revisions current 行（revision=1，双侧 pending）+ Neo4j 节点。

    S1 组 chunk（s1_chunk）有 current revision，走 online_s1（worker 消费 reindex_chunks → 双侧写）。
    """
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO knowledge_bases (id, tenant_id, project_id, name, slug, status, storage_prefix) "
                    "VALUES (%s,%s,%s,%s,%s,'active',%s) ON CONFLICT (id) DO NOTHING",
                    (kb, "t-nonempty", "p-nonempty", "nonempty-disposable", "nonempty-disposable", kb),
                )
                content = f"非空全链路内容-{s1_chunk}"
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                cur.execute(
                    "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
                    "source_content, source_content_hash, content, content_hash, content_revision, "
                    "revision_status, graph_status, vector_status, revision_source, reason, trace_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,'current','pending','pending',"
                    "'system_initial','nonempty_disposable','nonempty-disposable')",
                    (kb, "t-nonempty", "p-nonempty", doc, s1_chunk, content, digest, content, digest),
                )
            conn.commit()
        add_step("S1 组：KB 登记 + current 行播种（真实 PG）", True)
    except Exception as exc:  # noqa: BLE001
        add_step("S1 组：KB 登记 + current 行播种（真实 PG）", False, f"{type(exc).__name__}: {exc}")

    try:
        driver = neo4j_driver()
        with driver.session() as s:
            s.run(
                "MERGE (c:Chunk {chunk_id: $cid}) SET c.kb_id=$kb, c.doc_id=$doc, c.text=$text, "
                "c.tenant_id=$t, c.project_id=$p",
                {"cid": s1_chunk, "kb": kb, "doc": doc, "text": f"非空全链路内容-{s1_chunk}",
                 "t": "t-nonempty", "p": "p-nonempty"},
            )
        driver.close()
        global SEEDED
        SEEDED = True  # Neo4j + PG 已播种，cleanup 才允许连 Neo4j/Milvus 清理
        add_step("S1 组：Neo4j 节点播种（真实图库）", True)
    except Exception as exc:  # noqa: BLE001
        add_step("S1 组：Neo4j 节点播种（真实图库）", False, f"{type(exc).__name__}: {exc}")


# backfill 组：向 primary(v2) 预写向量（读源不变量 §16.1：primary 必须已持有该 chunk）
SEED_BF_DRIVER = (
    "from pymilvus import MilvusClient\n"
    "uri={uri!r}; primary={primary!r}; kb={kb!r}; chunk={chunk!r}; doc={doc!r}\n"
    "c=MilvusClient(uri=uri)\n"
    "rows=[{{'chunk_id': chunk, 'kb_id': kb, 'tenant_id': 't-nonempty', 'project_id': 'p-nonempty', "
    "'doc_id': doc, 'text': '非空全链路内容-'+chunk, 'vector': [0.9,0.8,0.7,0.6]}}]\n"
    "c.upsert(collection_name=primary, data=rows)\n"
    "print('SEED_BF_PRIMARY_OK')\n"
)


def seed_backfill(tmp: Path, kb: str, bf_chunk: str, doc: str) -> None:
    """backfill 数据组：解析产物（可恢复证据）+ primary(v2) 预写向量，PG 无 current revision。

    backfill 组 chunk（bf_chunk）与 S1 组（s1_chunk）不同：PG 开始没有 current revision，
    primary(v2) 预先存在该 chunk 的向量，shadow(v3) 开始没有该 chunk。执行 backfill_versioned 后
    应 rows_new=1、primary 不变、shadow 新增显式 content_revision。
    """
    parsed_root = tmp / "parsed_documents"
    d = parsed_root / kb / doc
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"parser_version": "nonempty", "content_hash": "h-" + doc}, ensure_ascii=False),
        encoding="utf-8",
    )
    (d / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": bf_chunk, "doc_id": doc, "text": f"非空全链路内容-{bf_chunk}"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    add_step("backfill 组：解析产物落盘（可恢复证据）", (d / "chunks.jsonl").exists())

    code, out = run_py(tmp, "-c", [SEED_BF_DRIVER.format(uri=MILVUS_URI, primary=PRIMARY_COLLECTION,
                                                         kb=kb, chunk=bf_chunk, doc=doc)],
                       dual_write=True)
    add_step("backfill 组：primary(v2) 预写向量", code == 0 and "SEED_BF_PRIMARY_OK" in out, out[-400:])


# S1 双写：内联 driver 消费 reindex_chunks，真实写 primary+shadow（monkeypatch 确定性向量）
S1_DRIVER = (
    "import sys, uuid\n"
    "sys.path.insert(0, {backend!r})\n"
    "from sqlalchemy import text\n"
    "from admin.database import engine\n"
    "from pymilvus import DataType, MilvusClient\n"
    "from services.vector_store import vector_store\n"
    "from services.embedding_service import embedding_service\n"
    "from services.job_runtime import execute_job\n"
    "kb={kb!r}; tenant='t-nonempty'; project='p-nonempty'; doc={doc!r}; chunk={chunk!r}\n"
    "primary={primary!r}; shadow={shadow!r}; uri={uri!r}\n"
    "def _mk(name, with_rev):\n"
    "    c=MilvusClient(uri=uri)\n"
    "    if c.has_collection(name): c.drop_collection(name)\n"
    "    sc=c.create_schema(auto_id=False, enable_dynamic_field=True)\n"
    "    sc.add_field(field_name='chunk_id', datatype=DataType.VARCHAR, is_primary=True, max_length=128)\n"
    "    for f in ('doc_id','kb_id','tenant_id','project_id'): sc.add_field(field_name=f, datatype=DataType.VARCHAR, max_length=128)\n"
    "    sc.add_field(field_name='text', datatype=DataType.VARCHAR, max_length=4096)\n"
    "    if with_rev: sc.add_field(field_name='content_revision', datatype=DataType.INT64)\n"
    "    sc.add_field(field_name='vector', datatype=DataType.FLOAT_VECTOR, dim=4)\n"
    "    c.create_collection(collection_name=name, schema=sc)\n"
    "_mk(primary, False)\n"
    "_mk(shadow, True)\n"
    "base=vector_store.config()\n"
    "vector_store.config=lambda: {{**base, 'collection': primary, 'dual_write': True, 'shadow_collection': shadow}}\n"
    "embedding_service.embed_texts=lambda texts: [[0.1,0.2,0.3,0.4] for _ in texts]\n"
    "vector_store._revision_field.clear()\n"
    "with engine.begin() as conn:\n"
    "    conn.execute(text(\"UPDATE chunk_revisions SET graph_status='pending', vector_status='pending' \"\n"
    "                       \"WHERE kb_id=:kb AND chunk_id=:c\"), {{'kb': kb, 'c': chunk}})\n"
    "res=execute_job(job_id=0, job_type='reindex_chunks', payload={{'kb_id': kb, 'tenant_id': tenant,\n"
    "    'project_id': project, 'targets': [{{'chunk_id': chunk, 'target_revision': 1}}],\n"
    "    'doc_id': doc, 'source': 'nonempty_s1'}})\n"
    "print('S1_EXEC_STATUS=' + str(res.get('execution_status')))\n"
    "c=MilvusClient(uri=uri)\n"
    "def _q(name, fields):\n"
    "    c.load_collection(collection_name=name)\n"
    "    return list(c.query(collection_name=name, filter='kb_id == \"'+kb+'\"', output_fields=fields, limit=10) or [])\n"
    "# 双侧读回：primary（legacy v2 剥版本）+ shadow（v3 显式版本）。\n"
    "# 真实观察动态残留：primary 查询 content_revision，SDK 会把 $meta 展开到顶层，残留即非空。\n"
    "p=_q(primary, ['chunk_id','text','kb_id','content_revision'])\n"
    "s=_q(shadow, ['chunk_id','text','kb_id','content_revision'])\n"
    "p_residual=[r.get('content_revision') for r in p if r.get('content_revision') not in (None, '')]\n"
    "s_rev=[r.get('content_revision') for r in s if r.get('content_revision') not in (None, '')]\n"
    "print('S1_PRIMARY_ROWS=' + str(len(p)) + ' S1_PRIMARY_RESIDUAL=' + str(p_residual))\n"
    "print('S1_SHADOW_ROWS=' + str(len(s)) + ' S1_SHADOW_REV=' + str(s_rev))\n"
)


def verify_s1_dual_write(tmp: Path, kb: str, doc: str, chunk: str) -> None:
    code, out = run_py(
        tmp, "-c", [S1_DRIVER.format(backend=str(BACKEND), kb=kb, doc=doc, chunk=chunk,
                                     primary=PRIMARY_COLLECTION, shadow=SHADOW_COLLECTION,
                                     uri=MILVUS_URI)],
        dual_write=True,
    )
    add_step("S1 双写：execute_job 退出 0", code == 0, out[-400:])
    add_step("S1 双写：execution_status=completed", "S1_EXEC_STATUS=completed" in out, out[-400:])
    add_step("S1 双写：primary 收到 1 行且动态残留为空（剥版本成立）",
             "S1_PRIMARY_ROWS=1" in out and "S1_PRIMARY_RESIDUAL=[]" in out, out[-400:])
    add_step("S1 双写：shadow 收到 1 行且 content_revision=1（带版本）",
             "S1_SHADOW_ROWS=1" in out and "S1_SHADOW_REV=[1]" in out, out[-400:])


def verify_backfill(tmp: Path, kb: str, bf_chunk: str) -> None:
    # 单向回填（backfill_versioned）：backfill 组 chunk（bf_chunk）→ rows_new=1。
    # 需要 dual_write/shadow 生效（_upsert_backfill_versioned 依赖 resolve_dual_write().active）。
    code, out = run_py(tmp, "admin/backfill_chunk_revisions.py",
                       ["--kb", kb, "--manifest-dir", str(tmp / "mf")], dual_write=True)
    add_step("单向回填：backfill 退出 0", code == 0, out[-400:])
    add_step("单向回填：rows_new=1（backfill 组 bf_chunk）", "rows_new=1" in out, out[-400:])


def verify_readback(tmp: Path, kb: str, s1_chunk: str, bf_chunk: str) -> None:
    """读回验证：PG current 行（S1 组 + backfill 组各 1 行），revision=1 且 vector_status=indexed。"""
    # 列序（与 SELECT 一致）：0 chunk_id / 1 content_revision / 2 graph_status / 3 vector_status /
    # 4 graph_content_revision / 5 vector_content_revision
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, content_revision, graph_status, vector_status, "
                "graph_content_revision, vector_content_revision FROM chunk_revisions "
                "WHERE kb_id=%s AND revision_status='current' ORDER BY chunk_id", (kb,),
            )
            rows = cur.fetchall()
    pg_map = {r[0]: r for r in rows}
    expected = {s1_chunk, bf_chunk}
    add_step("PG 读回 current 行数=2（S1 组 + backfill 组）",
             len(rows) == 2 and set(pg_map) == expected, f"rows={sorted(pg_map)}")
    add_step("PG 读回 revision=1 且 vector_status=indexed（两组）",
             all(pg_map.get(c) and pg_map[c][1] == 1 and pg_map[c][3] == "indexed" for c in expected),
             str(pg_map))


def inject_failure(tmp: Path, kb: str, before: int) -> None:
    code, out = run_py(tmp, "admin/backfill_chunk_revisions.py",
                       ["--kb", kb, "--manifest-dir", str(tmp / "mf")],
                       neo4j_uri="bolt://127.0.0.1:29999")
    add_step("失败重放：不可达 Neo4j → 非零退出（fail-closed）", code != 0, f"exit={code}")
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunk_revisions WHERE kb_id=%s", (kb,))
            after = cur.fetchone()[0]
    add_step("失败重放：零新增写入（无半截数据）", after == before, f"before={before} after={after}")


def verify_idempotent(tmp: Path, kb: str, before: int) -> None:
    code, out = run_py(tmp, "admin/backfill_chunk_revisions.py",
                       ["--kb", kb, "--manifest-dir", str(tmp / "mf")])
    add_step("幂等：恢复后重跑退出 0", code == 0, out[-200:])
    add_step("幂等：rows_new=0", "rows_new=0" in out, out[-300:])
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunk_revisions WHERE kb_id=%s", (kb,))
            after = cur.fetchone()[0]
    add_step("幂等：总行数不翻倍", after == before, f"before={before} after={after}")


def verify_rollback(tmp: Path, kb: str) -> None:
    code, out = run_py(tmp, "admin/migrate_jobs_targets_hash.py", ["--action", "rollback"])
    add_step("回滚 targets_hash 退出 0", code == 0, out[-200:])
    code, out = run_py(tmp, "admin/migrate_chunk_revisions.py", ["--action", "rollback"])
    add_step("回滚 chunk_revisions 退出 0", code == 0, out[-200:])
    with pg_connect() as conn:
        chunk_gone = not table_exists(conn, "chunk_revisions")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='admin_jobs' AND column_name='targets_hash' "
                "AND table_schema=CURRENT_SCHEMA()"
            )
            col_gone = cur.fetchone() is None
    add_step("回滚后结构消失（chunk_revisions 表 + targets_hash 列）",
             chunk_gone and col_gone, f"chunk_gone={chunk_gone} col_gone={col_gone}")
    code, out = run_py(tmp, "admin/migrate_chunk_revisions.py", ["--action", "migrate"])
    add_step("再迁移 chunk_revisions 退出 0", code == 0, out[-200:])
    code, out = run_py(tmp, "admin/migrate_jobs_targets_hash.py", ["--action", "migrate"])
    add_step("再迁移 targets_hash 退出 0", code == 0, out[-200:])
    code, out = run_py(tmp, "admin/backfill_chunk_revisions.py",
                       ["--kb", kb, "--manifest-dir", str(tmp / "mf")])
    add_step("回滚重建后 backfill 可再跑（rows_new=2）", code == 0 and "rows_new=2" in out, out[-300:])


def cleanup(tmp: Path, kb: str, keep_containers: bool) -> None:
    # 所有权守卫：只有真正播种过（SEEDED）才允许连 Neo4j/Milvus 清理。
    # 预检失败/未播种时不得连接任何引擎（否则会连到可能不存在的容器，或误删他人对象）。
    if SEEDED:
        try:
            driver = neo4j_driver()
            with driver.session() as s:
                s.run("MATCH (c:Chunk) WHERE c.kb_id=$kb DETACH DELETE c", {"kb": kb})
            driver.close()
            add_step("Neo4j 播种节点已删除", True)
        except Exception as exc:  # noqa: BLE001
            add_step("Neo4j 播种节点已删除", False, f"{type(exc).__name__}: {exc}")
        try:
            from pymilvus import MilvusClient

            c = MilvusClient(uri=MILVUS_URI, timeout=3)
            for name in (PRIMARY_COLLECTION, SHADOW_COLLECTION):
                if c.has_collection(name):
                    c.drop_collection(name)
            add_step("Milvus 合成 collection 已删除", True)
        except Exception as exc:  # noqa: BLE001
            add_step("Milvus 合成 collection 已删除", False, f"{type(exc).__name__}: {exc}")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        add_step("临时目录已删除", not tmp.exists())
    except Exception as exc:  # noqa: BLE001
        add_step("临时目录已删除", False, str(exc))
    if not keep_containers and CREATED_RESOURCES:
        for name in CONTAINERS:
            _run(["docker", "rm", "-f", name], timeout=60)
        _run(["docker", "network", "rm", NET], timeout=60)
        for vol in VOLUMES:
            _run(["docker", "volume", "rm", "-f", vol], timeout=60)
        left_containers = [n for n in CONTAINERS if n in _docker_names(all=True)]
        left_nets = [NET] if NET in _docker_networks() else []
        left_vols = [v for v in VOLUMES if v in _docker_volumes()]
        add_step("disposable 容器/网络/卷已销毁（复查三者零残留）",
                 not (left_containers or left_nets or left_vols),
                 f"containers={left_containers} nets={left_nets} vols={left_vols}")


def _write_emergency_evidence(base: Path, run_id: str, exec_time: str, reason: str) -> None:
    """写盘失败时的紧急证据（fail-closed：至少一个标记文件说明失败，ROUND_EXIT=2）。"""
    try:
        (base / "nonempty_emergency.txt").write_text(
            "EVIDENCE_WRITE_FAILED\n"
            f"RUN_ID={run_id}\n"
            f"EXEC_TIME={exec_time}\n"
            f"REASON={reason}\n"
            "ROUND_EXIT=2\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - 连紧急证据都写不了，只能返回失败
        pass


def write_evidence(base: Path, run_id: str, exec_time: str) -> int:
    """证据落盘（fail-closed）：先完成全部检查，再一次性生成 JSON/meta。返回退出码（0/2）。

    JSON/meta/最终退出码共用同一份 finalized_failed：
    1. 写 steps JSON（当前 STEPS 快照）；
    2. 复验 JSON（读回：RUN_ID 一致 + 可解析 + 步骤数）；
    3. add_step 记录 JSON 复验（计入 CHECKS/STEPS）；
    4. 写 meta（用当前 CHECKS/finalized_failed）；
    5. 复验 meta（读回：RUN_ID 落盘可读）；
    6. add_step 记录 meta 复验；
    7. 用最终 STEPS/CHECKS 一次性重写 JSON + meta（CHECKS_TOTAL=最终步骤数、
       CHECKS_FAILED=最终失败数、ROUND_EXIT=2 iff 有失败）。

    不允许"先写 ROUND_EXIT=0 再追加复验步骤"。任一步写盘失败 → 紧急证据 + 返回 2。
    """
    base.mkdir(parents=True, exist_ok=True)
    steps_path = base / "nonempty_steps.json"
    meta_path = base / "nonempty_meta.txt"

    def _dump_steps(snapshot: list) -> None:
        doc = {
            "schema": "gate0-nonempty-disposable-steps-v1",
            "run_id": run_id,
            "exec_time": exec_time,
            "target_kind": "disposable-docker",
            "engines": {
                "postgres": f"{PG}@127.0.0.1:{PG_PORT}",
                "neo4j": f"{NEO4J}@127.0.0.1:{NEO4J_BOLT_PORT}",
                "milvus": f"{MILVUS}@127.0.0.1:{MILVUS_PORT}",
            },
            "write_paths": {"s1_dual_write": "online_s1(primary+shadow)", "backfill": "backfill_versioned(shadow-only)"},
            "steps": snapshot,
        }
        steps_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def _dump_meta(finalized_failed: list) -> None:
        meta = [
            "GATE NONEMPTY-DISPOSABLE ROUND METADATA (single round, overwritten each round)",
            "RUN_ID=" + run_id,
            "EXEC_TIME=" + exec_time,
            "TARGET_KIND=disposable-docker",
            f"ENGINES=postgres({PG_PORT})+neo4j({NEO4J_BOLT_PORT})+milvus({MILVUS_PORT})",
            f"CHECKS_TOTAL={len(CHECKS)}",
            f"CHECKS_FAILED={len(finalized_failed)}",
            "ROUND_EXIT=%d" % (2 if finalized_failed else 0),
            "NOTE: real engines are disposable docker containers; no shared dev, no shared Milvus, no push.",
        ]
        meta_path.write_text("\n".join(meta) + "\n", encoding="utf-8")

    # 阶段 1：写 steps JSON（当前 STEPS 快照）
    steps_snapshot = list(STEPS)
    try:
        _dump_steps(steps_snapshot)
    except Exception as exc:  # noqa: BLE001 - 写盘失败 fail-closed
        _write_emergency_evidence(base, run_id, exec_time, f"steps_json_write_failed:{type(exc).__name__}")
        return 2

    # 阶段 2：复验 steps JSON（读回：RUN_ID 一致 + 可解析 + 步骤数）
    try:
        read_back = json.loads(steps_path.read_text(encoding="utf-8"))
        json_ok = (
            read_back.get("run_id") == run_id
            and isinstance(read_back.get("steps"), list)
            and len(read_back.get("steps")) == len(steps_snapshot)
        )
    except Exception:  # noqa: BLE001 - JSON 解析失败
        json_ok = False
    add_step("证据合同复验（JSON：RUN_ID 一致 + 可解析 + 步骤数）", json_ok)

    # 阶段 3：写 meta（用当前 CHECKS/finalized_failed）
    finalized_failed = [n for n, ok in CHECKS if not ok]
    try:
        _dump_meta(finalized_failed)
    except Exception as exc:  # noqa: BLE001
        _write_emergency_evidence(base, run_id, exec_time, f"meta_write_failed:{type(exc).__name__}")
        return 2

    # 阶段 4：复验 meta（读回：RUN_ID 落盘可读）
    try:
        meta_ok = f"RUN_ID={run_id}" in meta_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        meta_ok = False
    add_step("证据合同复验（meta：RUN_ID 落盘可读）", meta_ok)

    # 阶段 5：用最终 STEPS/CHECKS 一次性重写 JSON + meta（最终一致性）
    final_snapshot = list(STEPS)
    finalized_failed = [n for n, ok in CHECKS if not ok]
    try:
        _dump_steps(final_snapshot)
        _dump_meta(finalized_failed)
    except Exception as exc:  # noqa: BLE001
        _write_emergency_evidence(base, run_id, exec_time, f"finalize_write_failed:{type(exc).__name__}")
        return 2

    return 2 if finalized_failed else 0


def archive_manifests(base: Path, tmp: Path) -> None:
    """归档 backfill 运行清单到证据目录（cleanup 删除 tmp 前必须归档，否则审计产物丢失）。"""
    mf_dir = tmp / "mf"
    if not mf_dir.exists():
        add_step("归档 backfill manifest", False, "manifest 目录不存在")
        return
    dest = base / "manifests"
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for p in sorted(mf_dir.glob("*.json")):
        shutil.copy2(p, dest / p.name)
        count += 1
    add_step("归档 backfill manifest", count >= 1, f"files={count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 非空 disposable 全链路（真实 PG/Neo4j/Milvus）")
    parser.add_argument("--dry-run", action="store_true", help="打印计划，零连接零写入（默认）")
    parser.add_argument("--authorize", metavar="TOKEN", help=f"执行需传入 {AUTHORIZE_TOKEN}")
    parser.add_argument("--keep-containers", action="store_true", help="保留一次性容器（默认销毁）")
    parser.add_argument("--evidence-dir", default=r"E:\projects\gate0-evidence\2026-10-06\nonempty-disposable",
                        help="证据落盘目录")
    args = parser.parse_args()

    if args.authorize != AUTHORIZE_TOKEN:
        print_plan()
        return 3

    base = Path(args.evidence_dir)
    now = datetime.now()
    run_id = "RUN-" + now.strftime("%Y%m%dT%H%M%S") + "-nonempty-disposable"
    exec_time = now.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = base / ("tmp-" + run_id.split("-")[1])
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    kb = "kb-nonempty-" + uuid.uuid4().hex[:8]
    doc = "d1"
    # 两组独立数据：S1 组（s1_chunk，有 current revision）+ backfill 组（bf_chunk，无 current revision）。
    s1_chunk = "s1c1"
    bf_chunk = "bfc1"

    exit_code = 0
    try:
        say(f"[RUN] {run_id} 开始（真实三引擎 disposable）")
        # 预检：同名对象必须不存在（枚举失败抛异常 → except 记入失败证据）
        names = _docker_names(all=True)
        nets = _docker_networks()
        vols = _docker_volumes()
        occupied = ([n for n in CONTAINERS if n in names]
                    + ([NET] if NET in nets else [])
                    + [v for v in VOLUMES if v in vols])
        if occupied:
            add_step("预检：同名一次性对象不存在（拒绝执行）", False, str(occupied))
            exit_code = 2
        else:
            add_step("预检：同名一次性对象不存在", True)
            ensure_containers()
            migrate_tables(tmp)
            # S1 组：s1_chunk（有 current revision）→ online_s1 双写
            seed_s1(tmp, kb, s1_chunk, doc)
            verify_s1_dual_write(tmp, kb, doc, s1_chunk)
            # backfill 组：bf_chunk（无 current revision，primary 预置向量）→ backfill_versioned
            seed_backfill(tmp, kb, bf_chunk, doc)
            verify_backfill(tmp, kb, bf_chunk)
            verify_readback(tmp, kb, s1_chunk, bf_chunk)

            with pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM chunk_revisions WHERE kb_id=%s", (kb,))
                    before = cur.fetchone()[0]
            inject_failure(tmp, kb, before)
            verify_idempotent(tmp, kb, before)
            verify_rollback(tmp, kb)
    except Exception as exc:  # noqa: BLE001 - 异常必须进入失败证据，不得静默吞掉
        add_step("执行异常（进入失败证据）", False, f"{type(exc).__name__}: {exc}")
        exit_code = 2
    finally:
        # 独立保护结构：任一步失败都记录失败、继续后续步骤，异常不覆盖最终失败证据。
        try:
            archive_manifests(base, tmp)
        except Exception as exc:  # noqa: BLE001
            add_step("归档 backfill manifest（异常）", False, f"{type(exc).__name__}: {exc}")
        try:
            cleanup(tmp, kb, args.keep_containers)
        except Exception as exc:  # noqa: BLE001
            add_step("清理（异常）", False, f"{type(exc).__name__}: {exc}")
        try:
            evidence_exit = write_evidence(base, run_id, exec_time)
            if evidence_exit:
                exit_code = 2
        except Exception as exc:  # noqa: BLE001
            add_step("证据落盘（异常）", False, f"{type(exc).__name__}: {exc}")
            exit_code = 2

    failed = [n for n, ok in CHECKS if not ok]
    say("=" * 72)
    say(f"SUMMARY checks={len(CHECKS)} failed={len(failed)}")
    if failed:
        for name in failed:
            say("  FAILED: " + name)
        exit_code = 2
    say("=" * 72)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
