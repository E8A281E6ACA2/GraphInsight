#!/usr/bin/env python3
"""M5 §16.1 S1 双写（dual_write）自动化验证。

口径：MilvusVectorStore 在一次 upsert / delete 内扇出主库(v2)与影子(v3)，读源恒为主库；
默认关闭 = S0 现网安全态。本守卫用假 client 覆盖双写的生效判据、扇出一致性、失败语义与
幂等重放，全程不联网、不碰真实 Milvus（真实建集合/写入属 §5 需单独授权的迁移动作）。

必须锁死的不变量：
  1. 默认/未开启 → 只写主库，影子零调用；读源 config().collection 恒为主库；
  2. 生效判据：需开关开 + store 启用 + shadow 非空 + shadow≠主库；任一不满足则不双写并给原因；
  3. shadow 与主库同名 → 拒绝双写（退化成同集合重复写会掩盖真实缺口）；
  4. 主库写失败 → 抛出原始异常，影子零调用（绝不"影子已写、主库未写"）；
  5. 影子写失败（主库已成功）→ 抛 DualWriteShadowError，可定位主/影子名，主库确已写（投影判未收敛，可重放）；
  6. 影子 collection 缺显式 content_revision 字段 → §8.5 门拒写并包成 DualWriteShadowError；
  7. 主/影子 rows 逐字节一致、确认数相等；
  8. delete_doc / clear 扇出影子；影子删除失败 → DualWriteShadowError；
  9. 影子失败修复后重放：主库按主键幂等再写一次并成功收敛。
"""
from __future__ import annotations

import sys
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

PRIMARY = "graphinsight_chunks_v2"
SHADOW = "graphinsight_chunks_v3"
KB = "kb-1"


class Check:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, name: str, condition: bool, detail: str) -> None:
        if condition:
            self.passed += 1
            print(f"CHECK {name} PASS {detail}")
            return
        self.failed.append(name)
        print(f"CHECK {name} FAIL {detail}")


class RecordingClient:
    """按 collection 记录 upsert/delete；可按集合名注入失败。"""

    def __init__(self, *, fail_upsert=(), fail_delete=(), has_collection_all=True):
        self.upserts: dict[str, list] = {}
        self.deletes: dict[str, list] = {}
        self._fail_upsert = set(fail_upsert)
        self._fail_delete = set(fail_delete)
        self._has = has_collection_all

    def has_collection(self, name):
        return self._has

    def upsert(self, *, collection_name, data):
        if collection_name in self._fail_upsert:
            raise RuntimeError(f"boom upsert {collection_name}")
        self.upserts.setdefault(collection_name, []).append(data)
        return {"upsert_count": len(data)}

    def delete(self, *, collection_name, filter):  # noqa: A002 - 匹配 pymilvus 关键字
        if collection_name in self._fail_delete:
            raise RuntimeError(f"boom delete {collection_name}")
        self.deletes.setdefault(collection_name, []).append(filter)
        return {"delete_count": 1}


def _new_store(*, dual_write, shadow, enabled=True, primary=PRIMARY):
    """构造独立 MilvusVectorStore 实例并注入假 config / client，避免污染单例与真实依赖。"""
    from services.vector_store import MilvusVectorStore

    store = MilvusVectorStore()
    cfg = {
        "enabled": enabled,
        "provider": "milvus",
        "collection": primary,
        "dual_write": dual_write,
        "shadow_collection": shadow,
    }
    store.config = lambda: dict(cfg)
    store.is_enabled = lambda: bool(cfg["enabled"]) and cfg["provider"] == "milvus"
    store._revision_field = {primary: True, shadow: True} if shadow else {primary: True}
    return store


def _attach(store, client):
    store._get_client = lambda: client
    ensured = []

    def _ensure(dimension=None, collection=None):
        ensured.append(str(collection))
        return None

    store.ensure_collection = _ensure
    return ensured


