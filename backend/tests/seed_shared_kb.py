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

共享写目标**强制**：`--milvus-uri`/`--collection`（含 env 覆盖）统一预校验，仅接受
`http://127.0.0.1:19531` + `graphinsight_chunks_v2`（19530 属其它项目，严禁）。

回滚部分删除语义：删除按批次推进账本，批次中断即落 **PARTIAL** record（executed=true
+ deleted/pending 清单，读回为准），重跑同一 manifest 幂等收敛；绝不把部分删除谎报为
零删除 FAIL_CLOSED。

紧急证据链：manifest 落盘 = 主路径 → 同目录 `.emergency.<ts>.json` → 系统临时目录 →
stderr（BEGIN/END 包裹完整 JSON）；**每个候选写入后必须读回校验**（可读 + JSON 可解析
+ 内容一致），静默损坏视同写盘失败转入下一后备；manifest 写盘/校验失败的运行**不得
exit 0**（seed 成功但证据丢失 = exit 2，在 manifest 可用前不得进入后续阶段）。

P1 加固（2026-10-07 独立复核后）：① 授权令牌校验**先于一切模式分发**（错误令牌不得
进入回滚删除流程）；② 回滚 manifest 自带的 milvus_uri/collection 必须严格等于受控
v2 目标（篡改 manifest 不得把删除引向 v3/其它实例）；③ 可恢复写入账本——upsert 发出
即入账 dispatched，异常后读回区分 confirmed/unknown/pending，失败 manifest 收录
dispatched 期望行（超集收录安全）+ 完整运行上下文，保证"响应丢失已落库"的键可清理；
④ 写入所有权——成功 manifest 只收本次实际写入的 owned rows，already_seeded 行不入
回滚删除集合；⑤ manifest 落盘读回校验（见上）。

硬禁止（脚本内无入口）：建 v3 集合、回填写入、入队、修改共享配置、push、删除
manifest 之外的任何数据。

退出码：0 成功（含幂等 no-op）；2 fail-closed（零写入/零删除；或写入/删除已发生但
证据链降级、部分删除 PARTIAL、manifest 落盘失败——以 record 的 status/executed 为准）；
3 仅方案/计划。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

AUTHORIZE_TOKEN = "SEED-SHARED-AUTHORIZED"
DEFAULT_PARSED_ROOT = "E:/projects/GraphInsight/backend/parsed_documents"
# 共享写目标**强制**（validate_milvus_target，任何偏离 fail-closed）：
# 19530 属其它项目（data-stellarls-milvus-1），严禁；集合必须恰为 v2 primary。
ENFORCED_MILVUS_URI = "http://127.0.0.1:19531"
ENFORCED_COLLECTION = "graphinsight_chunks_v2"
# 兼容旧名（已改为强制口径，不再提供默认之外的自由度）。
DEFAULT_MILVUS_URI = ENFORCED_MILVUS_URI
DEFAULT_COLLECTION = ENFORCED_COLLECTION
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


def validate_milvus_target(uri: str, collection: str) -> Optional[str]:
    """共享写目标强制校验：仅 `http://127.0.0.1:19531` + `graphinsight_chunks_v2`。

    19530 属其它项目（data-stellarls-milvus-1），任何端口/主机/集合偏离都拒绝，
    防止受控写入落到错误实例或非 v2 集合（v3 / canary / 其它项目库）。返回 None = 通过。
    """
    try:
        parsed = urlparse(str(uri or "").strip())
        host = (parsed.hostname or "").strip()
        port = parsed.port  # 端口非法时 urlparse 抛 ValueError
    except ValueError:
        return f"milvus uri 无法解析: {uri!r}"
    if parsed.scheme not in ("http", "https"):
        return f"milvus uri scheme 非法（期望 http/https）: {uri!r}"
    if host != "127.0.0.1" or port != 19531:
        return (f"milvus uri 必须是 {ENFORCED_MILVUS_URI}（严禁 19530：属其它项目；"
                f"实际 host={host or '(empty)'} port={port}）: {uri!r}")
    if str(collection or "").strip() != ENFORCED_COLLECTION:
        return (f"collection 必须是 {ENFORCED_COLLECTION}（v2-only 硬约束，"
                f"禁 v3/canary/其它集合）: {collection!r}")
    return None


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


