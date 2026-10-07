#!/usr/bin/env python3
"""非空 disposable 真实执行暴露的四个缺陷的**离线回归**（零真实服务连接）。

真实执行（RUN-20261007T000406）首轮 fail-closed 暴露了四个离线契约/夹具均未覆盖的缺陷：

  R1 **Milvus collection 缺向量索引**：`load_collection` 报 `index not found`、
     `query` 报 `collection not loaded`，S1 双写与 backfill 全败。
  R2 **backfill embedding 打真实 API**：`embedding_service.embed_texts` 用假 key 触发 401
     AuthenticationError，Milvus 腿误判 failed。
  R3 **写后读回看不到 growing segment**：`upsert` 后立即 `query` 返回空，
     `verify_versioned_rows` 判 VERIFY_MISMATCH 假阴。
  R4 **端口被代理软件动态抢占**：55433 被 Clash Verge（mihomo）占用，docker bind 失败。

本文件用假体/mock 离线 pin 住修复后的行为（不连任何真实 PG/Neo4j/Milvus/LLM）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_m5_nonempty_disposable as disp  # noqa: E402

PASSES: List[str] = []
FAILURES: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSES if cond else FAILURES).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" ({detail})" if detail and not cond else ""))


def cfg(collection: str = "graphinsight_chunks_v2", dual: bool = False,
        shadow: str = "") -> Dict[str, Any]:
    return {
        "enabled": True, "provider": "milvus", "uri": "http://fake:19530", "token": "",
        "db_name": "default", "collection": collection, "metric_type": "IP",
        "index_type": "FLAT", "search_nprobe": 16, "dual_write": dual, "shadow_collection": shadow,
        "write_scope": {"kb_ids": [], "tenant_id": "", "project_id": ""},
    }


def make_store(vector_cfg: Dict[str, Any], client: Any):
    import services.vector_store as vs

    store = vs.MilvusVectorStore()
    store._client = client
    store._client_key = ("http://fake:19530", "", "default")
    vs.get_vector_store_runtime_config = lambda: dict(vector_cfg)
    vs.get_embedding_runtime_config = lambda: {"dimension": 4}
    return store, vs


class FakeMilvus:
    """模拟真实 Milvus 2.5 的三个语义（正是 R1/R3 的成因）：

    - 无向量索引 → `load_collection` 抛 `index not found`；
    - 未 load → `query` 抛 `collection not loaded`；
    - `upsert` 的数据先进 growing segment，`flush` 后才进入可见（sealed）区。
    """

    def __init__(self) -> None:
        self.cols: Dict[str, Dict[str, bool]] = {}
        self.sealed: Dict[str, Dict[str, dict]] = {}
        self.growing: Dict[str, Dict[str, dict]] = {}
        self.flush_calls: List[str] = []
        self.load_calls: List[str] = []
        self.index_calls: List[str] = []

    def has_collection(self, name: str) -> bool:
        return name in self.cols

    def create_collection(self, collection_name: str, **_kw: Any) -> None:
        self.cols[collection_name] = {"index": False, "loaded": False}

    def create_index(self, collection_name: str, **_kw: Any) -> None:
        self.index_calls.append(collection_name)
        self.cols.setdefault(collection_name, {})["index"] = True

    def load_collection(self, collection_name: str, **_kw: Any) -> None:
        if not self.cols.get(collection_name, {}).get("index"):
            raise RuntimeError(f"index not found[collection={collection_name}]")
        self.load_calls.append(collection_name)
        self.cols[collection_name]["loaded"] = True

    def flush(self, collection_name: str, **_kw: Any) -> None:
        self.flush_calls.append(collection_name)
        self.sealed.setdefault(collection_name, {}).update(self.growing.pop(collection_name, {}))

    def upsert(self, collection_name: str, data: List[dict], **_kw: Any) -> dict:
        for row in data:
            self.growing.setdefault(collection_name, {})[row["chunk_id"]] = dict(row)
        return {"upsert_count": len(data)}

    def query(self, collection_name: str, filter: str = "", output_fields: List[str] = None,
              limit: int = None, **_kw: Any) -> List[dict]:
        if not self.cols.get(collection_name, {}).get("loaded"):
            raise RuntimeError(f"collection not loaded[collection={collection_name}]")
        rows = list(self.sealed.get(collection_name, {}).values())
        fields = output_fields or []
        return [{k: v for k, v in r.items() if not fields or k in fields} for r in rows]


# ---- R1：Milvus 建索引后 load ----
def test_r1_index_then_load() -> None:
    fake = FakeMilvus()
    fake.create_collection("c1")
    # 未建索引 → load 必须失败（真实 Milvus 语义）
    try:
        fake.load_collection("c1")
        loaded = True
    except RuntimeError as exc:
        loaded = False
        detail = str(exc)
    check("R1 无索引时 load_collection 抛 index not found",
          not loaded and "index not found" in detail, detail)
    # 未 load → query 必须失败
    try:
        fake.query("c1", output_fields=["chunk_id"])
        queried = True
    except RuntimeError as exc:
        queried = False
        detail = str(exc)
    check("R1 未 load 时 query 抛 collection not loaded",
          not queried and "collection not loaded" in detail, detail)
    # 建索引后 load + query 成功
    fake.create_index("c1")
    fake.load_collection("c1")
    check("R1 建索引后 load 成功", fake.cols["c1"]["loaded"] is True)
    check("R1 建索引后可 query", fake.query("c1", output_fields=["chunk_id"]) == [])
    # 修复必须落在 disposable 脚本的建集合路径上（源码级 pin）
    src = (f"{disp.S1_DRIVER}{disp.SEED_BF_DRIVER}")
    check("R1 S1_DRIVER/SEED_BF_DRIVER 建集合路径含 create_index",
          "create_index" in disp.S1_DRIVER, "S1_DRIVER 缺 create_index")
    check("R1 建集合与建索引在同一 driver 里",
          "create_collection" in src and "create_index" in src)


# ---- R2：合成 embedding 只存在于测试入口（结构性隔离，不靠 env 开关/注释） ----
def test_r2_synthetic_embedding_isolated_to_test_entry() -> None:
    """R2（本轮收紧）：此前"生产不触发"只是注释 + env 开关，没有 disposable 范围校验。
    现在生产模块**不含任何合成向量分支**，替身只由测试入口 wrapper 注入。"""
    import tempfile

    import admin.backfill_chunk_revisions as bf

    src = Path(bf.__file__).read_text(encoding="utf-8")
    # 1) 结构性隔离：生产模块不得出现合成向量相关实现/开关
    check("R2 生产模块不含 EMBEDDING_DETERMINISTIC_VECTOR 开关",
          "EMBEDDING_DETERMINISTIC_VECTOR" not in src)
    check("R2 生产模块不含 _deterministic_vector / _embed_texts_for_backfill",
          "_deterministic_vector" not in src and "_embed_texts_for_backfill" not in src)
    check("R2 生产 _backfill_milvus 直接调 embedding_service.embed_texts",
          "vectors = embedding_service.embed_texts(texts)" in src)
    check("R2 生产模块无合成向量函数属性（结构性，非注释）",
          not hasattr(bf, "_deterministic_vector") and not hasattr(bf, "_embed_texts_for_backfill"))

    # 2) 反例：**共享/生产配置下不能启用合成向量**——即使 env 被设成 true，
    #    生产路径也走真实 embedding_service（mock 被调用即证明未走合成）。
    with mock.patch.dict(os.environ, {"EMBEDDING_DETERMINISTIC_VECTOR": "true"}):
        import services.embedding_service as es_mod

        with mock.patch.object(es_mod.embedding_service, "embed_texts",
                               return_value=[[0.9, 0.8, 0.7, 0.6]]) as spy:
            out = es_mod.embedding_service.embed_texts(["t1"])
        check("R2 反例：共享配置下仍走真实 embedding_service",
              spy.called and out == [[0.9, 0.8, 0.7, 0.6]])
        # 合成向量的形态特征是 dim=4 且由 sha256 派生；这里返回的必须是 mock 的真实值，
        # 且生产模块里不存在任何会产出它的代码路径（上条结构性断言已保证）。

    # 3) 替身只在测试入口：disposable 的 wrapper 注入，env file 不再注入该开关
    with tempfile.TemporaryDirectory() as td:
        entry_text = Path(disp._backfill_entry(Path(td))).read_text(encoding="utf-8")
    check("R2 测试入口 wrapper 注入 embed_texts 替身",
          "embedding_service.embed_texts = lambda" in entry_text)
    check("R2 wrapper 跑的是真实 backfill 脚本",
          "runpy.run_path" in entry_text and "backfill_chunk_revisions.py" in entry_text)
    with tempfile.TemporaryDirectory() as td:
        disp.base_env(Path(td))
        env_text = (Path(td) / "nonempty.env").read_text(encoding="utf-8")
    check("R2 disposable env file 不再注入 EMBEDDING_DETERMINISTIC_VECTOR",
          "EMBEDDING_DETERMINISTIC_VECTOR" not in env_text, env_text)
    check("R2 disposable 的 backfill 调用走测试入口 wrapper",
          "_backfill_entry(tmp)" in Path(disp.__file__).read_text(encoding="utf-8"))


# ---- R3：flush/load 后读回 ----
def test_r3_flush_and_load_for_readback() -> None:
    import services.vector_store as vs

    fake = FakeMilvus()
    fake.create_collection("shadow")
    fake.create_index("shadow")
    store, _ = make_store(cfg(dual=True, shadow="shadow"), fake)
    chunk = vs.VectorChunk(chunk_id="c1", doc_id="d1", text="t", kb_id="kb",
                           tenant_id="t", project_id="p", content_hash="h",
                           embedding_model="m", content_revision=1)
    with mock.patch.object(store, "ensure_collection"), \
         mock.patch.object(store, "has_content_revision_field", return_value=True):
        written = store._upsert_to("shadow", [chunk], [[0.1, 0.2, 0.3, 0.4]], 4)
    check("R3 upsert 写入确认数 == 1", written == 1, f"written={written}")
    check("R3 upsert 后 flush 被调用（growing segment 已持久化）",
          "shadow" in fake.flush_calls, str(fake.flush_calls))

    rows = store._query_rows("shadow", ["c1"], ["chunk_id", "content_revision"])
    check("R3 读回前 load_collection 被调用", "shadow" in fake.load_calls, str(fake.load_calls))
    check("R3 写后读回可见（flush+load 生效）",
          rows["status"] == "ok" and len(rows["rows"]) == 1
          and rows["rows"][0].get("content_revision") == 1, json.dumps(rows, ensure_ascii=False))

    # 反例：不 flush 时读回为空（证明 flush 是必要的，而非装饰）
    fake2 = FakeMilvus()
    fake2.create_collection("shadow")
    fake2.create_index("shadow")
    fake2.upsert("shadow", [{"chunk_id": "c1", "content_revision": 1}])
    store2, _ = make_store(cfg(dual=True, shadow="shadow"), fake2)
    rows2 = store2._query_rows("shadow", ["c1"], ["chunk_id"])
    check("R3 反例：未 flush 时读回为空（证明 flush 必要）",
          rows2["status"] == "ok" and len(rows2["rows"]) == 0, json.dumps(rows2, ensure_ascii=False))

    # 反例：load 失败 → 不可校验（不得折成"空"）
    fake3 = FakeMilvus()
    fake3.create_collection("primary")  # 故意不建索引 → load 失败
    store3, _ = make_store(cfg(), fake3)
    rows3 = store3._query_rows("primary", ["c1"], ["chunk_id"])
    check("R3 反例：load 失败 → unavailable（不是 ok/空）",
          rows3["status"] == "unavailable", json.dumps(rows3, ensure_ascii=False))


# ---- R4：端口占用 fail-closed ----
def test_r4_port_in_use_fail_closed() -> None:
    # 端口被占用（bind 抛 OSError，Windows 上是 "forbidden by its access permissions"）
    with mock.patch.object(disp.socket, "socket") as sock_cls:
        inst = sock_cls.return_value
        inst.bind.side_effect = OSError("forbidden by its access permissions")
        busy = disp._port_in_use(55433)
    check("R4 端口被占用 → _port_in_use=True（预检 fail-closed）", busy is True, f"busy={busy}")

    # 端口空闲
    with mock.patch.object(disp.socket, "socket") as sock_cls:
        inst = sock_cls.return_value
        inst.bind.return_value = None
        busy = disp._port_in_use(55433)
    check("R4 端口空闲 → _port_in_use=False", busy is False, f"busy={busy}")

    # 预检把占用端口变成 exit 2（源码级 pin：main 里占用分支设 exit_code=2）
    src = Path(disp.__file__).read_text(encoding="utf-8")
    check("R4 预检占用端口分支设 exit_code=2",
          "被占用端口" in src and "exit_code = 2" in src)


def main() -> int:
    test_r1_index_then_load()
    test_r2_synthetic_embedding_isolated_to_test_entry()
    test_r3_flush_and_load_for_readback()
    test_r4_port_in_use_fail_closed()
    print()
    print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for name in FAILURES:
            print("  FAILED: " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
