#!/usr/bin/env python3
"""共享目标 KB 的 **v2-only 受控数据准备**（`SEED-SHARED`）。

口径与 `docs/ENTERPRISE_M5_MILVUS_V3_MIGRATION_PLAN.md` §三（2026-10-07 修正版）一致：
  **唯一写入 = Milvus v2 primary 向量**（deterministic 向量，dim 取自 collection 实际
  schema）。**不播种** PG `chunk_revisions` current 行、**不播种** Neo4j `(:Chunk)` ——
  两者由 `backfill_versioned` 在读源归属核验通过后创建（预播种 current 行会使后续
  回填被 `rows_skipped_existing` 跳过）。write scope 只输出**提案**，应用需单独
  CONFIG-SHARED 授权（脚本内无修改共享配置的入口）。

运行模式（互斥）：
  1. plan（默认，无 `--authorize`）：**离线 dry-run** —— 解析产物逐行归属校验 +
     生成 seed/rollback manifest + 打印准备清单。零网络、零共享写入，exit 3。
     `--probe` 追加只读实时预检（PG KB 行 + Milvus collection/dim/占用），仍零写入。
  2. execute（`--authorize SEED-SHARED-AUTHORIZED`）：执行 v2 向量写入。前置全部
     fail-closed（KB 登记/active/tenant/project、collection 存在/核心字段/dim、
     逐 chunk PK 占用冲突），写后**逐 chunk 读回校验**；任一步失败 exit 2。
  3. rollback plan（`--rollback <run.json>`，无 `--authorize`）：打印将删除的键与
     删除判据，exit 3（零删除）。
  4. rollback execute（`--rollback <run.json> --authorize ...`）：**全有或全无**删除
     —— 先逐行核验"与 run manifest 完全一致（六字段 + 确定性向量）"，任一行不一致
     （外域覆盖 / 已变更）即整体拒绝、零删除 exit 2；manifest 键已不存在视为已回滚
     （幂等重跑）。

回滚合同：授权运行把实际写入的全部键（chunk_id / doc_id / kb / tenant / project /
text / 向量 digest）记入 run manifest；回滚只允许删除"与 manifest 完全一致"的行，
删除后读回确认为零。确定性向量可由 text 重算，因此 manifest 无需存全量向量。

PK 冲突防覆盖：chunk_id 是 v2 主键，upsert 按 PK 覆盖。占用检测**只按 chunk_id 查询**
（不带 kb/tenant 过滤）——任何属性不一致（含跨 KB / 跨 tenant 同键）都判 CONFLICT，
绝不静默覆盖外域数据；删除时 filter 仍叠加 kb/tenant/project 守卫。

硬禁止（脚本内无入口）：建 v3 集合、回填写入、入队、修改共享配置、push、删除
manifest 之外的任何数据。

退出码：0 成功（含幂等 no-op）；2 fail-closed（零写入或零删除）；3 仅方案/计划。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

AUTHORIZE_TOKEN = "SEED-SHARED-AUTHORIZED"
DEFAULT_PARSED_ROOT = "E:/projects/GraphInsight/backend/parsed_documents"
# 严禁 19530：属其它项目（data-stellarls-milvus-1）。默认走宿主映射端口 19531。
DEFAULT_MILVUS_URI = "http://127.0.0.1:19531"
DEFAULT_COLLECTION = "graphinsight_chunks_v2"
DEFAULT_PG_CONTAINER = "graphinsight-postgres"

PLAN_SCHEMA = "seed-shared-plan-v2"
RUN_SCHEMA = "seed-shared-run-v1"
ROLLBACK_SCHEMA = "seed-shared-rollback-v1"

# v2 行核心字段（与 MilvusVectorStore schema 的身份/作用域/内容字段一致；
# 向量字段单独处理，schema 里其余扩展字段一律不写，避免对共享 schema 做隐式假设）。
ROW_FIELDS = ("chunk_id", "doc_id", "kb_id", "tenant_id", "project_id", "text")
QUERY_FIELDS = list(ROW_FIELDS) + ["vector"]

VECTOR_TOLERANCE = 1e-5
BATCH = 200

EXIT_OK = 0
EXIT_FAIL_CLOSED = 2
EXIT_PLAN_ONLY = 3

# 安全标识符：单段路径 + Milvus filter 字面量安全（无引号/空白/分隔符），防注入与路径逃逸。
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_id(value: Any) -> Optional[str]:
    """校验 kb/doc/chunk 标识符：非空、单段、filter/路径安全；不合法返回 None。"""
    v = str(value if value is not None else "").strip()
    if not v or not _SAFE_ID_RE.match(v):
        return None
    return v


def deterministic_vector(text: str, dim: int) -> List[float]:
    """确定性本地测试向量（与 disposable 流程 `_det` 同式）：sha256 → [-1,1]，6 位小数。

    仅在本受控脚本内使用，禁止移入生产模块（`check_m5_disposable_regressions.py` R2）。
    """
    d = hashlib.sha256((text or "").encode("utf-8")).digest()
    return [round(((d[i % len(d)] / 255.0) - 0.5) * 2.0, 6) for i in range(int(dim))]


def vector_digest(vec: Sequence[float]) -> str:
    """向量的稳定 digest（manifest 记录用；6 位小数 float 的 JSON 序列化是确定的）。"""
    return hashlib.sha256(
        json.dumps([round(float(x), 6) for x in vec], separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 归属校验（纯函数，可测）
def collect_parsed(kb: str, root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """只读枚举 `parsed_documents/<kb>/<doc>/chunks.jsonl` 并做**逐行归属校验**。

    返回 (rows, issues, stats)。归属规则（任一不满足即计入 issues 且该行不采用）：
      - doc 目录名必须为安全标识符；行内 doc_id（若提供）必须与目录名一致；
      - 行内 kb_id（若提供）必须等于目标 KB；
      - chunk_id / doc_id 必须为安全标识符（防 Milvus filter 注入与路径逃逸）；
      - JSON 不可解析、chunk_id 缺失、text 缺失或为空 → 无效行；
      - chunk_id 跨文档重复 → 无效（upsert 按 PK 覆盖，重复键绝不允许）。
    """
    issues: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    stats = {"doc_count": 0, "chunks_total": 0, "invalid_lines": 0}

    kb_checked = safe_id(kb)
    if kb_checked is None:
        return rows, [{"scope": "kb", "reason": f"kb id 非法或不安全: {kb!r}"}], stats
    if not root.is_dir():
        return rows, [{"scope": "root", "reason": f"root missing: {root}"}], stats
    kb_dir = root / kb_checked
    if not kb_dir.is_dir():
        return rows, [{"scope": "kb", "reason": f"kb dir missing: {kb_dir}"}], stats

    for doc_dir in sorted(p for p in kb_dir.iterdir() if p.is_dir()):
        chunks_path = doc_dir / "chunks.jsonl"
        if not chunks_path.is_file():
            continue
        stats["doc_count"] += 1
        dir_doc_id = safe_id(doc_dir.name)
        if dir_doc_id is None:
            issues.append({"doc": doc_dir.name, "reason": "doc 目录名不是安全标识符"})
            continue
        for lineno, line in enumerate(chunks_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                stats["invalid_lines"] += 1
                issues.append({"doc": doc_dir.name, "line": lineno,
                               "reason": f"json 不可解析: {str(exc)[:120]}"})
                continue
            if not isinstance(obj, dict):
                stats["invalid_lines"] += 1
                issues.append({"doc": doc_dir.name, "line": lineno, "reason": "行不是 JSON 对象"})
                continue
            chunk_id = safe_id(obj.get("chunk_id"))
            doc_id = obj.get("doc_id")
            doc_id = dir_doc_id if doc_id in (None, "") else safe_id(doc_id)
            text = obj.get("text")
            if chunk_id is None:
                stats["invalid_lines"] += 1
                issues.append({"doc": doc_dir.name, "line": lineno, "reason": "chunk_id 缺失或不安全"})
                continue
            if doc_id is None:
                stats["invalid_lines"] += 1
                issues.append({"doc": doc_dir.name, "line": lineno, "chunk_id": chunk_id,
                               "reason": "doc_id 缺失或不安全（须与目录名一致）"})
                continue
            if doc_id != dir_doc_id:
                stats["invalid_lines"] += 1
                issues.append({"doc": doc_dir.name, "line": lineno, "chunk_id": chunk_id,
                               "reason": f"doc_id 与目录名不一致: {doc_id!r} != {dir_doc_id!r}"})
                continue
            line_kb = obj.get("kb_id")
            if line_kb not in (None, "") and str(line_kb) != kb_checked:
                stats["invalid_lines"] += 1
                issues.append({"doc": doc_dir.name, "line": lineno, "chunk_id": chunk_id,
                               "reason": f"行内 kb_id 与目标 KB 不一致: {line_kb!r}"})
                continue
            if not isinstance(text, str) or not text.strip():
                stats["invalid_lines"] += 1
                issues.append({"doc": doc_dir.name, "line": lineno, "chunk_id": chunk_id,
                               "reason": "text 缺失或为空"})
                continue
            if chunk_id in seen:
                stats["invalid_lines"] += 1
                issues.append({"doc": doc_dir.name, "line": lineno, "chunk_id": chunk_id,
                               "reason": f"chunk_id 重复（首见于 {seen[chunk_id]}）"})
                continue
            seen[chunk_id] = doc_dir.name
            stats["chunks_total"] += 1
            rows.append({"chunk_id": chunk_id, "doc_id": doc_id, "kb_id": kb_checked,
                         "tenant_id": "", "project_id": "", "text": text,
                         "text_sha256": text_sha256(text)})
    if not rows and not issues:
        issues.append({"scope": "kb", "reason": "解析产物为空（0 条有效 chunk），拒绝授权执行"})
    return rows, issues, stats


def classify_kb_row(rc: int, out: str) -> Dict[str, Any]:
    """解析 `psql -t -A` 的 knowledge_bases 单行（tenant|project|status）。

    rc != 0 → UNVERIFIED；0 行 → BLOCKED（未登记）；多行/列数异常 → UNVERIFIED；
    status != active、tenant/project 为空 → BLOCKED（归属信息不可用）。
    """
    if rc != 0:
        return {"status": "UNVERIFIED", "detail": (out or "")[:300]}
    lines = [l for l in (out or "").splitlines() if l.strip()]
    if not lines:
        return {"status": "BLOCKED", "reason": "kb_not_registered"}
    if len(lines) > 1:
        return {"status": "UNVERIFIED", "detail": "kb id 命中多行（主键异常），拒绝判定"}
    parts = lines[0].split("|")
    if len(parts) != 3:
        return {"status": "UNVERIFIED", "detail": f"unexpected row shape: {lines[0][:120]}"}
    tenant, project, status = (p.strip() for p in parts)
    if status != "active":
        return {"status": "BLOCKED", "reason": f"kb_status={status or '(empty)'}"}
    if not tenant or not project:
        return {"status": "BLOCKED", "reason": "kb_tenant_or_project_missing"}
    return {"status": "OK", "tenant_id": tenant, "project_id": project, "kb_status": status}


def parse_collection_schema(desc: Any) -> Tuple[Optional[int], List[str]]:
    """从 describe_collection 结果解析向量 dim 与字段名（解析不出 → dim=None）。"""
    if not isinstance(desc, dict):
        return None, []
    names: List[str] = []
    dim: Optional[int] = None
    for f in desc.get("fields") or []:
        if not isinstance(f, dict):
            continue
        names.append(str(f.get("name") or ""))
        if f.get("name") == "vector":
            try:
                dim = int((f.get("params") or {}).get("dim"))
            except (TypeError, ValueError):
                dim = None
    return dim, names


def vector_equal(a: Any, b: Sequence[float], tol: float = VECTOR_TOLERANCE) -> bool:
    if not isinstance(a, (list, tuple)) or len(a) != len(b):
        return False
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))


def row_matches(row: Dict[str, Any], live: Optional[Dict[str, Any]], dim: int) -> bool:
    """live 行与期望行是否完全一致（六字段 + 确定性向量）。live 缺失 → False。"""
    if not isinstance(live, dict):
        return False
    for f in ROW_FIELDS:
        if str(live.get(f) if live.get(f) is not None else "") != str(row.get(f) or ""):
            return False
    return vector_equal(live.get("vector"), deterministic_vector(row["text"], dim))


def plan_execution(rows: List[Dict[str, Any]], existing: Dict[str, Dict[str, Any]],
                   dim: int) -> Dict[str, Any]:
    """逐 chunk 决定 写 / 跳过 / 拒绝（fail-closed，绝不覆盖外域数据）。

    - 占用查询按 chunk_id（PK）且不带 kb/tenant 过滤 → 任何属性不一致（含跨 KB /
      跨 tenant 同键）都判 CONFLICT；
    - 完全一致（六字段 + 确定性向量）→ skip（幂等重跑）；
    - 不存在 → write。
    """
    to_write, skipped, conflicts = [], [], []
    for row in rows:
        live = existing.get(row["chunk_id"])
        if live is None:
            to_write.append(row)
        elif row_matches(row, live, dim):
            skipped.append(row["chunk_id"])
        else:
            conflicts.append({"chunk_id": row["chunk_id"],
                              "reason": "PK 已被占用且与 seed 期望不一致（不覆盖）"})
    return {"to_write": to_write, "skipped": skipped, "conflicts": conflicts}


def verify_readback(live_map: Dict[str, Dict[str, Any]], rows: List[Dict[str, Any]],
                    dim: int) -> List[Dict[str, Any]]:
    """写后读回逐 chunk 校验；返回不一致清单（为空 = 全部一致）。"""
    bad = []
    for row in rows:
        live = live_map.get(row["chunk_id"])
        if live is None:
            bad.append({"chunk_id": row["chunk_id"], "reason": "读回缺失"})
        elif not row_matches(row, live, dim):
            bad.append({"chunk_id": row["chunk_id"], "reason": "读回字段/向量与期望不一致"})
    return bad


def plan_rollback(manifest_rows: List[Dict[str, Any]], live_map: Dict[str, Optional[Dict[str, Any]]],
                  dim: int) -> Dict[str, Any]:
    """回滚判定：deletable（与 manifest 完全一致）/ already_absent（幂等）/ conflicts。

    **全有或全无**：调用方必须在 conflicts 非空时拒绝删除任何键。
    """
    deletable, absent, conflicts = [], [], []
    for row in manifest_rows:
        live = live_map.get(row["chunk_id"])
        if live is None:
            absent.append(row["chunk_id"])
        elif row_matches(row, live, dim):
            deletable.append(row["chunk_id"])
        else:
            conflicts.append({"chunk_id": row["chunk_id"],
                              "reason": "现值与 run manifest 不一致（疑似外域覆盖/已变更），拒绝删除"})
    return {"deletable": deletable, "already_absent": absent, "conflicts": conflicts}


# ---------------------------------------------------------------- 存取适配（懒触发，离线不依赖）
def _run(cmd: List[str], stdin: Optional[str] = None, timeout: int = 120) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def pg_query_kb(kb: str, container: str) -> Tuple[int, str]:
    """只读查询 knowledge_bases（docker exec 容器内 psql，信任本地 socket，无密码入参）。"""
    sql = ("SELECT tenant_id, project_id, status FROM knowledge_bases "
           f"WHERE id = '{kb}';")
    return _run(["docker", "exec", container, "psql", "-U", "graphinsight",
                 "-d", "graphinsight_admin", "-t", "-A", "-c", sql])


class MilvusAdapter:
    """pymilvus 薄封装。**懒导入**：离线 dry-run 与离线测试不会触碰 pymilvus/网络。"""

    def __init__(self, uri: str, timeout: int = 30):
        from pymilvus import MilvusClient  # 延迟导入：离线路径零依赖
        self._client = MilvusClient(uri=uri, timeout=timeout)

    def has_collection(self, name: str) -> bool:
        return bool(self._client.has_collection(name))

    def describe(self, name: str) -> Dict[str, Any]:
        return self._client.describe_collection(name)

    def load(self, name: str) -> None:
        self._client.load_collection(name)

    def query(self, name: str, filter_expr: str, output_fields: List[str],
              limit: int = BATCH) -> List[Dict[str, Any]]:
        return list(self._client.query(collection_name=name, filter=filter_expr,
                                       output_fields=output_fields, limit=limit) or [])

    def upsert(self, name: str, rows: List[Dict[str, Any]]) -> Any:
        return self._client.upsert(collection_name=name, data=rows)

    def delete(self, name: str, filter_expr: str) -> Any:
        return self._client.delete(collection_name=name, filter=filter_expr)


def _open_milvus(uri: str) -> MilvusAdapter:
    """可注入的适配器工厂（离线测试替换为假体，绝不连接真实 Milvus）。"""
    return MilvusAdapter(uri)


def pk_filter(ids: List[str]) -> str:
    """仅按 chunk_id（PK）过滤的 Milvus 表达式（ids 已过 safe_id，无注入面）。"""
    quoted = ", ".join(f'"{i}"' for i in ids)
    return f"chunk_id in [{quoted}]"


def guarded_delete_filter(ids: List[str], kb: str, tenant: str, project: str) -> str:
    """删除表达式：PK 命中 + kb/tenant/project 守卫（纵深防御，绝不越界删除）。"""
    expr = f"{pk_filter(ids)} and kb_id == \"{kb}\""
    if tenant:
        expr += f" and tenant_id == \"{tenant}\""
    if project:
        expr += f" and project_id == \"{project}\""
    return expr


def batched(items: List[str], size: int = BATCH) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


def query_live_rows(mv: MilvusAdapter, collection: str,
                    ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """按 PK 分批查询现有行（**不带 kb/tenant 过滤**，暴露一切 PK 冲突）。"""
    live: Dict[str, Dict[str, Any]] = {}
    for part in batched(ids):
        for row in mv.query(collection, pk_filter(part), QUERY_FIELDS, limit=max(BATCH, len(part))):
            cid = str(row.get("chunk_id") or "")
            if cid:
                live[cid] = row
    return live


# ---------------------------------------------------------------- 主流程
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _fail_record(out_dir: Path, *, mode: str, kb: str, step: str, detail: Any,
                 executed: bool = False, rows: Optional[List[Dict[str, Any]]] = None) -> None:
    """fail-closed 留痕：尽力写本地 record（写失败不影响退出码判定）。"""
    record: Dict[str, Any] = {
        "schema": RUN_SCHEMA, "mode": mode, "generated_at": _now(), "kb_id": kb,
        "executed": executed, "status": "FAIL_CLOSED", "failed_step": step,
        "detail": detail,
    }
    if rows is not None:
        # 已发生写入时的失败：manifest 必须带全量键，供回滚使用。
        record["rows"] = rows
    try:
        _write_json(out_dir / "seed-shared-run.json", record)
    except Exception:  # noqa: BLE001
        pass


def _print_plan(plan: Dict[str, Any], out_file: Path) -> None:
    v = plan["validation"]
    print("SEED-SHARED v2-only 数据准备方案（**仅方案，未执行任何共享写入**）")
    print(f"  目标 KB    : {plan['kb_id']}")
    print(f"  解析产物根 : {plan['parsed_root']}")
    print(f"  Milvus     : {plan['milvus_uri']} / {plan['collection']}")
    print(f"  解析产物   : {v['doc_count']} 个文档目录 / {v['chunks_total']} 条有效 chunk"
          f" / {v['invalid_lines']} 条无效行")
    print(f"  归属校验   : {'OK' if v['ok'] else 'BLOCKED（存在未解决问题，不得授权执行）'}")
    for issue in v["issues"][:10]:
        print(f"    - {issue}")
    if plan.get("preflight"):
        print(f"  实时预检   : {json.dumps(plan['preflight'], ensure_ascii=False)[:300]}")
    print()
    print("  授权阶梯：")
    for line in plan["authorization_ladder"]:
        print(f"    {line}")
    print()
    print(f"  归档：{out_file}")


def _write_scope_proposal(kb: str, tenant: str, project: str) -> Dict[str, Any]:
    """write scope **提案**（脚本不改共享配置；应用需 CONFIG-SHARED 单独授权）。"""
    return {
        "MILVUS_WRITE_SCOPE_KB_IDS": kb,
        "MILVUS_WRITE_SCOPE_TENANT": tenant or "(以 PG 核验结果为准)",
        "MILVUS_WRITE_SCOPE_PROJECT": project or "(以 PG 核验结果为准)",
        "note": "应用需 CONFIG-SHARED 授权；本脚本内无修改共享配置的入口",
    }


AUTHORIZATION_LADDER = [
    "阶段 1（本阶段，默认）：离线 dry-run（零写入）→ 产出 seed/rollback manifest",
    "阶段 2（SEED-SHARED 授权后）：仅写入 Milvus v2 primary 向量 + 读回校验",
    "阶段 3（数据准备完成后）：申请 CONFIG-SHARED（应用 write scope 提案 + vector 能力配置）",
    "阶段 4：申请 CREATE-V3（显式 INT64 schema + 向量索引）",
    "阶段 5（最后）：申请 BACKFILL-WRITE（单向回填只补 v3；PG current/Neo4j 由回填创建）",
]


def run_plan(args: argparse.Namespace) -> int:
    """模式 1：离线 dry-run（默认）。零网络、零共享写入，exit 3。"""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = Path(args.parsed_root)

    kb = safe_id(args.kb)
    rows: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    stats: Dict[str, int] = {"docs": 0, "chunks_total": 0, "invalid_lines": 0}
    if kb is None:
        issues.append({"scope": "kb", "reason": f"kb id 非法或不安全: {args.kb!r}"})
    else:
        rows, issues, stats = collect_parsed(kb, root)

    preflight: Dict[str, Any] = {}
    if args.probe and kb is not None and not issues:
        # 只读实时预检（PG KB 行 + Milvus collection/dim/占用），任一异常 → UNVERIFIED。
        try:
            preflight["pg"] = classify_kb_row(*pg_query_kb(kb, args.pg_container))
        except Exception as exc:  # noqa: BLE001
            preflight["pg"] = {"status": "UNVERIFIED", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        try:
            mv = _open_milvus(args.milvus_uri)
            if not mv.has_collection(args.collection):
                preflight["milvus"] = {"status": "BLOCKED", "reason": f"collection 不存在: {args.collection}"}
            else:
                dim, fields = parse_collection_schema(mv.describe(args.collection))
                live = query_live_rows(mv, args.collection, [r["chunk_id"] for r in rows])
                preflight["milvus"] = {"status": "OK", "dim": dim, "fields": fields,
                                       "occupied_chunk_ids": sorted(live)}
                if dim is None:
                    preflight["milvus"] = {"status": "UNVERIFIED", "detail": "无法解析 vector dim"}
        except Exception as exc:  # noqa: BLE001
            preflight["milvus"] = {"status": "UNVERIFIED", "detail": f"{type(exc).__name__}: {exc}"[:300]}

    pg_probe = preflight.get("pg") or {}
    plan: Dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "generated_at": _now(),
        "kb_id": kb or args.kb,
        "parsed_root": str(root),
        "milvus_uri": args.milvus_uri,
        "collection": args.collection,
        "validation": {"ok": not issues, "issues": issues, **stats},
        "rows_preview": [
            {k: r[k] for k in ("chunk_id", "doc_id", "kb_id", "text_sha256")}
            for r in rows
        ],
        "preflight": preflight or {"status": "SKIPPED", "detail": "未加 --probe，未做实时预检"},
        "write_scope_proposal": _write_scope_proposal(
            kb or args.kb,
            str(pg_probe.get("tenant_id") or "") if pg_probe.get("status") == "OK" else "",
            str(pg_probe.get("project_id") or "") if pg_probe.get("status") == "OK" else "",
        ),
        "authorization_ladder": AUTHORIZATION_LADDER,
        "rollback_contract": {
            "manifest_file": "seed-shared-run.json",
            "criteria": [
                "仅删除与 run manifest 完全一致的行（六字段 + 确定性向量）",
                "任一行不一致（外域覆盖/已变更）→ 整体拒绝、零删除",
                "manifest 键已不存在 → 视为已回滚（幂等）",
            ],
        },
        "executed": False,
        "result": "PLAN_ONLY",
    }
    out_file = out_dir / "seed-shared-plan.json"
    _write_json(out_file, plan)
    _print_plan(plan, out_file)
    return EXIT_PLAN_ONLY


def run_execute(args: argparse.Namespace) -> int:
    """模式 2：授权执行（仅 v2 向量写入）。前置全 fail-closed + 写后读回。"""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    kb = safe_id(args.kb)
    if kb is None:
        _fail_record(out_dir, mode="seed", kb=args.kb, step="kb_id",
                     detail=f"kb id 非法或不安全: {args.kb!r}")
        print(f"✗ fail-closed: kb id 非法或不安全: {args.kb!r}", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    def fail(step: str, detail: Any) -> int:
        _fail_record(out_dir, mode="seed", kb=kb, step=step, detail=detail)
        print(f"✗ fail-closed @ {step}: {json.dumps(detail, ensure_ascii=False)[:400]}",
              file=sys.stderr)
        return EXIT_FAIL_CLOSED

    # ① 解析产物逐行归属校验（fail-closed：任何无效行都阻止授权执行）
    rows, issues, stats = collect_parsed(kb, Path(args.parsed_root))
    if issues:
        return fail("collect_parsed", {"issues": issues[:20], **stats})
    if not rows:
        return fail("collect_parsed", {"reason": "无有效 chunk", **stats})

    # ② PG knowledge_bases 归属核验（登记 + active + tenant/project）
    pg = classify_kb_row(*pg_query_kb(kb, args.pg_container))
    if pg.get("status") != "OK":
        return fail("pg_kb_check", pg)
    tenant, project = pg["tenant_id"], pg["project_id"]
    for r in rows:
        r["tenant_id"], r["project_id"] = tenant, project

    # ③ Milvus collection 前置核验（存在 + 核心字段 + dim）
    try:
        mv = _open_milvus(args.milvus_uri)
    except Exception as exc:  # noqa: BLE001
        return fail("milvus_connect", f"{type(exc).__name__}: {exc}"[:300])
    if not mv.has_collection(args.collection):
        return fail("milvus_collection", {"reason": f"collection 不存在: {args.collection}"})
    dim, fields = parse_collection_schema(mv.describe(args.collection))
    missing = [f for f in ROW_FIELDS + ("vector",) if f not in fields]
    if dim is None:
        return fail("milvus_schema", {"reason": "无法解析 vector dim", "fields": fields})
    if missing:
        return fail("milvus_schema", {"reason": f"collection 缺核心字段: {missing}", "fields": fields})
    try:
        mv.load(args.collection)
    except Exception as exc:  # noqa: BLE001
        return fail("milvus_load", f"{type(exc).__name__}: {exc}"[:300]
                    + "（提示：缺向量索引时 load 报 index not found）")

    # ④ PK 占用冲突检测（只按 chunk_id 查，暴露一切跨 KB/tenant 同键）
    try:
        existing = query_live_rows(mv, args.collection, [r["chunk_id"] for r in rows])
    except Exception as exc:  # noqa: BLE001
        return fail("milvus_query", f"{type(exc).__name__}: {exc}"[:300])
    decision = plan_execution(rows, existing, dim)
    if decision["conflicts"]:
        return fail("conflict_check", {"conflicts": decision["conflicts"][:20]})

    # ⑤ 写入 v2（仅待写行）+ ⑥ 读回校验（覆盖写入行与幂等跳过行的全集）
    to_write = decision["to_write"]
    written_keys: List[str] = []
    try:
        for part in batched([r["chunk_id"] for r in to_write]):
            if not part:
                continue
            data = [dict({f: r[f] for f in ROW_FIELDS},
                         vector=deterministic_vector(r["text"], dim))
                    for r in to_write if r["chunk_id"] in set(part)]
            mv.upsert(args.collection, data)
            written_keys.extend(r["chunk_id"] for r in data)
        live_map = query_live_rows(mv, args.collection, [r["chunk_id"] for r in rows])
    except Exception as exc:  # noqa: BLE001
        # 半截数据风险：manifest 带已写入键，供回滚；exit 2。
        manifest_rows = [_manifest_row(r, dim) for r in rows if r["chunk_id"] in set(written_keys)]
        _fail_record(out_dir, mode="seed", kb=kb, step="milvus_write_or_readback",
                     detail=f"{type(exc).__name__}: {exc}"[:300], executed=True,
                     rows=manifest_rows)
        print(f"✗ fail-closed @ milvus_write_or_readback: {exc}", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    bad = verify_readback(live_map, rows, dim)
    if bad:
        manifest_rows = [_manifest_row(r, dim) for r in rows if r["chunk_id"] in set(written_keys)]
        _fail_record(out_dir, mode="seed", kb=kb, step="verify_readback",
                     detail={"mismatch": bad[:20]}, executed=True, rows=manifest_rows)
        print(f"✗ fail-closed @ verify_readback: {bad[:5]}", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    record: Dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "mode": "seed",
        "generated_at": _now(),
        "kb_id": kb,
        "tenant_id": tenant,
        "project_id": project,
        "milvus_uri": args.milvus_uri,
        "collection": args.collection,
        "dim": dim,
        "rows": [_manifest_row(r, dim) for r in rows],
        "counts": {"planned": len(rows), "written": len(written_keys),
                   "already_seeded": len(decision["skipped"])},
        "write_scope_proposal": _write_scope_proposal(kb, tenant, project),
        "rollback": {
            "file_note": "用 --rollback <本文件> --authorize SEED-SHARED-AUTHORIZED 执行",
            "criteria": ["全有或全无", "仅删与 manifest 完全一致的行", "已不存在视为已回滚"],
        },
        "executed": True,
        "status": "OK",
        "result": "SEEDED" if written_keys else "ALREADY_SEEDED",
    }
    _write_json(out_dir / "seed-shared-run.json", record)
    print(f"✓ SEED-SHARED 完成：planned={len(rows)} written={len(written_keys)} "
          f"already_seeded={len(decision['skipped'])} dim={dim} "
          f"collection={args.collection}（v2-only，未触碰 PG/Neo4j/v3）")
    print(f"  归档：{out_dir / 'seed-shared-run.json'}（含回滚 manifest）")
    return EXIT_OK


def _manifest_row(row: Dict[str, Any], dim: Optional[int]) -> Dict[str, Any]:
    out = {k: row[k] for k in ("chunk_id", "doc_id", "kb_id", "tenant_id", "project_id", "text")}
    out["text_sha256"] = row.get("text_sha256") or text_sha256(row["text"])
    if dim is not None:
        out["vector_sha256"] = vector_digest(deterministic_vector(row["text"], dim))
    return out


def _load_run_manifest(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"manifest 不可读: {type(exc).__name__}: {exc}"[:300]
    if not isinstance(manifest, dict) or manifest.get("schema") != RUN_SCHEMA:
        return None, f"manifest schema 非法（期望 {RUN_SCHEMA}）"
    if manifest.get("executed") is not True:
        return None, "manifest executed != true（该次运行未发生写入，无需回滚）"
    rows = manifest.get("rows")
    if not isinstance(rows, list) or not rows:
        return None, "manifest rows 为空（无可回滚键）"
    for r in rows:
        if not isinstance(r, dict) or safe_id(r.get("chunk_id")) is None:
            return None, "manifest rows 含非法 chunk_id"
    # kb/tenant/project 会进入 Milvus delete filter —— 必须全部过安全校验（防篡改注入）。
    if safe_id(manifest.get("kb_id")) is None:
        return None, "manifest kb_id 非法或不安全"
    for f in ("tenant_id", "project_id"):
        v = str(manifest.get(f) or "")
        if v and safe_id(v) is None:
            return None, f"manifest {f} 非法或不安全"
    return manifest, ""


def run_rollback(args: argparse.Namespace) -> int:
    """模式 3/4：回滚计划（无授权，exit 3）/ 回滚执行（全有或全无）。"""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest, err = _load_run_manifest(Path(args.rollback))
    if manifest is None:
        print(f"✗ fail-closed: {err}", file=sys.stderr)
        return EXIT_FAIL_CLOSED
    kb = str(manifest["kb_id"])
    tenant = str(manifest.get("tenant_id") or "")
    project = str(manifest.get("project_id") or "")
    collection = str(manifest.get("collection") or "")
    dim = manifest.get("dim")
    manifest_rows: List[Dict[str, Any]] = [
        {f: str(r.get(f) or "") for f in ROW_FIELDS} for r in manifest["rows"]
    ]
    if args.kb and args.kb != kb:
        print(f"✗ fail-closed: --kb {args.kb!r} 与 manifest kb_id {kb!r} 不一致", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    if not args.authorize:
        # 模式 3：回滚计划（离线，零删除）。
        plan = {
            "schema": ROLLBACK_SCHEMA, "mode": "rollback-plan", "generated_at": _now(),
            "based_on": {"file": str(args.rollback), "schema": manifest.get("schema"),
                         "kb_id": kb, "collection": collection, "dim": dim},
            "keys": [r["chunk_id"] for r in manifest_rows],
            "criteria": [
                "全有或全无：任一行现值与 manifest 不一致即整体拒绝、零删除",
                "仅删与 manifest 完全一致的行（六字段 + 确定性向量）",
                "manifest 键已不存在 → 视为已回滚（幂等）",
                "删除 filter 叠加 kb/tenant/project 守卫",
            ],
            "executed": False, "result": "PLAN_ONLY",
        }
        out_file = out_dir / "seed-shared-rollback-plan.json"
        _write_json(out_file, plan)
        print("SEED-SHARED 回滚方案（**仅方案，未删除任何数据**）")
        print(f"  依据 manifest : {args.rollback}")
        print(f"  目标 KB       : {kb}")
        print(f"  collection    : {collection}")
        print(f"  将核验键数    : {len(manifest_rows)}")
        print(f"  归档：{out_file}")
        return EXIT_PLAN_ONLY

    # 模式 4：回滚执行（fail-closed + 全有或全无 + 幂等）。
    def fail(step: str, detail: Any) -> int:
        record = {"schema": ROLLBACK_SCHEMA, "mode": "rollback", "generated_at": _now(),
                  "kb_id": kb, "collection": collection, "executed": False,
                  "status": "FAIL_CLOSED", "failed_step": step, "detail": detail}
        try:
            _write_json(out_dir / "seed-shared-rollback.json", record)
        except Exception:  # noqa: BLE001
            pass
        print(f"✗ fail-closed @ {step}: {json.dumps(detail, ensure_ascii=False)[:400]}",
              file=sys.stderr)
        return EXIT_FAIL_CLOSED

    if not collection:
        return fail("manifest", {"reason": "manifest 缺 collection"})
    if not isinstance(dim, int):
        return fail("manifest", {"reason": f"manifest dim 非法: {dim!r}"})

    try:
        mv = _open_milvus(args.milvus_uri)
    except Exception as exc:  # noqa: BLE001
        return fail("milvus_connect", f"{type(exc).__name__}: {exc}"[:300])
    if not mv.has_collection(collection):
        return fail("milvus_collection", {"reason": f"collection 不存在: {collection}"})
    try:
        mv.load(collection)
        live_map: Dict[str, Optional[Dict[str, Any]]] = query_live_rows(
            mv, collection, [r["chunk_id"] for r in manifest_rows])
    except Exception as exc:  # noqa: BLE001
        return fail("milvus_query", f"{type(exc).__name__}: {exc}"[:300])

    decision = plan_rollback(manifest_rows, live_map, dim)
    if decision["conflicts"]:
        return fail("rollback_precheck", {"conflicts": decision["conflicts"][:20]})

    deletable = decision["deletable"]
    deleted: List[str] = []
    if deletable:
        try:
            for part in batched(deletable):
                mv.delete(collection, guarded_delete_filter(part, kb, tenant, project))
            after = query_live_rows(mv, collection, [r["chunk_id"] for r in manifest_rows])
        except Exception as exc:  # noqa: BLE001
            return fail("milvus_delete", f"{type(exc).__name__}: {exc}"[:300])
        remaining = [r["chunk_id"] for r in manifest_rows
                     if r["chunk_id"] in deletable and after.get(r["chunk_id"]) is not None]
        if remaining:
            return fail("rollback_verify", {"remaining_after_delete": remaining[:20]})
        deleted = deletable

    record = {
        "schema": ROLLBACK_SCHEMA, "mode": "rollback", "generated_at": _now(),
        "based_on": {"file": str(args.rollback), "schema": manifest.get("schema")},
        "kb_id": kb, "collection": collection,
        "counts": {"deleted": len(deleted),
                   "already_absent": len(decision["already_absent"])},
        "deleted_keys": deleted,
        "already_absent_keys": decision["already_absent"],
        "executed": True,
        "status": "OK",
        "result": "ROLLED_BACK" if deleted else "ALREADY_ROLLED_BACK",
    }
    _write_json(out_dir / "seed-shared-rollback.json", record)
    print(f"✓ 回滚完成：deleted={len(deleted)} already_absent={len(decision['already_absent'])} "
          f"collection={collection}（v2-only；PG/Neo4j/v3 未触碰）")
    print(f"  归档：{out_dir / 'seed-shared-rollback.json'}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SEED-SHARED v2-only 数据准备（默认离线 dry-run，零写入）")
    ap.add_argument("--kb", default="", help="目标 KB id（rollback 模式可省略；提供时必须与 manifest 一致）")
    ap.add_argument("--parsed-root", default=DEFAULT_PARSED_ROOT)
    ap.add_argument("--out", required=True, help="方案/manifest 归档目录（本地文件，非共享写入）")
    ap.add_argument("--authorize", default="", help=f"执行授权令牌（{AUTHORIZE_TOKEN}）；缺省为 dry-run")
    ap.add_argument("--rollback", default="", help="回滚模式：run manifest JSON 路径")
    ap.add_argument("--probe", action="store_true", help="plan 模式追加只读实时预检（PG/Milvus）")
    ap.add_argument("--milvus-uri",
                    default=os.environ.get("MILVUS_URI") or DEFAULT_MILVUS_URI)
    ap.add_argument("--collection",
                    default=os.environ.get("MILVUS_COLLECTION") or DEFAULT_COLLECTION)
    ap.add_argument("--pg-container", default=DEFAULT_PG_CONTAINER)
    args = ap.parse_args(argv)

    if args.rollback:
        return run_rollback(args)
    if not args.kb:
        ap.error("--kb 必填（rollback 模式可省略）")
    if args.authorize and args.authorize != AUTHORIZE_TOKEN:
        # 错误令牌绝不能静默降级为 dry-run：明确 fail-closed。
        print(f"✗ fail-closed: --authorize 令牌不正确（期望 {AUTHORIZE_TOKEN}），零写入",
              file=sys.stderr)
        return EXIT_FAIL_CLOSED
    if args.authorize:
        return run_execute(args)
    return run_plan(args)


if __name__ == "__main__":
    raise SystemExit(main())
