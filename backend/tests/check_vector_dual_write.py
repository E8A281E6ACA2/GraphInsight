#!/usr/bin/env python3
"""
M5 S0→S3 双写语义验收测试（进程内 FakeClient，不触碰任何真实 Milvus）。

覆盖 ENTERPRISE_M5AB_REINDEX_DEPENDENCY_PLAN.md §4 的双写语义：
T1  默认 dual_write=false → 无双写目标（S0 原状）
T2  dual_write=true + primary=v2 → shadow=v3（S1：写 v2+v3、读 v2）
T3  dual_write=true + primary=v3 → 双写不生效（S2：读写都在 v3；禁止组合不可能出现）
T4  S1 upsert 同时落 v2/v3；content_revision 显式字段不被 metadata 覆盖
T5  v3 缺显式 content_revision 字段且 chunk 带版本号 → VectorStoreSchemaError（§8.5 fail-closed）
T6  v3 upsert 失败 → VectorStoreUpsertError 上抛（§16.1 禁止丢弃新写）
T7  dual_write=false → 只写 primary
T8  S1 delete_doc / clear 同步作用 v2+v3；S2 只作用 v3
T9  health() 透出 dual_write / dual_target_collection

运行：python backend/tests/check_vector_dual_write.py
"""
from __future__ import annotations

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


class _FakeSchema:
    def __init__(self, client: "FakeMilvusClient", name: str) -> None:
        self._client = client
        self._name = name
        self._fields: List[Dict[str, Any]] = []

    def add_field(self, field_name: str, datatype: Any, **kwargs: Any) -> None:
        entry: Dict[str, Any] = {"name": field_name, "data_type": datatype}
        if "dim" in kwargs:
            entry["params"] = {"dim": kwargs["dim"]}
        self._fields.append(entry)

    def commit(self) -> None:
        self._client.cols[self._name] = {"fields": list(self._fields), "rows": []}