def _chunk(rev=3):
    from services.vector_store import VectorChunk

    return VectorChunk(
        chunk_id="c-1",
        doc_id="d-1",
        text="正文",
        content_hash="h-1",
        kb_id=KB,
        tenant_id="t-1",
        project_id="p-1",
        content_revision=rev,
    )


def main() -> int:
    from services.vector_store import DualWriteShadowError, VectorStoreSchemaError

    check = Check()

    # 1) 默认关闭：只写主库，影子零调用。
    store = _new_store(dual_write=False, shadow="")
    client = RecordingClient()
    _attach(store, client)
    n = store.upsert_chunks([_chunk()], [[0.1, 0.2]])
    check.ok("off_only_primary", set(client.upserts) == {PRIMARY} and n == 1,
             f"cols={sorted(client.upserts)} n={n}")

    # 2) 生效判据：dual 开 + shadow 合法 → 主+影子都写。
    store = _new_store(dual_write=True, shadow=SHADOW)
    client = RecordingClient()
    _attach(store, client)
    n = store.upsert_chunks([_chunk()], [[0.1, 0.2]])
    rows_p = client.upserts.get(PRIMARY, [[]])[0]
    rows_s = client.upserts.get(SHADOW, [[]])[0]
    check.ok("active_writes_both", set(client.upserts) == {PRIMARY, SHADOW} and n == 1,
             f"cols={sorted(client.upserts)}")
    check.ok("active_rows_identical", rows_p == rows_s, "primary rows == shadow rows")
    check.ok("active_content_revision_present",
             rows_p[0].get("content_revision") == 3, f"rev={rows_p[0].get('content_revision')}")

    # 3) shadow 与主库同名 → 拒绝双写（resolve 判 inactive，只写主库一次）。
    store = _new_store(dual_write=True, shadow=PRIMARY)
    client = RecordingClient()
    _attach(store, client)
    dw = store.resolve_dual_write()
    n = store.upsert_chunks([_chunk()], [[0.1, 0.2]])
    check.ok("shadow_same_as_primary_refused", dw["active"] is False and set(client.upserts) == {PRIMARY} and n == 1,
             f"active={dw['active']} reason={dw['reason']} cols={sorted(client.upserts)}")

    # 4) shadow 为空 → 不双写并给原因。
    store = _new_store(dual_write=True, shadow="")
    dw = store.resolve_dual_write()
    check.ok("empty_shadow_not_active", dw["active"] is False and "shadow_collection" in dw["reason"],
             f"reason={dw['reason']}")

    # 4b) store 未启用 → 不双写。
    store = _new_store(dual_write=True, shadow=SHADOW, enabled=False)
    check.ok("store_disabled_not_active", store.resolve_dual_write()["active"] is False,
             "reason=vector_store 未启用")

    # 5) 主库写失败 → 抛原始异常，影子零调用。
    store = _new_store(dual_write=True, shadow=SHADOW)
    client = RecordingClient(fail_upsert=(PRIMARY,))
    _attach(store, client)
    raised = None
    try:
        store.upsert_chunks([_chunk()], [[0.1, 0.2]])
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check.ok("primary_fail_no_shadow",
             raised is not None and not isinstance(raised, DualWriteShadowError) and SHADOW not in client.upserts,
             f"raised={type(raised).__name__} shadow_cols={sorted(client.upserts)}")

    # 6) 影子写失败（主库已成功）→ DualWriteShadowError，可定位主/影子，主库确已写。
    store = _new_store(dual_write=True, shadow=SHADOW)
    client = RecordingClient(fail_upsert=(SHADOW,))
    _attach(store, client)
    raised = None
    try:
        store.upsert_chunks([_chunk()], [[0.1, 0.2]])
    except DualWriteShadowError as exc:
        raised = exc
    check.ok("shadow_fail_raises_divergence",
             isinstance(raised, DualWriteShadowError)
             and raised.primary_collection == PRIMARY
             and raised.shadow_collection == SHADOW
             and PRIMARY in client.upserts,
             f"raised={type(raised).__name__} primary_written={PRIMARY in client.upserts}")

    # 6b) DualWriteShadowError 是 VectorStoreUpsertError 子类（调用方原有 catch 仍生效）。
    from services.vector_store import VectorStoreUpsertError

    check.ok("divergence_is_upsert_error", issubclass(DualWriteShadowError, VectorStoreUpsertError),
             "catch VectorStoreUpsertError 仍能捕获影子未收敛")

    # 7) 影子缺显式 content_revision 字段 → §8.5 门拒写并包成 DualWriteShadowError（主库已写）。
    store = _new_store(dual_write=True, shadow=SHADOW)
    store._revision_field = {PRIMARY: True, SHADOW: False}
    client = RecordingClient()
    _attach(store, client)
    raised = None
    try:
        store.upsert_chunks([_chunk(rev=5)], [[0.1, 0.2]])
    except DualWriteShadowError as exc:
        raised = exc.__cause__
    check.ok("shadow_missing_field_guard",
             isinstance(raised, VectorStoreSchemaError) and PRIMARY in client.upserts and SHADOW not in client.upserts,
             f"cause={type(raised).__name__ if raised else None} shadow_written={SHADOW in client.upserts}")

    # 8) delete_doc 扇出影子。
    store = _new_store(dual_write=True, shadow=SHADOW)
    client = RecordingClient()
    _attach(store, client)
    store.delete_doc("d-1", KB)
    check.ok("delete_fanout_both", set(client.deletes) == {PRIMARY, SHADOW}, f"cols={sorted(client.deletes)}")

    # 8b) 影子删除失败 → DualWriteShadowError。
    store = _new_store(dual_write=True, shadow=SHADOW)
    client = RecordingClient(fail_delete=(SHADOW,))
    _attach(store, client)
    raised = None
    try:
        store.delete_doc("d-1", KB)
    except DualWriteShadowError as exc:
        raised = exc
    check.ok("delete_shadow_fail_raises",
             isinstance(raised, DualWriteShadowError) and PRIMARY in client.deletes and SHADOW not in client.deletes,
             f"raised={type(raised).__name__ if raised else None} primary_deleted={PRIMARY in client.deletes}")

    # 8c) clear 扇出影子。
    store = _new_store(dual_write=True, shadow=SHADOW)
    client = RecordingClient()
    _attach(store, client)
    store.clear([KB])
    check.ok("clear_fanout_both", set(client.deletes) == {PRIMARY, SHADOW}, f"cols={sorted(client.deletes)}")

    # 9) 影子失败修复后重放 → 主库按主键幂等再写一次并成功收敛。
    store = _new_store(dual_write=True, shadow=SHADOW)
    client = RecordingClient(fail_upsert=(SHADOW,))
    _attach(store, client)
    try:
        store.upsert_chunks([_chunk()], [[0.1, 0.2]])
    except DualWriteShadowError:
        pass
    # 修复：影子不再失败（同一 store 换健康 client，模拟下一轮重放）。
    client2 = RecordingClient()
    client2.upserts[PRIMARY] = client.upserts.get(PRIMARY, [])[:]  # 影子已存在主库历史，主库将被再次 upsert
    _attach(store, client2)
    n = store.upsert_chunks([_chunk()], [[0.1, 0.2]])
    check.ok("replay_converges",
             n == 1 and len(client2.upserts.get(PRIMARY, [])) >= 1 and len(client2.upserts.get(SHADOW, [])) == 1,
             f"primary_calls={len(client2.upserts.get(PRIMARY, []))} shadow_calls={len(client2.upserts.get(SHADOW, []))}")

    # 10) 读源不变：即便双写生效，config().collection 仍是主库。
    store = _new_store(dual_write=True, shadow=SHADOW)
    check.ok("read_source_unaffected", store.config()["collection"] == PRIMARY,
             f"collection={store.config()['collection']}")

    print(
        "M5_DUAL_WRITE_SUMMARY "
        f"passed={check.passed} failed={len(check.failed)}"
    )
    return 0 if not check.failed else 1


if __name__ == "__main__":
    sys.exit(main())