EMERGENCY_BEGIN = "-----BEGIN SEED-SHARED EMERGENCY MANIFEST-----"
EMERGENCY_END = "-----END SEED-SHARED EMERGENCY MANIFEST-----"


def _write_text(path: Path, text: str) -> None:
    """实际落盘点（唯一磁盘写入口，测试在此注入故障）。"""
    path.write_text(text, encoding="utf-8")


def _write_record(out_dir: Path, filename: str,
                  record: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    """manifest 落盘，带**紧急证据链**：主路径 → 同目录 `.emergency.<ts>.json` →
    系统临时目录；全部失败时把完整 JSON 包裹在 BEGIN/END 标记间打到 stderr。

    manifest 是回滚的唯一依据：落盘失败绝不允许静默吞掉（旧实现 `except: pass`
    会造成"已写入共享但无任何证据"的不可回滚状态）。
    返回 (实际落盘路径 | None, 各后备失败原因清单)。
    """
    payload = json.dumps(record, ensure_ascii=False, indent=1) + "\n"
    stem = Path(filename).stem
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidates = [
        out_dir / filename,
        out_dir / f"{stem}.emergency.{ts}.json",
        Path(tempfile.gettempdir()) / f"{stem}.emergency.{ts}.json",
    ]
    errors: List[str] = []
    for cand in candidates:
        try:
            cand.parent.mkdir(parents=True, exist_ok=True)
            _write_text(cand, payload)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{cand}: {type(exc).__name__}: {exc}"[:200])
            continue
        # P1-D：写后必须读回校验（可读 + JSON 可解析 + 内容一致）——静默损坏
        # （截断 / 半截写 / 被并发覆写）视同写盘失败，转入下一后备；全部失败走
        # stderr 紧急 dump。
        try:
            back = json.loads(cand.read_text(encoding="utf-8"))
            if back != record:
                errors.append(f"{cand}: 读回校验失败（内容与期望不一致/被截断）")
                continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{cand}: 读回校验异常 {type(exc).__name__}: {exc}"[:200])
            continue
        return str(cand), errors
    print(f"{EMERGENCY_BEGIN}\n{payload}{EMERGENCY_END}", file=sys.stderr)
    print("✗ 紧急证据：manifest 主路径与全部后备写盘均失败，完整 JSON 已输出到 stderr；"
          "无落盘 manifest 则无法回滚，请立即手工保存上述 JSON。", file=sys.stderr)
    return None, errors


def _announce_evidence(where: Optional[str], errors: List[str]) -> None:
    if where:
        print(f"  归档：{where}")
    else:
        for e in errors:
            print(f"  落盘失败：{e}", file=sys.stderr)


def _fail_record(out_dir: Path, *, mode: str, kb: str, step: str, detail: Any,
                 executed: bool = False, rows: Optional[List[Dict[str, Any]]] = None,
                 ctx: Optional[Dict[str, Any]] = None,
                 extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """fail-closed 留痕：走紧急证据链落盘，返回实际路径（None = 全部失败，已 dump stderr）。

    ctx 携带运行上下文（tenant_id/project_id/collection/dim/milvus_uri）：失败
    manifest 必须能被 `_load_run_manifest` 接受，否则"写后失败"的证据不可回滚
    （P1-B）。extra 合并附加字段（如 write_ledger）。
    """
    record: Dict[str, Any] = {
        "schema": RUN_SCHEMA, "mode": mode, "generated_at": _now(), "kb_id": kb,
        "executed": executed, "status": "FAIL_CLOSED", "failed_step": step,
        "detail": detail,
    }
    for k in ("tenant_id", "project_id", "collection", "dim", "milvus_uri"):
        if ctx and ctx.get(k) is not None:
            record[k] = ctx[k]
    if rows is not None:
        # 已发生（或可能已发生）写入时的失败：manifest 必须带 dispatched 期望行，
        # 供回滚清理（超集收录安全：未写入 → already_absent；被外域覆盖 → 拒删）。
        record["rows"] = rows
    if extra:
        record.update(extra)
    where, errors = _write_record(out_dir, "seed-shared-run.json", record)
    _announce_evidence(where, errors)
    return where


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
    # 提案里的 tenant/project 仅在 PG 核验 OK **且**过安全校验时回填。
    pg_ok = pg_probe.get("status") == "OK"
    prop_tenant = str(pg_probe.get("tenant_id") or "") if pg_ok and safe_id(pg_probe.get("tenant_id")) else ""
    prop_project = str(pg_probe.get("project_id") or "") if pg_ok and safe_id(pg_probe.get("project_id")) else ""
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
        "write_scope_proposal": _write_scope_proposal(kb or args.kb, prop_tenant, prop_project),
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
    where, errors = _write_record(out_dir, "seed-shared-plan.json", plan)
    if where is None or errors:
        # 主路径落盘失败 = 归档合同破坏：即便 emergency 后备接住也必须 exit 2，
        # 绝不允许 exit 3 假装"已归档"。
        print("✗ fail-closed: plan 落盘失败/降级（证据位置见下）", file=sys.stderr)
        _announce_evidence(where, errors)
        return EXIT_FAIL_CLOSED
    _print_plan(plan, Path(where))
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

    # 运行上下文（P1-B）：失败 manifest 必须带全，否则 _load_run_manifest 拒收、
    # "写后失败"的证据不可回滚。
    ctx: Dict[str, Any] = {"milvus_uri": args.milvus_uri, "collection": args.collection}

    def fail(step: str, detail: Any) -> int:
        _fail_record(out_dir, mode="seed", kb=kb, step=step, detail=detail, ctx=ctx)
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
    # tenant/project 会进入 v2 行数据、run manifest 与（回滚侧）delete filter ——
    # 必须过安全校验，PG 出现异常值时 fail-closed 而不是把脏值写进共享。
    if safe_id(tenant) is None or safe_id(project) is None:
        return fail("pg_kb_check", {"reason": "kb tenant/project 非安全标识符，拒绝写入",
                                    "tenant_id": tenant, "project_id": project})
    for r in rows:
        r["tenant_id"], r["project_id"] = tenant, project
    ctx["tenant_id"], ctx["project_id"] = tenant, project

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
    ctx["dim"] = dim
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

    # ⑤ 写入 v2（仅待写行，按批次推进**可恢复账本**）+ ⑥ 读回校验（全集）
    to_write = decision["to_write"]
    planned_keys = [r["chunk_id"] for r in rows]
    written_keys: List[str] = []
    dispatched: List[str] = []  # 已发出 upsert 的键（含响应丢失的批次——可能已部分落库）

    def write_fail(step: str, detail: Any,
                   live_map: Optional[Dict[str, Dict[str, Any]]]) -> int:
        """写后失败留痕（P1-B 可恢复账本）：
        - 读回区分 confirmed（在库且与期望一致 = 本次拥有）/ unknown（响应丢失、
          是否落库未知）/ pending（未发出）；
        - 失败 manifest 收录 **dispatched 批次的期望行**——回滚侧 row_matches 保证
          超集收录安全（未写入 → already_absent；被外域覆盖 → CONFLICT 拒删），
          杜绝"响应丢失导致漏记已写入键"；
        - 完整运行上下文 + 账本随 manifest 落盘。
        """
        confirmed: List[str] = []
        unknown: List[str] = list(dispatched)
        if live_map is None:
            try:
                live_map = query_live_rows(mv, args.collection, dispatched)
            except Exception:  # noqa: BLE001
                live_map = None
        if live_map is not None:
            matching = {r["chunk_id"] for r in to_write
                        if r["chunk_id"] in live_map
                        and row_matches(r, live_map[r["chunk_id"]], dim)}
            confirmed = [k for k in dispatched if k in matching]
            unknown = [k for k in dispatched if k not in matching]
        ledger = {"confirmed": confirmed, "unknown": unknown,
                  "pending": [k for k in planned_keys if k not in set(dispatched)]}
        manifest_rows = [_manifest_row(r, dim) for r in to_write
                         if r["chunk_id"] in set(dispatched)]
        _fail_record(out_dir, mode="seed", kb=kb, step=step, detail=detail, executed=True,
                     rows=manifest_rows, ctx=ctx, extra={"write_ledger": ledger})
        print(f"✗ fail-closed @ {step}: {json.dumps(detail, ensure_ascii=False)[:300]} "
              f"ledger confirmed={len(confirmed)} unknown={len(unknown)} "
              f"pending={len(ledger['pending'])}", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    try:
        for part in batched([r["chunk_id"] for r in to_write], BATCH):
            if not part:
                continue
            data = [dict({f: r[f] for f in ROW_FIELDS},
                         vector=deterministic_vector(r["text"], dim))
                    for r in to_write if r["chunk_id"] in set(part)]
            dispatched.extend(part)  # 发出即入账：响应丢失也可能已部分/全部落库
            mv.upsert(args.collection, data)
            written_keys.extend(r["chunk_id"] for r in data)
        live_map = query_live_rows(mv, args.collection, planned_keys)
    except Exception as exc:  # noqa: BLE001
        return write_fail("milvus_write_or_readback",
                          f"{type(exc).__name__}: {exc}"[:300], None)

    bad = verify_readback(live_map, rows, dim)
    if bad:
        return write_fail("verify_readback", {"mismatch": bad[:20]}, live_map)

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
        # P1-C 写入所有权：rows 只含**本次运行实际写入**的 owned rows；
        # already_seeded（本次运行前已在库、内容一致的行）不属于本次运行，
        # 绝不进入回滚删除集合。
        "rows": [_manifest_row(r, dim) for r in to_write],
        "already_seeded_keys": list(decision["skipped"]),
        "ownership_note": ("rows 仅含本次运行实际写入的 owned rows；"
                           "already_seeded 行不属于本次运行，不入回滚删除集合"),
        "counts": {"planned": len(rows), "written": len(written_keys),
                   "already_seeded": len(decision["skipped"])},
        "write_scope_proposal": _write_scope_proposal(kb, tenant, project),
        "rollback": {
            "file_note": "用 --rollback <本文件> --authorize SEED-SHARED-AUTHORIZED 执行",
            "criteria": ["全有或全无", "仅删与 manifest 完全一致且属于本次运行的 owned rows",
                         "已不存在视为已回滚", "already_seeded 行不在本 manifest 中"],
        },
        "executed": True,
        "status": "OK",
        "result": "SEEDED" if written_keys else "ALREADY_SEEDED",
    }
    where, errors = _write_record(out_dir, "seed-shared-run.json", record)
    print(f"✓ SEED-SHARED 完成：planned={len(rows)} written={len(written_keys)} "
          f"already_seeded={len(decision['skipped'])} dim={dim} "
          f"collection={args.collection}（v2-only，未触碰 PG/Neo4j/v3）")
    if where is None or errors:
        # 数据已写入且读回通过，但回滚依据（manifest）落盘失败或降级 —— 必须大张旗鼓
        # 地失败，绝不允许 exit 0 假装"有证据"；manifest 可用前不得进入后续阶段。
        print("✗ fail-closed: run manifest 落盘失败/降级（写入已发生；证据位置见下）；"
              "在 manifest 可用前不得进行任何后续阶段。", file=sys.stderr)
        _announce_evidence(where, errors)
        return EXIT_FAIL_CLOSED
    _announce_evidence(where, errors)
    print("  （manifest 含回滚依据：--rollback <上述文件>）")
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
    # P1-A：manifest 自带的写目标必须严格等于受控 v2 目标——CLI 侧校验不覆盖
    # manifest 内容，篡改 manifest（v3 / 其它实例）不得把回滚删除引过去。
    target_err = validate_milvus_target(str(manifest.get("milvus_uri") or ""),
                                        str(manifest.get("collection") or ""))
    if target_err:
        return None, f"manifest 写目标非法: {target_err}"
    rows = manifest.get("rows")
    if not isinstance(rows, list) or not rows:
        return None, ("manifest rows 为空（本次运行未写入任何 owned row，无可回滚；"
                      "请使用实际写入运行的 manifest）")
    for r in rows:
        if not isinstance(r, dict) or safe_id(r.get("chunk_id")) is None:
            return None, "manifest rows 含非法 chunk_id"
    # kb/tenant/project 会进入 Milvus delete filter —— 必须非空且全部过安全校验
    # （防篡改注入；合法 manifest 由 seed 写出，tenant/project 必然存在）。
    if safe_id(manifest.get("kb_id")) is None:
        return None, "manifest kb_id 非法或不安全"
    for f in ("tenant_id", "project_id"):
        v = str(manifest.get(f) or "")
        if not v or safe_id(v) is None:
            return None, f"manifest {f} 缺失或非安全标识符"
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
        where, errors = _write_record(out_dir, "seed-shared-rollback-plan.json", plan)
        if where is None:
            print("✗ fail-closed: rollback plan 落盘失败（紧急证据已 dump stderr）", file=sys.stderr)
            return EXIT_FAIL_CLOSED
        print("SEED-SHARED 回滚方案（**仅方案，未删除任何数据**）")
        print(f"  依据 manifest : {args.rollback}")
        print(f"  目标 KB       : {kb}")
        print(f"  collection    : {collection}")
        print(f"  将核验键数    : {len(manifest_rows)}")
        _announce_evidence(where, errors)
        return EXIT_PLAN_ONLY

    # 模式 4：回滚执行（fail-closed + 全有或全无 + 幂等）。
    def fail(step: str, detail: Any) -> int:
        record = {"schema": ROLLBACK_SCHEMA, "mode": "rollback", "generated_at": _now(),
                  "kb_id": kb, "collection": collection, "executed": False,
                  "tenant_id": tenant, "project_id": project,
                  "milvus_uri": args.milvus_uri, "dim": dim,
                  "status": "FAIL_CLOSED", "failed_step": step, "detail": detail}
        where, errors = _write_record(out_dir, "seed-shared-rollback.json", record)
        print(f"✗ fail-closed @ {step}: {json.dumps(detail, ensure_ascii=False)[:400]}",
              file=sys.stderr)
        _announce_evidence(where, errors)
        return EXIT_FAIL_CLOSED

    def partial_fail(step: str, detail: Any, deleted_keys: List[str],
                     pending_keys: List[str]) -> int:
        """删除已部分发生：如实记录 PARTIAL（executed=true + deleted/pending 清单），
        绝不谎报 FAIL_CLOSED（零删除）；重跑同一 manifest 可幂等收敛。"""
        record = {"schema": ROLLBACK_SCHEMA, "mode": "rollback", "generated_at": _now(),
                  "kb_id": kb, "collection": collection,
                  "tenant_id": tenant, "project_id": project,
                  "milvus_uri": args.milvus_uri, "dim": dim,
                  "executed": True, "status": "PARTIAL", "failed_step": step, "detail": detail,
                  "counts": {"deleted": len(deleted_keys),
                             "already_absent": len(decision["already_absent"]),
                             "pending": len(pending_keys)},
                  "deleted_keys": deleted_keys,
                  "pending_keys": pending_keys,
                  "already_absent_keys": decision["already_absent"]}
        where, errors = _write_record(out_dir, "seed-shared-rollback.json", record)
        print(f"✗ PARTIAL @ {step}: deleted={len(deleted_keys)} pending={len(pending_keys)} "
              f"{json.dumps(detail, ensure_ascii=False)[:300]}", file=sys.stderr)
        print("  重跑同一 --rollback 命令可幂等收敛（已删键视为 already_absent）。",
              file=sys.stderr)
        _announce_evidence(where, errors)
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
    pending: List[str] = list(deletable)
    if deletable:
        try:
            for part in batched(deletable, BATCH):
                mv.delete(collection, guarded_delete_filter(part, kb, tenant, project))
                # 批次成功才推进账本：中途异常时 deleted/pending 精确反映已发生状态。
                deleted.extend(part)
                done = set(part)
                pending = [k for k in pending if k not in done]
            after = query_live_rows(mv, collection, [r["chunk_id"] for r in manifest_rows])
        except Exception as exc:  # noqa: BLE001
            return partial_fail("milvus_delete", f"{type(exc).__name__}: {exc}"[:300]
                                + "（读回未执行，以账本为准）", deleted, pending)
        remaining = [r["chunk_id"] for r in manifest_rows
                     if r["chunk_id"] in deletable and after.get(r["chunk_id"]) is not None]
        if remaining:
            # 读回为准：仍在库中的键按"未删除"如实入账，不得计入 deleted。
            deleted = [k for k in deleted if k not in set(remaining)]
            pending = remaining
            return partial_fail("rollback_verify",
                                {"remaining_after_delete": remaining[:20]},
                                deleted, pending)
        deleted = deletable
        pending = []

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
    where, errors = _write_record(out_dir, "seed-shared-rollback.json", record)
    print(f"✓ 回滚完成：deleted={len(deleted)} already_absent={len(decision['already_absent'])} "
          f"collection={collection}（v2-only；PG/Neo4j/v3 未触碰）")
    if where is None or errors:
        print("✗ fail-closed: rollback manifest 落盘失败/降级（删除已发生；证据位置见下）",
              file=sys.stderr)
        _announce_evidence(where, errors)
        return EXIT_FAIL_CLOSED
    _announce_evidence(where, errors)
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
                    default=os.environ.get("MILVUS_URI") or ENFORCED_MILVUS_URI,
                    help=f"必须为 {ENFORCED_MILVUS_URI}（强制，偏离即 fail-closed）")
    ap.add_argument("--collection",
                    default=os.environ.get("MILVUS_COLLECTION") or ENFORCED_COLLECTION,
                    help=f"必须为 {ENFORCED_COLLECTION}（强制，v2-only）")
    ap.add_argument("--pg-container", default=DEFAULT_PG_CONTAINER)
    args = ap.parse_args(argv)

    # 共享写目标强制：所有模式统一预校验（防 --milvus-uri/--collection/env 把
    # 受控写入引到 19530 / 其它项目实例 / v3 集合）。
    target_err = validate_milvus_target(args.milvus_uri, args.collection)
    if target_err:
        print(f"✗ fail-closed: {target_err}，零写入零删除", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    # P1-A：令牌校验**先于一切模式分发**——回滚执行与写入执行同级受控，错误
    # 令牌绝不能进入删除流程，也绝不能静默降级为 dry-run / 回滚计划。
    if args.authorize and args.authorize != AUTHORIZE_TOKEN:
        print(f"✗ fail-closed: --authorize 令牌不正确（期望 {AUTHORIZE_TOKEN}），"
              "零写入零删除", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    if args.rollback:
        return run_rollback(args)
    if not args.kb:
        ap.error("--kb 必填（rollback 模式可省略）")
    if args.authorize:
        return run_execute(args)
    return run_plan(args)


if __name__ == "__main__":
    raise SystemExit(main())