class FakeMilvusClient:
    """最小 MilvusClient 假体：describe 驱动的 schema 校验 + 行级 upsert 记录。"""

    def __init__(self, collections: Dict[str, List[Dict[str, Any]]] | None = None) -> None:
        # {name: {"fields": [...], "rows": [...]}}
        self.cols: Dict[str, Dict[str, Any]] = {
            name: {"fields": fields, "rows": []} for name, fields in (collections or {}).items()
        }
        self.deleted: List[tuple] = []
        self.fail_upsert_on: set = set()

    def has_collection(self, name: str) -> bool:
        return name in self.cols

    def describe_collection(self, name: str) -> Dict[str, Any]:
        return {"fields": self.cols[name]["fields"]}

    def create_schema(self, auto_id: bool = False, enable_dynamic_field: bool = True) -> _FakeSchema:
        return _FakeSchema(self, "__pending__")

    def create_collection(self, collection_name: str, schema: _FakeSchema) -> None:
        schema._name = collection_name
        self.cols[collection_name] = {"fields": list(schema._fields), "rows": []}

    def prepare_index_params(self) -> Any:
        class _IP:
            def add_index(self, **kw: Any) -> None:
                pass

        return _IP()

    def create_index(self, collection_name: str, index_params: Any) -> None:
        pass

    def load_collection(self, name: str) -> None:
        pass

    def upsert(self, collection_name: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        if collection_name in self.fail_upsert_on:
            raise RuntimeError(f"injected upsert failure on {collection_name}")
        self.cols[collection_name]["rows"].extend(data)
        return {"upsert_count": len(data)}

    def delete(self, collection_name: str, filter: str) -> None:
        self.deleted.append((collection_name, filter))


V2_FIELDS = [
    {"name": "chunk_id", "data_type": "VARCHAR"},
    {"name": "doc_id", "data_type": "VARCHAR"},
    {"name": "kb_id", "data_type": "VARCHAR"},
    {"name": "tenant_id", "data_type": "VARCHAR"},
    {"name": "project_id", "data_type": "VARCHAR"},
    {"name": "vector", "data_type": "FLOAT_VECTOR", "params": {"dim": 4}},
]
V3_FIELDS = V2_FIELDS + [{"name": "content_revision", "data_type": "INT64"}]


def make_store(monkey: Dict[str, Any], client: FakeMilvusClient) -> Any:
    import services.vector_store as vs

    store = vs.MilvusVectorStore()
    store._client = client
    store._client_key = ("http://fake:19530", "", "default")

    def fake_vector_cfg() -> Dict[str, Any]:
        return dict(monkey["vector_cfg"])

    def fake_embedding_cfg() -> Dict[str, Any]:
        return {"dimension": 4}

    vs.get_vector_store_runtime_config = fake_vector_cfg
    vs.get_embedding_runtime_config = fake_embedding_cfg
    return store


def make_chunk(revision: int | None = 7, metadata: Dict[str, Any] | None = None) -> Any:
    import services.vector_store as vs

    return vs.VectorChunk(
        chunk_id="c1", doc_id="d1", text="hello", kb_id="kb1",
        tenant_id="t1", project_id="p1", content_hash="h", embedding_model="m",
        metadata=metadata or {}, content_revision=revision,
    )


def base_cfg(collection: str, dual: bool) -> Dict[str, Any]:
    return {
        "enabled": True, "provider": "milvus", "uri": "http://fake:19530",
        "token": "", "db_name": "default", "collection": collection,
        "metric_type": "COSINE", "index_type": "IVF_FLAT", "search_nprobe": 16,
        "dual_write": dual,
    }


def main() -> int:
    import services.vector_store as vs

    print("T1 默认 dual_write=false → 无双写目标")
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v2", False)},
                       FakeMilvusClient({"graphinsight_chunks_v2": V2_FIELDS}))
    cfg = store.config()
    check("T1 dual_write 默认 false", cfg["dual_write"] is False)
    check("T1 dual_target 为空", cfg["dual_target_collection"] == "")

    print("T2 dual_write=true + primary=v2 → shadow=v3（S1）")
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v2", True)},
                       FakeMilvusClient({"graphinsight_chunks_v2": V2_FIELDS}))
    cfg = store.config()
    check("T2 dual_write=true", cfg["dual_write"] is True)
    check("T2 dual_target=v3", cfg["dual_target_collection"] == "graphinsight_chunks_v3")

    print("T3 dual_write=true + primary=v3 → 不生效（S2 读写都在 v3）")
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v3", True)},
                       FakeMilvusClient({"graphinsight_chunks_v3": V3_FIELDS}))
    cfg = store.config()
    check("T3 dual_target 为空", cfg["dual_target_collection"] == "")

    print("T4 S1 upsert 同时落 v2/v3；版本由 v3 承载，v2 剥版本写（§8.5）")
    client = FakeMilvusClient({"graphinsight_chunks_v2": V2_FIELDS})
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v2", True)}, client)
    store.upsert_chunks([make_chunk(metadata={"content_revision": "evil"})], [[0.1, 0.2, 0.3, 0.4]])
    v2_rows = client.cols["graphinsight_chunks_v2"]["rows"]
    v3_rows = client.cols["graphinsight_chunks_v3"]["rows"]
    check("T4 v2 落 1 行", len(v2_rows) == 1)
    check("T4 v3 落 1 行", len(v3_rows) == 1)
    check("T4 v2 无 content_revision（剥版本，非 dynamic metadata）",
          "content_revision" not in v2_rows[0])
    check("T4 v3 显式 revision=7（不被 metadata 覆盖）",
          v3_rows[0]["content_revision"] == 7)
    check("T4 v3 建库含 content_revision 字段",
          any(f["name"] == "content_revision" for f in client.cols["graphinsight_chunks_v3"]["fields"]))

    print("T5 v3 缺显式字段且 chunk 带版本号 → §8.5 fail-closed")
    client = FakeMilvusClient({
        "graphinsight_chunks_v2": V2_FIELDS,
        "graphinsight_chunks_v3": V2_FIELDS,  # 旧 v3 无 content_revision 字段
    })
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v2", True)}, client)
    raised = False
    try:
        store.upsert_chunks([make_chunk(revision=7)], [[0.1, 0.2, 0.3, 0.4]])
    except vs.VectorStoreSchemaError:
        raised = True
    check("T5 抛 VectorStoreSchemaError", raised)
    check("T5 v3 未被写入", len(client.cols["graphinsight_chunks_v3"]["rows"]) == 0)
    check("T5 v2 已写入（primary 先行）", len(client.cols["graphinsight_chunks_v2"]["rows"]) == 1)

    print("T6 v3 upsert 失败 → §16.1 fail-closed 上抛")
    client = FakeMilvusClient({"graphinsight_chunks_v2": V2_FIELDS})
    client.fail_upsert_on.add("graphinsight_chunks_v3")
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v2", True)}, client)
    raised = False
    try:
        store.upsert_chunks([make_chunk(revision=None)], [[0.1, 0.2, 0.3, 0.4]])
    except vs.VectorStoreUpsertError:
        raised = True
    check("T6 抛 VectorStoreUpsertError", raised)

    print("T7 dual_write=false → 只写 primary；带版本 chunk 无 dual 时仍 §8.5 拒写")
    client = FakeMilvusClient({"graphinsight_chunks_v2": V2_FIELDS})
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v2", False)}, client)
    store.upsert_chunks([make_chunk(revision=None)], [[0.1, 0.2, 0.3, 0.4]])
    check("T7 无版本 chunk 落 v2", len(client.cols["graphinsight_chunks_v2"]["rows"]) == 1)
    check("T7 v3 未创建", "graphinsight_chunks_v3" not in client.cols)
    raised = False
    try:
        store.upsert_chunks([make_chunk(revision=7)], [[0.1, 0.2, 0.3, 0.4]])
    except vs.VectorStoreSchemaError:
        raised = True
    check("T7b 带版本+无 dual+primary 缺字段 → 拒写（版本将完全丢失）", raised)
    check("T7b 拒写零落行", len(client.cols["graphinsight_chunks_v2"]["rows"]) == 1)

    print("T8 S1 删除同步 v2+v3；S2 只作用 v3")
    client = FakeMilvusClient({"graphinsight_chunks_v2": V2_FIELDS, "graphinsight_chunks_v3": V3_FIELDS})
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v2", True)}, client)
    store.delete_doc("d1", "kb1")
    targets = [c for c, _ in client.deleted]
    check("T8 S1 delete 双侧", targets.count("graphinsight_chunks_v2") == 1
          and targets.count("graphinsight_chunks_v3") == 1)
    client.deleted.clear()
    store.clear(["kb1"])
    targets = [c for c, _ in client.deleted]
    check("T8 S1 clear 双侧", targets.count("graphinsight_chunks_v2") == 1
          and targets.count("graphinsight_chunks_v3") == 1)
    client2 = FakeMilvusClient({"graphinsight_chunks_v3": V3_FIELDS})
    store2 = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v3", False)}, client2)
    store2.delete_doc("d1", "kb1")
    targets = [c for c, _ in client2.deleted]
    check("T8 S2 delete 仅 v3", targets == ["graphinsight_chunks_v3"])

    print("T9 health() 透出双写状态")
    store = make_store({"vector_cfg": base_cfg("graphinsight_chunks_v2", True)},
                       FakeMilvusClient({"graphinsight_chunks_v2": V2_FIELDS}))
    health = store.health()
    check("T9 health.dual_write=true", health.get("dual_write") is True)
    check("T9 health.dual_target=v3", health.get("dual_target_collection") == "graphinsight_chunks_v3")

    print()
    print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for f in FAILURES:
            print("  FAILED: " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
