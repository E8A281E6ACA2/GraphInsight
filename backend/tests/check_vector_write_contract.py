#!/usr/bin/env python3
"""向量写入契约回归矩阵（统一入口 `upsert_chunks` 的两种意图 + KB 范围守卫）。

覆盖工单要求的场景（进程内假体，零真实连接）：
  C1  在线 S1：legacy v2 无显式 revision + 影子生效 → primary **剥版本写**、影子带版本
  C2  在线 S1：primary 也有显式 INT64 → 两侧都带版本
  C3  在线 S1：primary 缺字段且**无影子** → 拒写（§8.5，版本不下降进 metadata）
  C4  在线 S1：影子缺显式字段 → 写前拒绝，primary 也未被写（不是"写完才发现"）
  C5  在线 S1：primary 失败 → 抛错且**影子零写**（绝不出现"影子已写、主库未写"）
  C6  在线 S1：shadow 失败 → DualWriteShadowError，primary 已写（可定位、可重放收敛）
  C7  在线 S1：确认数不一致（部分 upsert）→ 抛错，不静默
  C8  历史回填：primary 有该 chunk → 只写影子（v2 零写）、影子带版本、写后读回一致
  C9  历史回填：primary 尚无该 chunk → 拒写（§16.1 读源不变量），零写入
  C10 历史回填：影子未生效（dual_write 关闭）→ 拒写（revision_field_absent 口径），零写入
  C11 历史回填：写后读回不一致 → 拒标 indexed（VERIFY_MISMATCH）
  C12 revision 2/3 保留：写入原样落库，**绝不重置为 1**；同批重跑幂等（行数不增）
  C13 范围守卫：write_scope 只允许 kb-a → 写 kb-b 拒（KB_NOT_ALLOWED）且零写入；kb-a 放行
  C14 范围守卫：tenant 与授权范围不一致 → 拒（SCOPE_MISMATCH）
CAS 失败场景见 `check_b0_reindex_chunks.py`（worker 层 `direct_cas_failure`），本套件不重复。

运行：python backend/tests/check_vector_write_contract.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FAILURES: list = []
PASSES: list = []


def check(name: str, cond: bool) -> None:
    (PASSES if cond else FAILURES).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name)


class FakeSchema:
    def __init__(self, name: str) -> None:
        self.name = name
        self.fields: List[Dict[str, Any]] = []

    def add_field(self, field_name: str, datatype: Any, **kwargs: Any) -> None:
        entry = {"name": field_name, "data_type": datatype}
        if "dim" in kwargs:
            entry["params"] = {"dim": kwargs["dim"]}
        self.fields.append(entry)

    def commit(self) -> None:
        pass


class FakeClient:
    """最小 MilvusClient 假体：schema 探针 + 行级 upsert/query + 注入点。"""

    V2_FIELDS = [
        {"name": "chunk_id", "data_type": "VARCHAR"},
        {"name": "doc_id", "data_type": "VARCHAR"},
        {"name": "kb_id", "data_type": "VARCHAR"},
        {"name": "tenant_id", "data_type": "VARCHAR"},
        {"name": "project_id", "data_type": "VARCHAR"},
        {"name": "vector", "data_type": "FLOAT_VECTOR", "params": {"dim": 4}},
    ]
    V3_FIELDS = V2_FIELDS + [{"name": "content_revision", "data_type": "INT64"}]

    def __init__(self, collections: Dict[str, List[Dict[str, Any]]]) -> None:
        self.cols = {name: {"fields": fields, "rows": []} for name, fields in collections.items()}
        self.upsert_calls: List[str] = []
        self.fail_upsert_on: set = set()
        self.partial_on: set = set()
        self.tamper_revision: Dict[str, int] = {}
        self.drop_collection_calls: List[str] = []

    # --- schema 探针 ---
    def has_collection(self, name: str) -> bool:
        return name in self.cols

    def describe_collection(self, name: str) -> Dict[str, Any]:
        return {"fields": self.cols[name]["fields"]}

    def create_schema(self, auto_id: bool = False, enable_dynamic_field: bool = True) -> FakeSchema:
        return FakeSchema("__pending__")

    def create_collection(self, collection_name: str, schema: FakeSchema) -> None:
        self.cols[collection_name] = {"fields": list(schema.fields), "rows": []}

    def prepare_index_params(self) -> Any:
        class _IP:
            def add_index(self, **kw: Any) -> None:
                pass

        return _IP()

    def create_index(self, collection_name: str, index_params: Any) -> None:
        pass

    def load_collection(self, name: str) -> None:
        pass

    def flush(self, collection_name: str) -> None:
        pass

    # --- mutation ---
    def upsert(self, collection_name: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.upsert_calls.append(collection_name)
        if collection_name in self.fail_upsert_on:
            raise RuntimeError(f"injected upsert failure on {collection_name}")
        page = self.cols.setdefault(collection_name, {"fields": self.V3_FIELDS, "rows": []})
        for row in data:
            page["rows"] = [existing for existing in page["rows"] if existing.get("chunk_id") != row.get("chunk_id")]
            page["rows"].append(dict(row))
        if collection_name in self.partial_on:
            return {"upsert_count": max(0, len(data) - 1)}
        return {"upsert_count": len(data)}

    def delete(self, collection_name: str, filter: str) -> None:  # noqa: A002
        self.upsert_calls.append(f"delete:{collection_name}")

    # --- read ---
    @staticmethod
    def _filtered_ids(filter_str: str):
        """解析 `chunk_id in ["a", "b"]` 过滤（夹具必须尊重 filter，否则会误报"缺 chunk"）。"""
        m = re.search(r'chunk_id in \[(.*?)\]', filter_str or "")
        if not m:
            return None
        return {value.replace('\\"', '"').replace("\\\\", "\\")
                for value in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))}

    def query(self, collection_name: str, filter: str, output_fields: List[str], limit: int = None,
              **_kw: Any) -> List[Dict[str, Any]]:
        rows = list(self.cols.get(collection_name, {}).get("rows", []))
        wanted = self._filtered_ids(filter)
        if wanted is not None:
            rows = [row for row in rows if str(row.get("chunk_id") or "") in wanted]
        out = []
        for row in rows[: (limit or len(rows))]:
            item = {key: row.get(key) for key in output_fields if key in row}
            cid = str(row.get("chunk_id") or "")
            if cid in self.tamper_revision:
                item["content_revision"] = self.tamper_revision[cid]
            out.append(item)
        return out


def make_store(vector_cfg: Dict[str, Any], client: FakeClient):
    import services.vector_store as vs

    store = vs.MilvusVectorStore()
    store._client = client
    store._client_key = ("http://fake:19530", "", "default")
    vs.get_vector_store_runtime_config = lambda: dict(vector_cfg)
    vs.get_embedding_runtime_config = lambda: {"dimension": 4}
    return store, vs


def cfg(collection: str = "graphinsight_chunks_v2", dual: bool = False, shadow: str = "",
        scope: Dict[str, Any] = None) -> Dict[str, Any]:
    return {
        "enabled": True, "provider": "milvus", "uri": "http://fake:19530", "token": "",
        "db_name": "default", "collection": collection, "metric_type": "COSINE",
        "index_type": "IVF_FLAT", "search_nprobe": 16, "dual_write": dual, "shadow_collection": shadow,
        "write_scope": scope or {"kb_ids": [], "tenant_id": "", "project_id": ""},
    }


def chunk(cid: str, revision: int | None, kb: str = "kb-a", tenant: str = "t1", project: str = "p1") -> Any:
    import services.vector_store as vs

    return vs.VectorChunk(chunk_id=cid, doc_id="d1", text=f"text-{cid}", kb_id=kb,
                          tenant_id=tenant, project_id=project, content_hash="h",
                          embedding_model="m", content_revision=revision)


VEC = [0.1, 0.2, 0.3, 0.4]


def main() -> int:
    print("向量写入契约回归矩阵（统一入口 + 两种意图 + KB 范围守卫）")

    # C1 legacy v2 无字段 + 影子生效 → primary 剥版本、影子带版本
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    store.upsert_chunks([chunk("c1", 1), chunk("c2", 1)], [VEC, VEC])
    v2_rows = {r["chunk_id"]: r for r in client.cols["graphinsight_chunks_v2"]["rows"]}
    v3_rows = {r["chunk_id"]: r for r in client.cols["graphinsight_chunks_v3"]["rows"]}
    check("C1 primary(v2) 收到 2 行", len(v2_rows) == 2)
    check("C1 primary 行**不带** content_revision（剥版本，非 metadata）",
          all("content_revision" not in row for row in v2_rows.values()))
    check("C1 影子(v3) 收到 2 行且带显式版本=1",
          len(v3_rows) == 2 and all(row["content_revision"] == 1 for row in v3_rows.values()))

    # C2 primary 也有 INT64 → 两侧都带版本
    client = FakeClient({"graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    store, vs = make_store(cfg(collection="graphinsight_chunks_v3", dual=True,
                               shadow="graphinsight_chunks_v3_shadow"), client)
    client.cols["graphinsight_chunks_v3_shadow"] = {"fields": FakeClient.V3_FIELDS, "rows": []}
    store.upsert_chunks([chunk("c1", 2)], [VEC])
    check("C2 primary 带显式版本=2", client.cols["graphinsight_chunks_v3"]["rows"][0]["content_revision"] == 2)
    check("C2 影子带显式版本=2", client.cols["graphinsight_chunks_v3_shadow"]["rows"][0]["content_revision"] == 2)

    # C3 primary 缺字段 + 无影子 → 拒写且零写入
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS})
    store, vs = make_store(cfg(dual=False), client)
    raised = False
    try:
        store.upsert_chunks([chunk("c1", 1)], [VEC])
    except vs.VectorStoreSchemaError:
        raised = True
    check("C3 抛 VectorStoreSchemaError", raised)
    check("C3 零写入（v2 行数 0）", len(client.cols["graphinsight_chunks_v2"]["rows"]) == 0)

    # C4 影子缺显式字段 → 写前拒绝，primary 也未写
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS})
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_bad"), client)
    client.cols["graphinsight_chunks_bad"] = {"fields": FakeClient.V2_FIELDS, "rows": []}
    raised = False
    try:
        store.upsert_chunks([chunk("c1", 1)], [VEC])
    except vs.VectorStoreSchemaError:
        raised = True
    check("C4 影子无 INT64 字段 → 拒绝", raised)
    check("C4 拒绝发生在写之前（primary 零行、无 upsert 调用）",
          len(client.cols["graphinsight_chunks_v2"]["rows"]) == 0
          and client.upsert_calls.count("graphinsight_chunks_v2") == 0)

    # C5 primary 失败 → 抛错且影子零写
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.fail_upsert_on.add("graphinsight_chunks_v2")
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    raised = False
    try:
        store.upsert_chunks([chunk("c1", None)], [VEC])
    except Exception:  # noqa: BLE001
        raised = True
    check("C5 primary 失败 → 抛错", raised)
    check("C5 影子零写（无 v3 upsert 调用）", "graphinsight_chunks_v3" not in client.upsert_calls)

    # C6 shadow 失败 → DualWriteShadowError（primary 已写、可定位）
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.fail_upsert_on.add("graphinsight_chunks_v3")
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    err = None
    try:
        store.upsert_chunks([chunk("c1", None)], [VEC])
    except Exception as exc:  # noqa: BLE001
        err = exc
    check("C6 抛 DualWriteShadowError", isinstance(err, vs.DualWriteShadowError))
    check("C6 primary 已写（v2 有 1 行）、影子 0 行",
          len(client.cols["graphinsight_chunks_v2"]["rows"]) == 1
          and len(client.cols["graphinsight_chunks_v3"]["rows"]) == 0)
    check("C6 异常携带两侧集合名（可定位）",
          getattr(err, "primary_collection", "") == "graphinsight_chunks_v2"
          and getattr(err, "shadow_collection", "") == "graphinsight_chunks_v3")

    # C7 部分 upsert（影子确认数少 1）→ 抛错
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.partial_on.add("graphinsight_chunks_v3")
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    raised = False
    try:
        store.upsert_chunks([chunk("c1", None), chunk("c2", None)], [VEC, VEC])
    except vs.DualWriteShadowError:
        raised = True
    check("C7 确认数不一致 → DualWriteShadowError（不静默）", raised)

    # C8 历史回填：primary 已有该 chunk → 只写影子
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.cols["graphinsight_chunks_v2"]["rows"] = [{"chunk_id": "c1", "kb_id": "kb-a"}]
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    written = store.upsert_chunks([chunk("c1", 1)], [VEC], intent="backfill_versioned")
    check("C8 回填返回确认数 1", written == 1)
    check("C8 v2 **零写**（未对 primary 发起 upsert）",
          client.upsert_calls.count("graphinsight_chunks_v2") == 0)
    check("C8 影子带显式版本=1", client.cols["graphinsight_chunks_v3"]["rows"][0]["content_revision"] == 1)

    # C9 回填：primary 缺该 chunk → 拒写（§16.1），零写入
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    err = None
    try:
        store.upsert_chunks([chunk("c-missing", 1)], [VEC], intent="backfill_versioned")
    except vs.VectorWriteContractError as exc:
        err = exc
    check("C9 抛 VectorWriteContractError(PRIMARY_VECTOR_MISSING)",
          err is not None and err.reason == vs.REASON_PRIMARY_VECTOR_MISSING)
    check("C9 零写入（影子 0 行）", len(client.cols["graphinsight_chunks_v3"]["rows"]) == 0)

    # C10 回填：影子未生效（dual_write 关闭）→ 拒写
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS})
    store, vs = make_store(cfg(dual=False), client)
    err = None
    try:
        store.upsert_chunks([chunk("c1", 1)], [VEC], intent="backfill_versioned")
    except vs.VectorWriteContractError as exc:
        err = exc
    check("C10 抛 SHADOW_NOT_ACTIVE", err is not None and err.reason == vs.REASON_SHADOW_NOT_ACTIVE)
    check("C10 零写入", all(not page["rows"] for page in client.cols.values()))

    # C11 回填：写后读回不一致 → 拒标 indexed
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.cols["graphinsight_chunks_v2"]["rows"] = [{"chunk_id": "c1", "kb_id": "kb-a"}]
    client.tamper_revision["c1"] = 99
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    err = None
    try:
        store.upsert_chunks([chunk("c1", 1)], [VEC], intent="backfill_versioned")
    except vs.VectorWriteContractError as exc:
        err = exc
    check("C11 抛 VERIFY_MISMATCH", err is not None and err.reason == vs.REASON_VERIFY_MISMATCH)

    # C12 revision 2/3 保留 + 重跑幂等
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.cols["graphinsight_chunks_v2"]["rows"] = [{"chunk_id": "c9", "kb_id": "kb-a"}]
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    store.upsert_chunks([chunk("c9", 3)], [VEC], intent="backfill_versioned")
    store.upsert_chunks([chunk("c9", 3)], [VEC], intent="backfill_versioned")
    rows = client.cols["graphinsight_chunks_v3"]["rows"]
    check("C12 revision=3 原样保留（绝不重置为 1）", len(rows) == 1 and rows[0]["content_revision"] == 3)
    check("C12 同批重跑幂等（行数不增）", len(rows) == 1)

    # C13 范围守卫：只允许 kb-a
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.cols["graphinsight_chunks_v2"]["rows"] = [
        {"chunk_id": "c-b", "kb_id": "kb-b"},
        {"chunk_id": "c-a", "kb_id": "kb-a"},
    ]
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3",
                               scope={"kb_ids": ["kb-a"], "tenant_id": "", "project_id": ""}), client)
    err = None
    try:
        store.upsert_chunks([chunk("c-b", 1, kb="kb-b")], [VEC], intent="backfill_versioned")
    except vs.VectorWriteContractError as exc:
        err = exc
    check("C13 非目标 KB 拒写（KB_NOT_ALLOWED）", err is not None and err.reason == vs.REASON_KB_NOT_ALLOWED)
    check("C13 零写入（影子 0 行、无任何 upsert 调用）",
          len(client.cols["graphinsight_chunks_v3"]["rows"]) == 0 and not client.upsert_calls)
    written = store.upsert_chunks([chunk("c-a", 1, kb="kb-a")], [VEC], intent="backfill_versioned")
    check("C13 目标 KB 放行（返回 1）", written == 1)

    # C14 范围守卫：tenant 不一致
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.cols["graphinsight_chunks_v2"]["rows"] = [{"chunk_id": "c1", "kb_id": "kb-a"}]
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3",
                               scope={"kb_ids": ["kb-a"], "tenant_id": "t-authorized", "project_id": ""}), client)
    err = None
    try:
        store.upsert_chunks([chunk("c1", 1, kb="kb-a", tenant="t-other")], [VEC], intent="backfill_versioned")
    except vs.VectorWriteContractError as exc:
        err = exc
    check("C14 tenant 不一致 → SCOPE_MISMATCH", err is not None and err.reason == vs.REASON_SCOPE_MISMATCH)
    check("C14 零写入", len(client.cols["graphinsight_chunks_v3"]["rows"]) == 0)

    # ---- C15–C17：P1-3 metadata 不得覆盖保留字段 / 剥版本必须彻底移除 ----
    def _mk_chunk(cid: str, revision, metadata: Dict[str, Any]):
        import services.vector_store as _vs

        return _vs.VectorChunk(chunk_id=cid, doc_id="d1", text=f"text-{cid}", kb_id="kb-a",
                               tenant_id="t1", project_id="p1", content_hash="h", embedding_model="m",
                               content_revision=revision, metadata=metadata)

    # C15 版本承载侧：显式 revision=2 + metadata revision=99 → 落库必须是 2
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.cols["graphinsight_chunks_v2"]["rows"] = [{"chunk_id": "c1", "kb_id": "kb-a"}]
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    store.upsert_chunks([_mk_chunk("c1", 2, {"content_revision": 99})], [VEC], intent="backfill_versioned")
    v3_row = client.cols["graphinsight_chunks_v3"]["rows"][0]
    check("C15 metadata 里的 content_revision=99 被丢弃，落库为显式 2", v3_row["content_revision"] == 2)

    # C16 剥版本侧（legacy v2 无字段）：metadata 试图补写版本 → 该键必须彻底不存在
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    store.upsert_chunks([_mk_chunk("c1", 1, {"content_revision": 99})], [VEC])
    v2_row = client.cols["graphinsight_chunks_v2"]["rows"][0]
    check("C16 剥版本后 v2 行**不含** content_revision 键（metadata 不能补写）",
          "content_revision" not in v2_row)
    check("C16 影子侧仍带显式版本 1", client.cols["graphinsight_chunks_v3"]["rows"][0]["content_revision"] == 1)

    # C17 metadata 试图覆盖身份/向量 → 必须被丢弃
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3"), client)
    store.upsert_chunks(
        [_mk_chunk("c-keep", None, {"chunk_id": "hijack", "vector": [9.0] * 4, "kb_id": "kb-evil"})],
        [VEC],
    )
    row = client.cols["graphinsight_chunks_v2"]["rows"][0]
    check("C17 metadata 不能覆盖 chunk_id", row["chunk_id"] == "c-keep")
    check("C17 metadata 不能覆盖 vector", row["vector"] == VEC)
    check("C17 metadata 不能覆盖 kb_id", row["kb_id"] == "kb-a")

    # ---- C18–C20：P1-4 范围条件独立生效 / 非法配置不得退化为无限制 ----
    # C18 kb_ids 配置非法（","）→ SCOPE_CONFIG_INVALID 且零写入
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.cols["graphinsight_chunks_v2"]["rows"] = [{"chunk_id": "c1", "kb_id": "kb-a"}]
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3",
                               scope={"kb_ids": [], "kb_ids_configured": True,
                                      "tenant_id": "", "project_id": ""}), client)
    err = None
    try:
        store.upsert_chunks([chunk("c1", 1)], [VEC], intent="backfill_versioned")
    except vs.VectorWriteContractError as exc:
        err = exc
    check("C18 非法配置（配置过但解析为空）→ SCOPE_CONFIG_INVALID",
          err is not None and err.reason == vs.REASON_SCOPE_CONFIG_INVALID)
    check("C18 非法配置零写入",
          len(client.cols["graphinsight_chunks_v3"]["rows"]) == 0 and not client.upsert_calls)

    # C19 kb_ids **未配置** 但 tenant 已配置 → tenant 限制必须独立生效
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client.cols["graphinsight_chunks_v2"]["rows"] = [{"chunk_id": "c1", "kb_id": "kb-a"}]
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3",
                               scope={"kb_ids": [], "kb_ids_configured": False,
                                      "tenant_id": "t-authorized", "project_id": ""}), client)
    written = store.upsert_chunks([chunk("c1", 1, tenant="t-authorized")], [VEC],
                                  intent="backfill_versioned")
    check("C19 kb_ids 未配置时 tenant 匹配 → 放行", written == 1)
    client2 = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                          "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    client2.cols["graphinsight_chunks_v2"]["rows"] = [{"chunk_id": "c1", "kb_id": "kb-a"}]
    store2, vs2 = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3",
                                 scope={"kb_ids": [], "kb_ids_configured": False,
                                        "tenant_id": "t-authorized", "project_id": ""}), client2)
    err = None
    try:
        store2.upsert_chunks([chunk("c1", 1, tenant="t-other")], [VEC], intent="backfill_versioned")
    except vs2.VectorWriteContractError as exc:
        err = exc
    check("C19 kb_ids 未配置但 tenant 不符 → 仍拒（SCOPE_MISMATCH，零写入）",
          err is not None and err.reason == vs2.REASON_SCOPE_MISMATCH
          and len(client2.cols["graphinsight_chunks_v3"]["rows"]) == 0)

    # C20 delete_doc / clear 同样受名单与非法配置约束（零 mutation）
    client = FakeClient({"graphinsight_chunks_v2": FakeClient.V2_FIELDS,
                         "graphinsight_chunks_v3": FakeClient.V3_FIELDS})
    store, vs = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3",
                               scope={"kb_ids": ["kb-a"], "kb_ids_configured": True,
                                      "tenant_id": "", "project_id": ""}), client)
    err = None
    try:
        store.delete_doc("d1", "kb-b")
    except vs.VectorWriteContractError as exc:
        err = exc
    check("C20 delete_doc 非名单 KB → KB_NOT_ALLOWED",
          err is not None and err.reason == vs.REASON_KB_NOT_ALLOWED)
    check("C20 delete_doc 零 mutation", not client.upsert_calls)
    err = None
    try:
        store.clear(["kb-b"])
    except vs.VectorWriteContractError as exc:
        err = exc
    check("C20 clear 非名单 KB → KB_NOT_ALLOWED 且零 mutation",
          err is not None and err.reason == vs.REASON_KB_NOT_ALLOWED and not client.upsert_calls)
    store_bad, vs_bad = make_store(cfg(dual=True, shadow="graphinsight_chunks_v3",
                                       scope={"kb_ids": [], "kb_ids_configured": True,
                                              "tenant_id": "", "project_id": ""}), client)
    err = None
    try:
        store_bad.clear(["kb-a"])
    except vs_bad.VectorWriteContractError as exc:
        err = exc
    check("C20 clear 在非法配置下拒绝（不得退化为无限制）",
          err is not None and err.reason == vs_bad.REASON_SCOPE_CONFIG_INVALID
          and not client.upsert_calls)

    print()
    print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for item in FAILURES:
            print("  FAILED: " + item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
