#!/usr/bin/env python3
"""`seed_shared_kb.py`（SEED-SHARED v2-only 数据准备）的**离线契约**回归。

零真实服务连接：PG 查询与 Milvus 适配器全部注入假体，解析产物用临时目录。
覆盖：
  - 归属校验 fail-closed（doc/kb 不一致、重复 chunk_id、不安全标识符、空产物）；
  - dry-run 默认零写入 exit 3，错误令牌不降级（exit 2）；
  - 授权执行：fail-closed 前置（KB 未登记/非 active/docker 失败/collection 缺失/
    schema 缺字段）、PK 冲突绝不覆盖、幂等重跑 written=0、读回失败留全量回滚键；
  - 回滚：全有或全无、外域覆盖拒绝删除、幂等（已不存在视为已回滚）；
  - 确定性向量与 digest 稳定性；模块导入不触发 pymilvus（netguard 兼容）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import seed_shared_kb as s  # noqa: E402

PASSES = []
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSES if cond else FAILURES).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" ({detail})" if detail and not cond else ""))


KB = "34905f75-38ca-4cfa-bf22-c89c96107fa8"
TENANT, PROJECT = "t-seed", "p-seed"


# ---------------------------------------------------------------- 假体（零网络）
class FakeMilvus:
    """进程内假 Milvus：实现 seed 脚本用到的适配器面（含故障注入点）。"""

    def __init__(self, collections: dict, fail_upsert: bool = False,
                 fail_query_after_upsert: bool = False,
                 fail_delete_after_n: int = 0,
                 delete_drops_only_first: bool = False,
                 fail_upsert_after_rows: int = 0,
                 fail_delete_after_apply: bool = False,
                 fail_query_after_delete: bool = False):
        self.collections = collections          # name -> {"dim": int, "fields": [...], "rows": {pk: row}}
        self.fail_upsert = fail_upsert
        self.fail_query_after_upsert = fail_query_after_upsert
        self.fail_delete_after_n = fail_delete_after_n
        self.delete_drops_only_first = delete_drops_only_first
        self.fail_upsert_after_rows = fail_upsert_after_rows
        self.fail_delete_after_apply = fail_delete_after_apply
        self.fail_query_after_delete = fail_query_after_delete
        self.deleted_filters: list = []
        self.upserted: list = []

    def has_collection(self, name):
        return name in self.collections

    def describe(self, name):
        c = self.collections[name]
        fields = []
        for f in c["fields"]:
            if f == "vector":
                fields.append({"name": "vector", "params": {"dim": c["dim"]}})
            else:
                fields.append({"name": f})
        return {"fields": fields}

    def load(self, name):
        assert name in self.collections

    def query(self, name, filter_expr, output_fields, limit=200):
        c = self.collections[name]
        if self.fail_query_after_delete and self.deleted_filters:
            raise RuntimeError("injected query failure after delete")
        if self.fail_query_after_upsert and self.upserted:
            # 模拟"写入成功但读回丢失一行"（fail_query 注入只生效一次）
            rows = dict(c["rows"])
            self.fail_query_after_upsert = False
            if rows:
                rows.pop(sorted(rows)[0], None)
                return [dict(r) for r in rows.values()]
        if "chunk_id in" not in filter_expr:
            raise AssertionError(f"unexpected filter: {filter_expr}")
        # 按 chunk_id 引号字面量粗提取（假体足够：ids 已过 safe_id）
        ids = [part.strip('"') for part in
               filter_expr.split("chunk_id in [", 1)[1].split("]", 1)[0].split(", ")]
        out = [dict(r) for pk, r in c["rows"].items() if pk in set(ids)]
        return out[:limit]

    def upsert(self, name, data):
        if self.fail_upsert:
            raise RuntimeError("injected upsert failure")
        c = self.collections[name]
        applied = 0
        for r in data:
            if self.fail_upsert_after_rows and applied >= self.fail_upsert_after_rows:
                # 模拟"部分行已落库后连接丢失"：响应从未返回，但已应用部分行
                raise RuntimeError("injected upsert failure after partial application")
            self.upserted.append(r["chunk_id"])
            c["rows"][r["chunk_id"]] = dict(r)
            applied += 1
        return {"upsert_count": len(data)}

    def delete(self, name, filter_expr):
        c = self.collections[name]
        if self.fail_delete_after_n and len(self.deleted_filters) >= self.fail_delete_after_n:
            raise RuntimeError("injected delete failure")
        self.deleted_filters.append(filter_expr)
        ids = [part.strip('"') for part in
               filter_expr.split("chunk_id in [", 1)[1].split("]", 1)[0].split(", ")]
        kept_first = None
        for pk in list(c["rows"]):
            if pk in set(ids) and f'kb_id == "{KB}"' in filter_expr:
                if self.delete_drops_only_first:
                    # 模拟引擎"只删了第一个键"的部分删除行为
                    if kept_first is None:
                        kept_first = pk
                        del c["rows"][pk]
                    continue
                del c["rows"][pk]
        if self.fail_delete_after_apply:
            # 模拟"删除已应用但响应丢失"：行已删，客户端收到异常
            raise RuntimeError("injected delete failure after applying deletions")
        return {"delete_count": 0}


def fake_pg_ok(kb, container):
    return 0, f"{TENANT}|{PROJECT}|active"


def make_parsed(root: Path, lines: dict, kb: str = KB):
    for doc, text_lines in lines.items():
        d = root / kb / doc
        d.mkdir(parents=True, exist_ok=True)
        (d / "chunks.jsonl").write_text("".join(text_lines), encoding="utf-8")


def run_seed(argv, milvus=None, pg=None):
    """注入假体后调用 main()；返回 (exit_code)。"""
    orig_open, orig_pg = s._open_milvus, s.pg_query_kb
    s._open_milvus = (lambda uri: milvus) if milvus is not None else orig_open
    s.pg_query_kb = pg or fake_pg_ok
    try:
        return s.main(argv)
    finally:
        s._open_milvus, s.pg_query_kb = orig_open, orig_pg


COLL = "graphinsight_chunks_v2"
DIM = 4


def fresh_collection(**kw):
    return {COLL: {"dim": DIM,
                   "fields": ["chunk_id", "doc_id", "kb_id", "tenant_id", "project_id",
                              "text", "title", "location", "content_hash",
                              "embedding_model", "entities_json", "content_revision", "vector"],
                   "rows": {}}, **kw}


# ---------------------------------------------------------------- 纯函数
def test_safe_id() -> None:
    check("safe_id: uuid 合法", s.safe_id(KB) == KB)
    check("safe_id: chunk 后缀合法", s.safe_id("abc-000") == "abc-000")
    check("safe_id: 空拒绝", s.safe_id("") is None and s.safe_id(None) is None)
    for bad in ("../etc", "a/b", "a b", 'a"b', "a;b", " "):
        check(f"safe_id: {bad!r} 拒绝", s.safe_id(bad) is None)


def test_deterministic_vector() -> None:
    import hashlib
    v1 = s.deterministic_vector("hello", DIM)
    v2 = s.deterministic_vector("hello", DIM)
    check("确定性向量：同 text+dim 稳定", v1 == v2)
    check("确定性向量：dim 生效", len(s.deterministic_vector("hello", 8)) == 8)
    check("确定性向量：不同 text 不同向量",
          s.deterministic_vector("a", DIM) != s.deterministic_vector("b", DIM))
    check("确定性向量：值域 [-1,1]", all(-1.0 <= x <= 1.0 for x in v1))
    d = hashlib.sha256("hello".encode()).digest()
    expected = [round(((d[i % len(d)] / 255.0) - 0.5) * 2.0, 6) for i in range(DIM)]
    check("确定性向量：与 disposable `_det` 同式", v1 == expected, str(v1))
    check("向量 digest：稳定", s.vector_digest(v1) == s.vector_digest(v2))
    check("向量 digest：不同向量不同 digest", s.vector_digest(v1) != s.vector_digest([0.0] * DIM))


def test_collect_parsed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "hello world"}) + "\n",
            json.dumps({"chunk_id": "c-2", "text": "second"}) + "\n",
            json.dumps({"chunk_id": "c-3", "doc_id": "d1", "kb_id": KB, "text": "third"}) + "\n",
        ]})
        rows, issues, stats = s.collect_parsed(KB, root)
        check("归属校验：3 条有效、0 问题",
              len(rows) == 3 and not issues and stats["chunks_total"] == 3, str(issues))
        check("归属校验：缺 doc_id 以目录名为准", rows[1]["doc_id"] == "d1")
        check("归属校验：行内 kb_id 一致放行", rows[2]["kb_id"] == KB)
        check("归属校验：text_sha256 记录", all(r["text_sha256"] for r in rows))

        make_parsed(root, {"d2": [
            "{bad json\n",
            json.dumps({"doc_id": "d2", "text": "no chunk id"}) + "\n",
            json.dumps({"chunk_id": "c-x", "doc_id": "OTHER", "text": "t"}) + "\n",
            json.dumps({"chunk_id": "c-y", "doc_id": "d2", "kb_id": "other-kb", "text": "t"}) + "\n",
            json.dumps({"chunk_id": "c-z", "doc_id": "d2", "text": ""}) + "\n",
            json.dumps({"chunk_id": "c-1", "doc_id": "d2", "text": "dup pk"}) + "\n",
        ]})
        rows, issues, stats = s.collect_parsed(KB, root)
        check("归属校验：d1 3 条仍有效", len(rows) == 3)
        reasons = " | ".join(i["reason"] for i in issues)
        for label in ("json 不可解析", "chunk_id 缺失", "doc_id 与目录名不一致",
                      "行内 kb_id 与目标 KB 不一致", "text 缺失或为空", "chunk_id 重复"):
            check(f"归属校验拒绝：{label}", label in reasons, reasons)

        rows, issues, _ = s.collect_parsed(KB, root / "nope")
        check("归属校验：kb 目录缺失 → 问题且零行", issues and not rows)
        rows, issues, _ = s.collect_parsed("bad kb!", root)
        check("归属校验：kb id 不安全 → 拒绝", any("kb id 非法" in i["reason"] for i in issues))

    with tempfile.TemporaryDirectory() as td:
        (Path(td) / KB).mkdir(parents=True)
        rows, issues, _ = s.collect_parsed(KB, Path(td))
        check("归属校验：空产物 → 拒绝授权", not rows and any("解析产物为空" in i["reason"] for i in issues))


def test_classify_kb_row() -> None:
    check("PG 行：OK 并解析 tenant/project",
          s.classify_kb_row(0, f"{TENANT}|{PROJECT}|active") ==
          {"status": "OK", "tenant_id": TENANT, "project_id": PROJECT, "kb_status": "active"})
    check("PG 行：rc!=0 → UNVERIFIED", s.classify_kb_row(1, "boom")["status"] == "UNVERIFIED")
    check("PG 行：0 行 → BLOCKED 未登记",
          s.classify_kb_row(0, "")["reason"] == "kb_not_registered")
    check("PG 行：多行 → UNVERIFIED",
          s.classify_kb_row(0, "a|b|active\nc|d|active")["status"] == "UNVERIFIED")
    check("PG 行：列数异常 → UNVERIFIED",
          s.classify_kb_row(0, "a|b")["status"] == "UNVERIFIED")
    check("PG 行：非 active → BLOCKED", s.classify_kb_row(0, "t|p|archived")["reason"] == "kb_status=archived")
    check("PG 行：tenant/project 缺失 → BLOCKED",
          s.classify_kb_row(0, "|p|active")["reason"] == "kb_tenant_or_project_missing")


def test_schema_and_decision() -> None:
    dim, fields = s.parse_collection_schema(
        {"fields": [{"name": "chunk_id"}, {"name": "vector", "params": {"dim": 7}}]})
    check("schema 解析：dim/字段", dim == 7 and "chunk_id" in fields)
    check("schema 解析：缺 vector → dim None",
          s.parse_collection_schema({"fields": [{"name": "chunk_id"}]})[0] is None)
    check("schema 解析：非 dict → None", s.parse_collection_schema("x")[0] is None)

    row = {"chunk_id": "c-1", "doc_id": "d1", "kb_id": KB, "tenant_id": TENANT,
           "project_id": PROJECT, "text": "t"}
    vec = s.deterministic_vector("t", DIM)
    check("plan_execution：不存在 → 写入",
          s.plan_execution([row], {}, DIM)["to_write"] == [row])
    live = dict(row, vector=vec)
    check("plan_execution：完全一致 → 幂等跳过",
          s.plan_execution([row], {"c-1": live}, DIM)["skipped"] == ["c-1"])
    for label, mutated in (
        ("跨 tenant 同键（PK 冲突）", dict(row, kb_id=KB, tenant_id="other", vector=vec)),
        ("text 不同", dict(row, text="changed", vector=vec)),
        ("向量不同", dict(row, vector=[0.0] * DIM)),
        ("kb 不同", dict(row, kb_id="other-kb", vector=vec)),
    ):
        res = s.plan_execution([row], {"c-1": mutated}, DIM)
        check(f"plan_execution：{label} → CONFLICT 不覆盖",
              len(res["conflicts"]) == 1 and not res["to_write"], str(res))

    check("verify_readback：一致 → 空", s.verify_readback({"c-1": live}, [row], DIM) == [])
    check("verify_readback：缺失 → 不一致",
          s.verify_readback({}, [row], DIM)[0]["chunk_id"] == "c-1")
    check("verify_readback：向量漂移 → 不一致",
          s.verify_readback({"c-1": dict(row, vector=[0.5] * DIM)}, [row], DIM) != [])

    check("plan_rollback：一致 → deletable",
          s.plan_rollback([row], {"c-1": live}, DIM)["deletable"] == ["c-1"])
    check("plan_rollback：已不存在 → 幂等 absent",
          s.plan_rollback([row], {"c-1": None}, DIM)["already_absent"] == ["c-1"])
    check("plan_rollback：外域覆盖 → CONFLICT",
          s.plan_rollback([row], {"c-1": dict(row, text="hijacked", vector=[0.1] * DIM)}, DIM)["conflicts"])


# ---------------------------------------------------------------- 端到端（假体注入）
def test_plan_mode_zero_write() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        mv = FakeMilvus(fresh_collection())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out)], milvus=mv)
        plan = json.loads((out / "seed-shared-plan.json").read_text(encoding="utf-8"))
        check("dry-run：exit 3", rc == s.EXIT_PLAN_ONLY, str(rc))
        check("dry-run：plan schema v2", plan["schema"] == s.PLAN_SCHEMA)
        check("dry-run：零写入（假 Milvus 无 upsert）", mv.upserted == [])
        check("dry-run：归属校验 OK 且 1 条预览",
              plan["validation"]["ok"] and len(plan["rows_preview"]) == 1)
        check("dry-run：未 probe 时记录 SKIPPED", plan["preflight"]["status"] == "SKIPPED")
        check("dry-run：write scope 仅提案", "note" in plan["write_scope_proposal"])


def test_probe_mode_recorded() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        mv = FakeMilvus(fresh_collection())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out), "--probe"],
                      milvus=mv)
        plan = json.loads((out / "seed-shared-plan.json").read_text(encoding="utf-8"))
        check("probe：exit 3 且零写入", rc == s.EXIT_PLAN_ONLY and mv.upserted == [])
        check("probe：PG OK + Milvus dim/占用被记录",
              plan["preflight"]["pg"]["status"] == "OK"
              and plan["preflight"]["milvus"]["dim"] == DIM
              and plan["preflight"]["milvus"]["occupied_chunk_ids"] == [])


def test_authorize_gate() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        mv = FakeMilvus(fresh_collection())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                       "--authorize", "WRONG-TOKEN"], milvus=mv)
        check("错误令牌：exit 2 且零写入（不降级 dry-run）",
              rc == s.EXIT_FAIL_CLOSED and mv.upserted == [])


def test_execute_happy_and_idempotent() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n",
            json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n",
        ]})
        mv = FakeMilvus(fresh_collection())
        argv = ["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                "--authorize", s.AUTHORIZE_TOKEN]
        rc = run_seed(argv, milvus=mv)
        check("授权执行：exit 0", rc == s.EXIT_OK, str(rc))
        check("授权执行：写入 2 行且带归属字段",
              sorted(mv.upserted) == ["c-1", "c-2"]
              and mv.collections[COLL]["rows"]["c-1"]["tenant_id"] == TENANT
              and mv.collections[COLL]["rows"]["c-1"]["project_id"] == PROJECT)
        check("授权执行：向量 = deterministic(text, dim)",
              mv.collections[COLL]["rows"]["c-1"]["vector"] == s.deterministic_vector("t1", DIM))
        record = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("授权执行：run manifest schema/executed/counts",
              record["schema"] == s.RUN_SCHEMA and record["executed"] is True
              and record["counts"] == {"planned": 2, "written": 2, "already_seeded": 0})
        check("授权执行：manifest 记录 text/vector digest",
              all(r.get("text_sha256") and r.get("vector_sha256") for r in record["rows"]))

        mv2 = FakeMilvus(fresh_collection())
        mv2.collections[COLL]["rows"] = dict(mv.collections[COLL]["rows"])
        rc2 = run_seed(argv, milvus=mv2)
        record2 = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("幂等重跑：exit 0 且 written=0 already_seeded=2",
              rc2 == s.EXIT_OK and mv2.upserted == []
              and record2["counts"] == {"planned": 2, "written": 0, "already_seeded": 2},
              str(record2.get("counts")))


def test_execute_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        line = json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"

        def argv(extra=()):
            return ["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                    "--authorize", s.AUTHORIZE_TOKEN, *extra]

        # KB 未登记 / 非 active / docker 失败
        for label, pg in (
            ("未登记", lambda kb, c: (0, "")),
            ("非 active", lambda kb, c: (0, "t|p|archived")),
            ("docker 失败", lambda kb, c: (1, "connection refused")),
            ("tenant 缺失", lambda kb, c: (0, "|p|active")),
        ):
            make_parsed(root, {"d1": [line]})
            rc = run_seed(argv(), milvus=FakeMilvus(fresh_collection()), pg=pg)
            check(f"前置 fail-closed：KB {label} → exit 2", rc == s.EXIT_FAIL_CLOSED, str(rc))

        # collection 缺失 / schema 缺核心字段
        make_parsed(root, {"d1": [line]})
        rc = run_seed(argv(), milvus=FakeMilvus({}))
        check("前置 fail-closed：collection 缺失 → exit 2", rc == s.EXIT_FAIL_CLOSED)
        make_parsed(root, {"d1": [line]})
        bad_schema = {COLL: {"dim": DIM, "fields": ["chunk_id", "vector"], "rows": {}}}
        rc = run_seed(argv(), milvus=FakeMilvus(bad_schema))
        check("前置 fail-closed：schema 缺核心字段 → exit 2", rc == s.EXIT_FAIL_CLOSED)

        # PK 被外域占用（同 chunk_id 不同 tenant）→ 拒绝且零写入
        make_parsed(root, {"d1": [line]})
        mv = FakeMilvus(fresh_collection())
        mv.collections[COLL]["rows"]["c-1"] = {
            "chunk_id": "c-1", "doc_id": "d1", "kb_id": KB, "tenant_id": "OTHER",
            "project_id": PROJECT, "text": "t1", "vector": s.deterministic_vector("t1", DIM)}
        rc = run_seed(argv(), milvus=mv)
        check("PK 冲突：跨 tenant 同键 → exit 2 零写入",
              rc == s.EXIT_FAIL_CLOSED and mv.upserted == [])

        # upsert 故障 → exit 2（无半截 manifest 键）
        make_parsed(root, {"d1": [line]})
        mv = FakeMilvus(fresh_collection(), fail_upsert=True)
        out_rb = Path(td) / "out_rb"  # 读回失败用例的独立归档目录
        rc = run_seed(argv(), milvus=mv)
        rec = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("upsert 故障：exit 2 且 manifest FAIL_CLOSED",
              rc == s.EXIT_FAIL_CLOSED and rec["status"] == "FAIL_CLOSED")

        # 读回丢一行 → exit 2，manifest 带已写入键（回滚可用）。
        # 独立 out：前一用例（upsert 故障）会残留 journal，R5 守卫会先拒绝新写入。
        make_parsed(root, {"d1": [line, json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n"]})
        mv = FakeMilvus(fresh_collection(), fail_query_after_upsert=True)
        rc = run_seed(argv(["--out", str(out_rb)]), milvus=mv)
        rec = json.loads((out_rb / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("读回失败：exit 2 且 executed=true 带回滚键",
              rc == s.EXIT_FAIL_CLOSED and rec["executed"] is True
              and len(rec.get("rows", [])) >= 1, str(rec.get("rows"))[:120])

        # 解析产物有无效行 → 授权执行被阻止
        make_parsed(root, {"d1": [line, "{bad\n"]})
        rc = run_seed(argv(), milvus=FakeMilvus(fresh_collection()))
        check("无效行：授权执行 fail-closed", rc == s.EXIT_FAIL_CLOSED)


def test_rollback_flow() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        mv = FakeMilvus(fresh_collection())
        seed_argv = ["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                     "--authorize", s.AUTHORIZE_TOKEN]
        run_seed(seed_argv, milvus=mv)
        run_manifest = out / "seed-shared-run.json"

        # 回滚计划：零删除
        rc = run_seed(["--rollback", str(run_manifest), "--out", str(out)], milvus=mv)
        check("回滚计划：exit 3 零删除", rc == s.EXIT_PLAN_ONLY and mv.deleted_filters == [])
        plan = json.loads((out / "seed-shared-rollback-plan.json").read_text(encoding="utf-8"))
        check("回滚计划：记录判据与键", plan["keys"] == ["c-1"] and len(plan["criteria"]) >= 3)

        # 回滚执行：删除 + 读回为零
        rc = run_seed(["--rollback", str(run_manifest), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("回滚执行：exit 0 且行已删除",
              rc == s.EXIT_OK and "c-1" not in mv.collections[COLL]["rows"])
        check("回滚执行：delete filter 带 kb 守卫",
              any(f'kb_id == "{KB}"' in f for f in mv.deleted_filters))
        rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("回滚执行：ROLLBACK manifest 记录 deleted",
              rec["schema"] == s.ROLLBACK_SCHEMA and rec["counts"]["deleted"] == 1)

        # 幂等重跑：全部已不存在 → ALREADY_ROLLED_BACK
        rc = run_seed(["--rollback", str(run_manifest), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("回滚幂等：deleted=0 且 ALREADY_ROLLED_BACK",
              rc == s.EXIT_OK and rec["result"] == "ALREADY_ROLLED_BACK", str(rec))

        # 外域覆盖后的行 → 拒绝删除（全有或全无）
        run_seed(seed_argv, milvus=mv)
        mv.collections[COLL]["rows"]["c-1"]["text"] = "hijacked"
        rc = run_seed(["--rollback", str(run_manifest), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("回滚防误删：外域覆盖 → exit 2 零删除",
              rc == s.EXIT_FAIL_CLOSED and mv.collections[COLL]["rows"].get("c-1") is not None)

        # 非法 manifest 拒绝：schema 不对 / executed!=true
        bad1 = out / "bad-schema.json"
        bad1.write_text(json.dumps({"schema": "whatever"}), encoding="utf-8")
        rc = run_seed(["--rollback", str(bad1), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("回滚：schema 非法的 manifest → exit 2", rc == s.EXIT_FAIL_CLOSED)
        bad2 = out / "bad-executed.json"
        bad2.write_text(json.dumps({"schema": s.RUN_SCHEMA, "executed": False}), encoding="utf-8")
        rc = run_seed(["--rollback", str(bad2), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("回滚：executed!=true 的 manifest → exit 2", rc == s.EXIT_FAIL_CLOSED)
        # --kb 与 manifest 不一致 → exit 2
        rc = run_seed(["--kb", "other-kb", "--rollback", str(run_manifest), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("回滚：--kb 与 manifest 不一致 → exit 2", rc == s.EXIT_FAIL_CLOSED)


def test_no_top_level_pymilvus_import() -> None:
    check("离线安全：模块导入不触发 pymilvus", "pymilvus" not in sys.modules)
    check("离线安全：模块导入不触发 psycopg", "psycopg" not in sys.modules)


# ---------------------------------------------------------------- fix2：共享写目标强制
def test_enforced_milvus_target() -> None:
    ok = s.validate_milvus_target("http://127.0.0.1:19531", "graphinsight_chunks_v2")
    check("目标强制：19531 + v2 通过", ok is None, str(ok))
    for uri, coll, label in (
        ("http://127.0.0.1:19530", "graphinsight_chunks_v2", "19530 拒绝（属其它项目）"),
        ("http://localhost:19531", "graphinsight_chunks_v2", "host 非 127.0.0.1 拒绝"),
        ("http://127.0.0.1:19532", "graphinsight_chunks_v2", "端口偏离拒绝"),
        ("ftp://127.0.0.1:19531", "graphinsight_chunks_v2", "scheme 非法拒绝"),
        ("http://127.0.0.1:notaport", "graphinsight_chunks_v2", "端口不可解析拒绝"),
        ("", "graphinsight_chunks_v2", "空 uri 拒绝"),
        ("http://127.0.0.1:19531", "graphinsight_chunks_v3", "v3 集合拒绝（v2-only）"),
        ("http://127.0.0.1:19531", "graphinsight_chunks_v2_canary", "canary 集合拒绝"),
        ("http://127.0.0.1:19531", "", "空集合拒绝"),
    ):
        err = s.validate_milvus_target(uri, coll)
        check(f"目标强制：{label}", err is not None, str(err))
    check("目标强制：19530 报错点名违规端口",
          "19530" in (s.validate_milvus_target("http://127.0.0.1:19530", "graphinsight_chunks_v2") or ""))


def test_enforced_target_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        mv = FakeMilvus(fresh_collection())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                       "--milvus-uri", "http://127.0.0.1:19530"], milvus=mv)
        check("目标强制 e2e：19530 → exit 2 且不产出 plan",
              rc == s.EXIT_FAIL_CLOSED and not (out / "seed-shared-plan.json").exists())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                       "--collection", "graphinsight_chunks_v3",
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("目标强制 e2e：v3 集合 → exit 2 零写入",
              rc == s.EXIT_FAIL_CLOSED and mv.upserted == [])
        rc = run_seed(["--rollback", str(out / "nonexistent.json"), "--out", str(out),
                       "--milvus-uri", "http://10.0.0.5:19531",
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("目标强制 e2e：rollback 模式同样预校验", rc == s.EXIT_FAIL_CLOSED)


# ---------------------------------------------------------------- fix1：tenant/project 安全校验
def test_tenant_project_safety() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        argv = ["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                "--authorize", s.AUTHORIZE_TOKEN]
        mv = FakeMilvus(fresh_collection())
        rc = run_seed(argv, milvus=mv, pg=lambda kb, c: (0, 'a"b|p|active'))
        rec = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("fix1：PG tenant 含引号 → exit 2 零写入",
              rc == s.EXIT_FAIL_CLOSED and mv.upserted == [], str(rec.get("failed_step")))
        check("fix1：fail record 指向 pg_kb_check", rec.get("failed_step") == "pg_kb_check")

        # plan --probe：PG OK 但 tenant 不安全 → 提案保持占位，不回填脏值
        out2 = Path(td) / "out2"
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out2), "--probe"],
                      milvus=FakeMilvus(fresh_collection()), pg=lambda kb, c: (0, 'a"b|p|active'))
        plan = json.loads((out2 / "seed-shared-plan.json").read_text(encoding="utf-8"))
        check("fix1：probe 提案不回填不安全 tenant",
              rc == s.EXIT_PLAN_ONLY
              and plan["write_scope_proposal"]["MILVUS_WRITE_SCOPE_TENANT"] == "(以 PG 核验结果为准)",
              json.dumps(plan["write_scope_proposal"], ensure_ascii=False))

        # manifest loader 收紧：tenant 缺失/不安全均拒绝回滚
        mv2 = FakeMilvus(fresh_collection())
        run_seed(argv, milvus=mv2)
        run_manifest = out / "seed-shared-run.json"
        good = json.loads(run_manifest.read_text(encoding="utf-8"))
        for label, mutate in (("缺失", {"tenant_id": ""}), ("不安全", {"tenant_id": 'a"b'})):
            bad = out / f"bad-{label}.json"
            bad.write_text(json.dumps({**good, **mutate}), encoding="utf-8")
            rc = run_seed(["--rollback", str(bad), "--out", str(out),
                           "--authorize", s.AUTHORIZE_TOKEN], milvus=mv2)
            check(f"fix1：manifest tenant {label} → 回滚 exit 2 零删除",
                  rc == s.EXIT_FAIL_CLOSED and mv2.deleted_filters == [])


# ---------------------------------------------------------------- fix3：回滚部分删除语义
def test_rollback_partial_semantics() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n",
            json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n",
        ]})
        argv = ["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                "--authorize", s.AUTHORIZE_TOKEN]
        rb_argv = ["--rollback", str(out / "seed-shared-run.json"), "--out", str(out),
                   "--authorize", s.AUTHORIZE_TOKEN]

        # 场景 A：批次中断（第 2 批抛异常）→ PARTIAL 如实入账 + 幂等收敛
        mv = FakeMilvus(fresh_collection())
        run_seed(argv, milvus=mv)
        orig_batch = s.BATCH
        try:
            s.BATCH = 1  # 两键两批，制造"第 1 批成功、第 2 批失败"
            mv2 = FakeMilvus(fresh_collection())
            mv2.collections[COLL]["rows"] = dict(mv.collections[COLL]["rows"])
            mv2.fail_delete_after_n = 1
            rc = run_seed(rb_argv, milvus=mv2)
            rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
            check("fix3 A：批次中断 → exit 2 PARTIAL",
                  rc == s.EXIT_FAIL_CLOSED and rec["status"] == "PARTIAL"
                  and rec["executed"] is True, str(rec.get("status")))
            check("fix3 A：账本 deleted=[c-1] pending=[c-2]",
                  rec["deleted_keys"] == ["c-1"] and rec["pending_keys"] == ["c-2"]
                  and rec["counts"] == {"deleted": 1, "already_absent": 0, "pending": 1},
                  str(rec.get("counts")))
            check("fix3 A：c-1 确已删除、c-2 未动",
                  mv2.collections[COLL]["rows"].get("c-1") is None
                  and mv2.collections[COLL]["rows"].get("c-2") is not None)
            mv2.fail_delete_after_n = 0  # 清除注入，验证收敛
            rc = run_seed(rb_argv, milvus=mv2)  # 重跑：幂等收敛
            rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
            check("fix3 A：重跑收敛 exit 0（deleted=[c-2] absent=[c-1]）",
                  rc == s.EXIT_OK and rec["counts"] == {"deleted": 1, "already_absent": 1}
                  and not mv2.collections[COLL]["rows"], str(rec.get("counts")))
        finally:
            s.BATCH = orig_batch

        # 场景 B：读回发现"只删了第一个键" → 以读回为准入账 PARTIAL + 重跑收敛
        mv3 = FakeMilvus(fresh_collection())
        run_seed(argv, milvus=mv3)
        mv4 = FakeMilvus(fresh_collection(), delete_drops_only_first=True)
        mv4.collections[COLL]["rows"] = dict(mv3.collections[COLL]["rows"])
        rc = run_seed(rb_argv, milvus=mv4)
        rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("fix3 B：部分删陠除键 → exit 2 PARTIAL（读回为准）",
              rc == s.EXIT_FAIL_CLOSED and rec["status"] == "PARTIAL"
              and rec["deleted_keys"] == ["c-1"] and rec["pending_keys"] == ["c-2"],
              str(rec.get("deleted_keys")))
        rc = run_seed(rb_argv, milvus=mv4)
        rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("fix3 B：重跑收敛 exit 0", rc == s.EXIT_OK
              and rec["result"] == "ROLLED_BACK" and not mv4.collections[COLL]["rows"])


# ---------------------------------------------------------------- fix4：manifest 落盘失败的紧急证据
def test_manifest_emergency_evidence() -> None:
    import io
    from contextlib import redirect_stderr

    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        argv = ["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                "--authorize", s.AUTHORIZE_TOKEN]
        orig_write = s._write_text

        # 场景 A：主路径失败 → 同目录 emergency 后备接住，exit 2（不许 exit 0 假绿）
        def fail_primary(path, text):
            if Path(path).name == "seed-shared-run.json":
                raise OSError("injected primary manifest write failure")
            return orig_write(path, text)

        mv = FakeMilvus(fresh_collection())
        s._write_text = fail_primary
        try:
            rc = run_seed(argv, milvus=mv)
        finally:
            s._write_text = orig_write
        emergencies = list(out.glob("seed-shared-run.emergency.*.json"))
        check("fix4 A：主路径失败 → exit 2（数据已写入）",
              rc == s.EXIT_FAIL_CLOSED and sorted(mv.upserted) == ["c-1"], str(rc))
        check("fix4 A：同目录 emergency manifest 落盘且内容完整",
              len(emergencies) == 1
              and json.loads(emergencies[0].read_text(encoding="utf-8"))["executed"] is True
              and json.loads(emergencies[0].read_text(encoding="utf-8"))["status"] == "OK")

        # 场景 B：run manifest 全部候选写后损坏（journal 放行，写入发生）→
        # stderr BEGIN/END 包裹完整 JSON + exit 2 + journal 残留可恢复
        mv2 = FakeMilvus(fresh_collection())
        err_buf = io.StringIO()

        def corrupt_run_manifest_everywhere(path, text):
            if Path(path).name.startswith("seed-shared-run"):
                return orig_write(path, text[:5] + "{corrupt")
            return orig_write(path, text)

        s._write_text = corrupt_run_manifest_everywhere
        try:
            with redirect_stderr(err_buf):
                rc = run_seed(argv, milvus=mv2)
        finally:
            s._write_text = orig_write
        err_text = err_buf.getvalue()
        check("fix4 B：run manifest 全候选损坏 → exit 2（写入已发生）",
              rc == s.EXIT_FAIL_CLOSED and sorted(mv2.upserted) == ["c-1"], str(rc))
        check("fix4 B：stderr 含 BEGIN/END 紧急标记",
              s.EMERGENCY_BEGIN in err_text and s.EMERGENCY_END in err_text)
        try:
            dumped = err_text.split(s.EMERGENCY_BEGIN, 1)[1].split(s.EMERGENCY_END, 1)[0]
            payload = json.loads(dumped)
            check("fix4 B：stderr 内 JSON 可解析且 executed=true",
                  payload["executed"] is True and payload["counts"]["written"] == 1)
        except Exception as exc:  # noqa: BLE001
            check(f"fix4 B：stderr 内 JSON 可解析且 executed=true（解析失败 {exc}）", False)
        check("fix4 B：journal 残留，崩溃/证据丢失后仍可恢复",
              (out / "seed-shared-journal.json").is_file())

        # 场景 C：回滚成功 manifest 落盘失败 → exit 2 + emergency 接住。
        # 前置：场景 B 残留的 journal 按 R5 合同先回滚收敛（journal 被清理）。
        rc_resolve = run_seed(["--rollback", str(out / "seed-shared-journal.json"),
                               "--out", str(out), "--authorize", s.AUTHORIZE_TOKEN], milvus=mv2)
        check("fix4 C 前置：残留 journal 回滚收敛并清理",
              rc_resolve == s.EXIT_OK and not (out / "seed-shared-journal.json").is_file(),
              str(rc_resolve))
        run_seed(argv, milvus=FakeMilvus(fresh_collection()))
        rb_argv = ["--rollback", str(out / "seed-shared-run.json"), "--out", str(out),
                   "--authorize", s.AUTHORIZE_TOKEN]
        mv3 = FakeMilvus(fresh_collection())
        mv3.collections[COLL]["rows"] = {
            "c-1": {"chunk_id": "c-1", "doc_id": "d1", "kb_id": KB, "tenant_id": TENANT,
                    "project_id": PROJECT, "text": "t1",
                    "vector": s.deterministic_vector("t1", DIM)}}

        def fail_rb_primary(path, text):
            if Path(path).name == "seed-shared-rollback.json":
                raise OSError("injected rollback manifest write failure")
            return orig_write(path, text)

        s._write_text = fail_rb_primary
        try:
            rc = run_seed(rb_argv, milvus=mv3)
        finally:
            s._write_text = orig_write
        rb_emergencies = list(out.glob("seed-shared-rollback.emergency.*.json"))
        check("fix4 C：回滚 manifest 落盘失败 → exit 2 + emergency 落盘",
              rc == s.EXIT_FAIL_CLOSED and len(rb_emergencies) == 1
              and json.loads(rb_emergencies[0].read_text(encoding="utf-8"))["schema"] == s.ROLLBACK_SCHEMA
              and mv3.collections[COLL]["rows"] == {}, str(rc))

        # 场景 D：plan 落盘失败 → exit 2（不许 exit 3 假装已归档）
        def fail_plan(path, text):
            if Path(path).name == "seed-shared-plan.json":
                raise OSError("injected plan write failure")
            return orig_write(path, text)

        out2 = Path(td) / "out2"
        s._write_text = fail_plan
        try:
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out2)], milvus=mv3)
        finally:
            s._write_text = orig_write
        check("fix4 D：plan 落盘失败 → exit 2 且 emergency plan 落盘",
              rc == s.EXIT_FAIL_CLOSED and len(list(out2.glob("seed-shared-plan.emergency.*.json"))) == 1)


# ---------------------------------------------------------------- P1-A：令牌 + manifest 目标门禁
def test_p1a_token_and_manifest_target_gate() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        mv = FakeMilvus(fresh_collection())
        run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                  "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        run_manifest = out / "seed-shared-run.json"
        good = json.loads(run_manifest.read_text(encoding="utf-8"))

        def rows_alive():
            return "c-1" in mv.collections[COLL]["rows"]

        # 错误令牌 + 回滚执行 → exit 2 零删除（令牌校验先于模式分发）
        rc = run_seed(["--rollback", str(run_manifest), "--out", str(out),
                       "--authorize", "WRONG-TOKEN"], milvus=mv)
        check("P1-A：错误令牌 + 回滚执行 → exit 2 零删除",
              rc == s.EXIT_FAIL_CLOSED and mv.deleted_filters == [] and rows_alive())
        # 错误令牌 + 回滚计划 → 同样拒绝（不得降级为计划模式）
        rc = run_seed(["--rollback", str(run_manifest), "--out", str(out),
                       "--authorize", "WRONG-TOKEN"], milvus=mv)
        check("P1-A：错误令牌 + 回滚计划 → exit 2（不降级）",
              rc == s.EXIT_FAIL_CLOSED and not (out / "seed-shared-rollback-plan.json").exists())

        # manifest 篡改目标 → loader 拒收（纯函数）+ 回滚零删除（e2e）
        for label, mutate in (
            ("collection→v3", {"collection": "graphinsight_chunks_v3"}),
            ("milvus_uri→19530", {"milvus_uri": "http://127.0.0.1:19530"}),
            ("milvus_uri 缺失", {"milvus_uri": ""}),
        ):
            bad = out / f"tampered-{label.replace('→', '-').replace(' ', '')}.json"
            bad.write_text(json.dumps({**good, **mutate}), encoding="utf-8")
            m, err = s._load_run_manifest(bad)
            check(f"P1-A：loader 拒收 {label}", m is None and "写目标非法" in err, str(err))
            rc = run_seed(["--rollback", str(bad), "--out", str(out),
                           "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
            check(f"P1-A：篡改 {label} → 回滚 exit 2 零删除",
                  rc == s.EXIT_FAIL_CLOSED and mv.deleted_filters == [] and rows_alive())


# ---------------------------------------------------------------- P1-B：可恢复写入账本
def test_p1b_write_ledger_and_recoverable_fail_manifest() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n",
            json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n",
        ]})
        # 部分应用后丢失响应：c-1 已落库、c-2 未落库、客户端当批全失败
        mv = FakeMilvus(fresh_collection(), fail_upsert_after_rows=1)
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("P1-B：部分应用丢失响应 → exit 2", rc == s.EXIT_FAIL_CLOSED, str(rc))
        rec = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("P1-B：失败 manifest 带完整运行上下文（tenant/project/collection/dim/uri）",
              rec.get("tenant_id") == TENANT and rec.get("project_id") == PROJECT
              and rec.get("collection") == COLL and rec.get("dim") == DIM
              and rec.get("milvus_uri") == s.ENFORCED_MILVUS_URI,
              str({k: rec.get(k) for k in ("tenant_id", "project_id", "collection", "dim", "milvus_uri")}))
        check("P1-B：账本 confirmed=[c-1] unknown=[c-2] pending=[]",
              rec.get("write_ledger") == {"confirmed": ["c-1"], "unknown": ["c-2"], "pending": []},
              str(rec.get("write_ledger")))
        check("P1-B：失败 manifest 收录 dispatched 期望行（c-1、c-2）",
              sorted(r["chunk_id"] for r in rec.get("rows", [])) == ["c-1", "c-2"],
              str(rec.get("rows"))[:120])
        m, err = s._load_run_manifest(out / "seed-shared-run.json")
        check("P1-B：失败 manifest 可被 loader 接受", m is not None, err)

        # 用失败 manifest 回滚 → 清理实际写入的 c-1；c-2 从未落库 → already_absent
        rc = run_seed(["--rollback", str(out / "seed-shared-run.json"), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        rec_rb = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("P1-B：失败 manifest 回滚收敛 exit 0（c-1 删除、c-2 已不存在）",
              rc == s.EXIT_OK and rec_rb["counts"] == {"deleted": 1, "already_absent": 1}
              and not mv.collections[COLL]["rows"], str(rec_rb.get("counts")))


# ---------------------------------------------------------------- P1-C：写入所有权
def test_p1c_ownership() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n",
            json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n",
        ]})
        # 预存一致行 c-1（内容与 seed 期望完全一致，模拟先前 seed 运行留下）
        mv = FakeMilvus(fresh_collection())
        mv.collections[COLL]["rows"]["c-1"] = {
            "chunk_id": "c-1", "doc_id": "d1", "kb_id": KB, "tenant_id": TENANT,
            "project_id": PROJECT, "text": "t1", "vector": s.deterministic_vector("t1", DIM)}
        run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                  "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        rec = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("P1-C：成功 manifest 只收 owned rows（c-2）",
              [r["chunk_id"] for r in rec["rows"]] == ["c-2"], str(rec["rows"])[:120])
        check("P1-C：already_seeded 键单独记录且不入回滚集",
              rec["already_seeded_keys"] == ["c-1"]
              and rec["counts"] == {"planned": 2, "written": 1, "already_seeded": 1},
              str(rec.get("counts")))

        rc = run_seed(["--rollback", str(out / "seed-shared-run.json"), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("P1-C：回滚只删本次写入的 c-2，预存行 c-1 存活",
              rc == s.EXIT_OK and "c-2" not in mv.collections[COLL]["rows"]
              and "c-1" in mv.collections[COLL]["rows"], str(rc))

        # 纯 rerun（无 owned rows）：两键都已预存且与期望一致 → manifest rows=[]
        mv2 = FakeMilvus(fresh_collection())
        for cid, txt in (("c-1", "t1"), ("c-2", "t2")):
            mv2.collections[COLL]["rows"][cid] = {
                "chunk_id": cid, "doc_id": "d1", "kb_id": KB, "tenant_id": TENANT,
                "project_id": PROJECT, "text": txt, "vector": s.deterministic_vector(txt, DIM)}
        out2 = Path(td) / "out2"
        run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out2),
                  "--authorize", s.AUTHORIZE_TOKEN], milvus=mv2)
        rec2 = json.loads((out2 / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("P1-C：幂等 rerun manifest rows 为空",
              rec2["result"] == "ALREADY_SEEDED" and rec2["rows"] == [],
              str(rec2.get("result")))
        rc = run_seed(["--rollback", str(out2 / "seed-shared-run.json"), "--out", str(out2),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv2)
        check("P1-C：空 owned rows 的 manifest 回滚 → exit 2 且预存行存活",
              rc == s.EXIT_FAIL_CLOSED and "c-1" in mv2.collections[COLL]["rows"]
              and "c-2" in mv2.collections[COLL]["rows"], str(rc))


# ---------------------------------------------------------------- P1-D：manifest 落盘读回校验
def test_p1d_manifest_readback_verify() -> None:
    import io
    from contextlib import redirect_stderr

    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        argv = ["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                "--authorize", s.AUTHORIZE_TOKEN]
        orig_write = s._write_text

        # 主路径写后静默损坏（截断 + 非法 JSON）→ 读回校验失败 → emergency 接住
        def corrupt_primary(path, text):
            if Path(path).name == "seed-shared-run.json":
                return orig_write(path, text[: max(1, len(text) // 3)] + "{corrupt")
            return orig_write(path, text)

        mv = FakeMilvus(fresh_collection())
        s._write_text = corrupt_primary
        try:
            rc = run_seed(argv, milvus=mv)
        finally:
            s._write_text = orig_write
        emergencies = list(out.glob("seed-shared-run.emergency.*.json"))
        check("P1-D：主路径静默损坏 → exit 2（数据已写入）",
              rc == s.EXIT_FAIL_CLOSED and sorted(mv.upserted) == ["c-1"], str(rc))
        check("P1-D：emergency 证据完好且经读回校验",
              len(emergencies) == 1
              and json.loads(emergencies[0].read_text(encoding="utf-8"))["executed"] is True)

        # 全候选（run manifest）写后损坏（journal 放行）→ stderr 紧急 dump + exit 2。
        # 独立 out：场景 A 残留的损坏主文件会先触发 superseded 留存（副本同样被
        # 损坏 → R5-2 拒绝覆盖），走不到本场景目标分支。
        out_b = Path(td) / "out_b"
        mv2 = FakeMilvus(fresh_collection())
        err_buf = io.StringIO()
        s._write_text = lambda path, text: (
            orig_write(path, text[:5] + "{corrupt")
            if Path(path).name.startswith("seed-shared-run") else orig_write(path, text))
        argv_b = ["--kb", KB, "--parsed-root", str(root), "--out", str(out_b),
                  "--authorize", s.AUTHORIZE_TOKEN]
        try:
            with redirect_stderr(err_buf):
                rc = run_seed(argv_b, milvus=mv2)
        finally:
            s._write_text = orig_write
        err_text = err_buf.getvalue()
        check("P1-D：全候选损坏 → exit 2", rc == s.EXIT_FAIL_CLOSED and sorted(mv2.upserted) == ["c-1"])
        check("P1-D：stderr 紧急 dump 含 BEGIN/END 且 JSON 完整可解析",
              s.EMERGENCY_BEGIN in err_text and s.EMERGENCY_END in err_text
              and json.loads(err_text.split(s.EMERGENCY_BEGIN, 1)[1]
                             .split(s.EMERGENCY_END, 1)[0])["executed"] is True)


# ---------------------------------------------------------------- R1：同目录重跑不毁所有权证据
def test_r1_rerun_preserves_ownership_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        argv = ["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                "--authorize", s.AUTHORIZE_TOKEN]
        mv = FakeMilvus(fresh_collection())
        run_seed(argv, milvus=mv)          # run1: c-1 owned（写入）
        run_seed(argv, milvus=mv)          # run2 同目录同库 rerun：c-1 已在库一致
        rec2 = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        superseded = list(out.glob("seed-shared-run.superseded.*.json"))
        check("R1：同目录 rerun 后 manifest 为 ALREADY_SEEDED 且记录 supersedes",
              rec2["result"] == "ALREADY_SEEDED" and rec2["rows"] == []
              and rec2.get("supersedes") and Path(rec2["supersedes"]).is_file(),
              str(rec2.get("supersedes")))
        check("R1：superseded 副本持有原 owned rows",
              len(superseded) == 1
              and [r["chunk_id"] for r in json.loads(
                  superseded[0].read_text(encoding="utf-8"))["rows"]] == ["c-1"])
        rc = run_seed(["--rollback", rec2["supersedes"], "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN],
                      milvus=FakeMilvus(fresh_collection()))
        check("R1：首次写入的证据仍可回滚（superseded 副本）",
              rc == s.EXIT_OK, str(rc))

        # 留存失败 → 拒绝覆盖（原 manifest 原样保留）
        out2 = Path(td) / "out2"
        owned = {"schema": s.RUN_SCHEMA, "executed": True, "kb_id": KB,
                 "tenant_id": TENANT, "project_id": PROJECT, "collection": COLL,
                 "dim": DIM, "milvus_uri": s.ENFORCED_MILVUS_URI,
                 "rows": [{"chunk_id": "z-1", "doc_id": "d1", "kb_id": KB,
                           "tenant_id": TENANT, "project_id": PROJECT, "text": "z",
                           "text_sha256": s.text_sha256("z")}]}
        out2.mkdir(parents=True)
        (out2 / "seed-shared-run.json").write_text(json.dumps(owned), encoding="utf-8")
        orig_write = s._write_text

        def block_superseded(path, text):
            if ".superseded." in Path(path).name:
                raise OSError("injected superseded preservation failure")
            return orig_write(path, text)

        s._write_text = block_superseded
        try:
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out2),
                           "--authorize", s.AUTHORIZE_TOKEN],
                          milvus=FakeMilvus(fresh_collection()))
        finally:
            s._write_text = orig_write
        check("R1：留存失败 → exit 2 拒绝覆盖且原 owned manifest 原样保留",
              rc == s.EXIT_FAIL_CLOSED
              and json.loads((out2 / "seed-shared-run.json").read_text(encoding="utf-8")) == owned)


# ---------------------------------------------------------------- R2：mutation journal 崩溃恢复
def test_r2_journal_crash_recovery() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n",
            json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n",
        ]})

        class HardCrash(FakeMilvus):
            """模拟 upsert 部分应用后进程硬中断（SystemExit 绕过 except Exception）。"""

            def upsert(self, name, data):
                applied = 0
                cc = self.collections[name]
                for r in data:
                    if applied >= self.fail_upsert_after_rows:
                        raise SystemExit("simulated hard crash mid-upsert")
                    cc["rows"][r["chunk_id"]] = dict(r)
                    self.upserted.append(r["chunk_id"])
                    applied += 1
                return {"upsert_count": len(data)}

        mv = HardCrash(fresh_collection(), fail_upsert_after_rows=1)
        try:
            run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                      "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
            crashed = False
        except SystemExit:
            crashed = True
        check("R2：进程硬中断复现（journal 前置于写入）", crashed)
        check("R2：崩溃后无 run manifest 但 journal 在盘",
              not (out / "seed-shared-run.json").exists()
              and (out / "seed-shared-journal.json").is_file())
        jr = json.loads((out / "seed-shared-journal.json").read_text(encoding="utf-8"))
        check("R2：journal 含完整上下文与 mutation 目标期望行",
              jr["schema"] == s.JOURNAL_SCHEMA and jr["kb_id"] == KB
              and jr["tenant_id"] == TENANT and jr["collection"] == COLL
              and sorted(r["chunk_id"] for r in jr["rows"]) == ["c-1", "c-2"])
        m, err = s._load_run_manifest(out / "seed-shared-journal.json")
        check("R2：loader 接受 journal 作为回滚输入", m is not None, err)
        rc = run_seed(["--rollback", str(out / "seed-shared-journal.json"), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("R2：journal 回滚收敛（c-1 删除、c-2 已不存在）",
              rc == s.EXIT_OK and rec["counts"] == {"deleted": 1, "already_absent": 1}
              and not mv.collections[COLL]["rows"], str(rec.get("counts")))

        # 成功路径：manifest 落盘后 journal 被清理
        out2 = Path(td) / "out2"
        run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out2),
                  "--authorize", s.AUTHORIZE_TOKEN], milvus=FakeMilvus(fresh_collection()))
        check("R2：成功运行后 journal 已清理",
              (out2 / "seed-shared-run.json").is_file()
              and not (out2 / "seed-shared-journal.json").is_file())


# ---------------------------------------------------------------- R3：删除响应丢失按实际状态入账
def test_r3_delete_response_loss_ledger() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n",
            json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n",
        ]})
        run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                  "--authorize", s.AUTHORIZE_TOKEN], milvus=FakeMilvus(fresh_collection()))
        rb_argv = ["--rollback", str(out / "seed-shared-run.json"), "--out", str(out),
                   "--authorize", s.AUTHORIZE_TOKEN]

        def two_rows():
            return {"c-1": {"chunk_id": "c-1", "doc_id": "d1", "kb_id": KB,
                            "tenant_id": TENANT, "project_id": PROJECT, "text": "t1",
                            "vector": s.deterministic_vector("t1", DIM)},
                    "c-2": {"chunk_id": "c-2", "doc_id": "d1", "kb_id": KB,
                            "tenant_id": TENANT, "project_id": PROJECT, "text": "t2",
                            "vector": s.deterministic_vector("t2", DIM)}}

        # 场景 A：删除已应用但响应丢失 → 读回按实际状态入账 verified PARTIAL
        mv = FakeMilvus(fresh_collection(), fail_delete_after_apply=True)
        mv.collections[COLL]["rows"] = two_rows()
        rc = run_seed(rb_argv, milvus=mv)
        rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("R3 A：删除响应丢失 → exit 2 PARTIAL（verified=true 按实际状态）",
              rc == s.EXIT_FAIL_CLOSED and rec["status"] == "PARTIAL"
              and rec["verified"] is True
              and rec["deleted_keys"] == ["c-1", "c-2"] and rec["pending_keys"] == [],
              str({k: rec.get(k) for k in ("status", "verified", "deleted_keys", "pending_keys")}))
        check("R3 A：记录与实际存储一致（两行确已不在）", not mv.collections[COLL]["rows"])
        rc = run_seed(rb_argv, milvus=mv)
        rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("R3 A：重跑收敛 ALREADY_ROLLED_BACK",
              rc == s.EXIT_OK and rec["result"] == "ALREADY_ROLLED_BACK")

        # 场景 B：删除已应用 + 读回也失败 → PARTIAL_UNVERIFIED（账本估计，不伪装精确）
        mv2 = FakeMilvus(fresh_collection(), fail_delete_after_apply=True,
                         fail_query_after_delete=True)
        mv2.collections[COLL]["rows"] = two_rows()
        rc = run_seed(rb_argv, milvus=mv2)
        rec = json.loads((out / "seed-shared-rollback.json").read_text(encoding="utf-8"))
        check("R3 B：读回失败 → PARTIAL_UNVERIFIED 且 verified=false",
              rc == s.EXIT_FAIL_CLOSED and rec["status"] == "PARTIAL_UNVERIFIED"
              and rec["verified"] is False, str(rec.get("status")))
        check("R3 B：实际存储中两行确已删除（record 未声称精确）",
              not mv2.collections[COLL]["rows"])
        mv2.fail_query_after_delete = False  # 恢复读回能力
        rc = run_seed(rb_argv, milvus=mv2)
        check("R3 B：读回恢复后重跑收敛 exit 0", rc == s.EXIT_OK)


# ---------------------------------------------------------------- R5：journal 未处置守卫 + superseded 副本完整性
def test_r5_journal_pending_and_superseded_integrity() -> None:
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n",
            json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n",
        ]})

        class HardCrash(FakeMilvus):
            def upsert(self, name, data):
                cc = self.collections[name]
                for r in data:
                    raise SystemExit("simulated hard crash before applying batch")

        # 崩溃留下 journal（写入前已持久化）
        mv = HardCrash(fresh_collection())
        try:
            run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                      "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        except SystemExit:
            pass
        check("R5 前置：崩溃后 journal 在盘", (out / "seed-shared-journal.json").is_file())

        # 旧 journal 未处置 → 新执行被拒绝（旧恢复依据不被覆盖/删除）
        mv2 = FakeMilvus(fresh_collection())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv2)
        rec = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("R5：未处置 journal → 新写入 exit 2（journal_pending）零写入",
              rc == s.EXIT_FAIL_CLOSED and rec.get("failed_step") == "journal_pending"
              and mv2.upserted == [], str(rec.get("failed_step")))
        check("R5：旧 journal 原样保留",
              (out / "seed-shared-journal.json").is_file())

        # 回滚 journal 收敛 → journal 自动清理 → 新执行放行
        rc = run_seed(["--rollback", str(out / "seed-shared-journal.json"), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        check("R5：journal 回滚收敛且 journal 被清理",
              rc == s.EXIT_OK and not (out / "seed-shared-journal.json").is_file(), str(rc))
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=FakeMilvus(fresh_collection()))
        check("R5：journal 处置后新执行放行", rc == s.EXIT_OK, str(rc))

        # superseded 副本静默损坏 → 读回比较拒绝覆盖，原 owned manifest 原样保留
        out2 = Path(td) / "out2"
        owned = {"schema": s.RUN_SCHEMA, "executed": True, "kb_id": KB,
                 "tenant_id": TENANT, "project_id": PROJECT, "collection": COLL,
                 "dim": DIM, "milvus_uri": s.ENFORCED_MILVUS_URI,
                 "rows": [{"chunk_id": "z-1", "doc_id": "d1", "kb_id": KB,
                           "tenant_id": TENANT, "project_id": PROJECT, "text": "z",
                           "text_sha256": s.text_sha256("z")}]}
        out2.mkdir(parents=True)
        (out2 / "seed-shared-run.json").write_text(json.dumps(owned), encoding="utf-8")
        orig_write = s._write_text

        def corrupt_superseded(path, text):
            if ".superseded." in Path(path).name:
                return orig_write(path, text[:5] + "{corrupt")
            return orig_write(path, text)

        s._write_text = corrupt_superseded
        try:
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out2),
                           "--authorize", s.AUTHORIZE_TOKEN],
                          milvus=FakeMilvus(fresh_collection()))
        finally:
            s._write_text = orig_write
        check("R5：superseded 副本静默损坏 → exit 2 拒绝覆盖",
              rc == s.EXIT_FAIL_CLOSED, str(rc))
        check("R5：原 owned manifest 内容原样保留（所有权证据未丢失）",
              json.loads((out2 / "seed-shared-run.json").read_text(encoding="utf-8")) == owned)
        check("R5：损坏的 superseded 副本不冒充证据（无完好副本时拒绝）",
              all("corrupt" in p.read_text(encoding="utf-8")
                  for p in out2.glob("seed-shared-run.superseded.*.json"))
              or not list(out2.glob("seed-shared-run.superseded.*.json")))


def test_r5_uri_whitespace() -> None:
    check("R5：URI 首部空格拒绝",
          s.validate_milvus_target(" http://127.0.0.1:19531", "graphinsight_chunks_v2") is not None)
    check("R5：URI 尾部空格拒绝",
          s.validate_milvus_target("http://127.0.0.1:19531 ", "graphinsight_chunks_v2") is not None)
    check("R5：collection 首尾空格拒绝",
          s.validate_milvus_target("http://127.0.0.1:19531", " graphinsight_chunks_v2") is not None)
    check("R5：精确 URI/collection 仍通过",
          s.validate_milvus_target("http://127.0.0.1:19531", "graphinsight_chunks_v2") is None)


# ---------------------------------------------------------------- R6：journal 证据可见性
def test_r6_journal_evidence_visibility() -> None:
    """R6-1 规范 journal 名被非普通文件占用不得放行；R6-2 journal 证据降级须拒绝写入。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "parsed"
        make_parsed(root, {"d1": [
            json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})

        # R6-1：规范 journal 名被目录占用 → 视为存在未处置证据 → 拒绝新写入、零 upsert
        out_dir = Path(td) / "out_dir"
        (out_dir / "seed-shared-journal.json").mkdir(parents=True)
        mv = FakeMilvus(fresh_collection())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out_dir),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv)
        rec = json.loads((out_dir / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("R6-1：journal 名被目录占用 → exit 2（journal_pending）零写入",
              rc == s.EXIT_FAIL_CLOSED and rec.get("failed_step") == "journal_pending"
              and mv.upserted == [], f"rc={rc} step={rec.get('failed_step')} up={mv.upserted}")

        # R6-2：规范 journal 路径不可写（_write_record 降级到 emergency）→ 拒绝写入
        out2 = Path(td) / "out2"
        orig_write = s._write_text

        def fail_canonical_journal(path, text):
            if Path(path).name == "seed-shared-journal.json":
                raise OSError("injected: canonical journal path unwritable")
            return orig_write(path, text)

        s._write_text = fail_canonical_journal
        mv2 = FakeMilvus(fresh_collection())
        try:
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out2),
                           "--authorize", s.AUTHORIZE_TOKEN], milvus=mv2)
        finally:
            s._write_text = orig_write
        rec2 = json.loads((out2 / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("R6-2：journal 证据降级到后备 → 拒绝写入（journal_write）零 upsert",
              rc == s.EXIT_FAIL_CLOSED and rec2.get("failed_step") == "journal_write"
              and mv2.upserted == [], f"rc={rc} step={rec2.get('failed_step')} up={mv2.upserted}")
        check("R6-2：规范 journal 未落盘（证据不隐身）",
              not (out2 / "seed-shared-journal.json").is_file())

        # R6-3：首次规范写失败 → 后备成功 → 规范重试成功，须清理 orphan emergency journal
        out3 = Path(td) / "out3"
        orig_write3 = s._write_text
        calls = {"canon": 0}

        def flaky_canonical(path, text):
            if Path(path).name == "seed-shared-journal.json":
                calls["canon"] += 1
                if calls["canon"] == 1:
                    raise OSError("injected: first canonical attempt fails")
            return orig_write3(path, text)

        s._write_text = flaky_canonical
        mv3 = FakeMilvus(fresh_collection())
        try:
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out3),
                           "--authorize", s.AUTHORIZE_TOKEN], milvus=mv3)
        finally:
            s._write_text = orig_write3
        check("R6-3：规范写失败→后备→规范重试成功 仍 exit 0",
              rc == s.EXIT_OK and mv3.upserted == ["c-1"], f"rc={rc} up={mv3.upserted}")
        check("R6-3：无 orphan emergency journal 遗留",
              not list(out3.glob("seed-shared-journal.emergency.*.json")),
              str([p.name for p in out3.glob("*.json")]))
        check("R6-3：成功运行后 journal 正常清理、run manifest 落盘",
              not (out3 / "seed-shared-journal.json").is_file()
              and (out3 / "seed-shared-run.json").is_file())

        # R6-4：悬空符号链接占用规范名 → lexists 语义拒绝新写入
        out4 = Path(td) / "out4"
        out4.mkdir(parents=True)
        link = out4 / "seed-shared-journal.json"
        try:
            os.symlink(str(out4 / "nonexistent-target.json"), str(link))
            dangling_ok = True
        except (OSError, NotImplementedError):
            dangling_ok = False
        if dangling_ok:
            mv4 = FakeMilvus(fresh_collection())
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out4),
                           "--authorize", s.AUTHORIZE_TOKEN], milvus=mv4)
            rec4 = json.loads((out4 / "seed-shared-run.json").read_text(encoding="utf-8"))
            check("R6-4：悬空符号链接占用 journal 名 → exit 2 零写入",
                  rc == s.EXIT_FAIL_CLOSED and rec4.get("failed_step") == "journal_pending"
                  and mv4.upserted == [], f"rc={rc} step={rec4.get('failed_step')}")
        else:
            check("R6-4：悬空符号链接用例跳过（环境不支持 symlink）", True)

        # R6-5：后备清理 raise OSError → 写前拒绝（exit 2、零 upsert、保留规范 journal、
        # 结构化证据记录后备路径与清理异常）；不再只打警告后返回成功。
        out5 = Path(td) / "out5"
        orig_write5 = s._write_text
        orig_unlink5 = s._unlink
        calls5 = {"canon": 0}

        def flaky_canonical5(path, text):
            if Path(path).name == "seed-shared-journal.json":
                calls5["canon"] += 1
                if calls5["canon"] == 1:
                    raise OSError("injected: first canonical attempt fails")
            return orig_write5(path, text)

        def fail_unlink(path):
            raise OSError("injected: fallback journal cleanup fails")

        s._write_text = flaky_canonical5
        s._unlink = fail_unlink
        mv5 = FakeMilvus(fresh_collection())
        try:
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out5),
                           "--authorize", s.AUTHORIZE_TOKEN], milvus=mv5)
        finally:
            s._write_text = orig_write5
            s._unlink = orig_unlink5
        rec5 = json.loads((out5 / "seed-shared-run.json").read_text(encoding="utf-8"))
        fallbacks = list(out5.glob("seed-shared-journal.emergency.*.json"))
        check("R6-5：后备清理失败 → exit 2（journal_fallback_cleanup）零 upsert",
              rc == s.EXIT_FAIL_CLOSED
              and rec5.get("failed_step") == "journal_fallback_cleanup"
              and mv5.upserted == [], f"rc={rc} step={rec5.get('failed_step')} up={mv5.upserted}")
        check("R6-5：结构化证据记录后备路径与清理异常",
              rec5.get("detail", {}).get("fallback_journal")
              and "fallback_journal" in json.dumps(rec5.get("detail", {}))
              and "cleanup_error" in rec5.get("detail", {})
              and fallbacks and rec5["detail"]["fallback_journal"] == str(fallbacks[0]),
              json.dumps(rec5.get("detail", {}), ensure_ascii=False)[:200])
        check("R6-5：规范 journal 保留（固化后的权威证据不丢）",
              (out5 / "seed-shared-journal.json").is_file())

        # R6-5 正常路径对照：不注入 _unlink → 后备清理成功、exit 0、无 orphan
        out6 = Path(td) / "out6"
        orig_write6 = s._write_text
        calls6 = {"canon": 0}

        def flaky_canonical6(path, text):
            if Path(path).name == "seed-shared-journal.json":
                calls6["canon"] += 1
                if calls6["canon"] == 1:
                    raise OSError("injected: first canonical attempt fails")
            return orig_write6(path, text)

        s._write_text = flaky_canonical6
        mv6 = FakeMilvus(fresh_collection())
        try:
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out6),
                           "--authorize", s.AUTHORIZE_TOKEN], milvus=mv6)
        finally:
            s._write_text = orig_write6
        check("R6-5 对照：后备清理成功 → exit 0 且无 orphan emergency journal",
              rc == s.EXIT_OK and mv6.upserted == ["c-1"]
              and not list(out6.glob("seed-shared-journal.emergency.*.json")),
              f"rc={rc} up={mv6.upserted}")

        # R6-6：R6-5 失败 → 规范 journal 回滚 → emergency journal 仍在 → 新执行必须拒绝
        out7 = Path(td) / "out7"
        orig_write7, orig_unlink7 = s._write_text, s._unlink
        calls7 = {"canon": 0}

        def flaky_canonical7(path, text):
            if Path(path).name == "seed-shared-journal.json":
                calls7["canon"] += 1
                if calls7["canon"] == 1:
                    raise OSError("injected: first canonical attempt fails")
            return orig_write7(path, text)

        def fail_unlink7(path):
            raise OSError("injected: fallback cleanup fails")

        # 步骤 1：制造 R6-5 失败（规范 + emergency journal 均保留）
        s._write_text, s._unlink = flaky_canonical7, fail_unlink7
        mv7a = FakeMilvus(fresh_collection())
        try:
            rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out7),
                           "--authorize", s.AUTHORIZE_TOKEN], milvus=mv7a)
        finally:
            s._write_text, s._unlink = orig_write7, orig_unlink7
        check("R6-6 前置：R6-5 失败后规范 + emergency journal 均在盘",
              rc == s.EXIT_FAIL_CLOSED and (out7 / "seed-shared-journal.json").is_file()
              and list(out7.glob("seed-shared-journal.emergency.*.json")),
              f"rc={rc}")

        # 步骤 2：用规范 journal 回滚收敛（规范 journal 被删除，emergency 仍在）
        mv7b = FakeMilvus(fresh_collection())
        rc = run_seed(["--rollback", str(out7 / "seed-shared-journal.json"), "--out", str(out7),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv7b)
        check("R6-6 前置：规范 journal 回滚收敛且规范 journal 被清理",
              rc == s.EXIT_OK and not (out7 / "seed-shared-journal.json").is_file()
              and list(out7.glob("seed-shared-journal.emergency.*.json")), f"rc={rc}")

        # 步骤 3：残留 emergency journal → 新执行必须拒绝、零 upsert
        mv7c = FakeMilvus(fresh_collection())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out7),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv7c)
        rec7 = json.loads((out7 / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("R6-6：残留 emergency journal → exit 2（journal_pending）零 upsert",
              rc == s.EXIT_FAIL_CLOSED and rec7.get("failed_step") == "journal_pending"
              and mv7c.upserted == [], f"rc={rc} step={rec7.get('failed_step')} up={mv7c.upserted}")
        check("R6-6：失败证据列出残留后备路径",
              len(rec7.get("detail", {}).get("backups", [])) >= 1,
              json.dumps(rec7.get("detail", {}), ensure_ascii=False)[:160])

        # 步骤 4：清理 emergency journal 后新执行放行
        for p in out7.glob("seed-shared-journal.emergency.*.json"):
            p.unlink()
        mv7d = FakeMilvus(fresh_collection())
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out7),
                       "--authorize", s.AUTHORIZE_TOKEN], milvus=mv7d)
        check("R6-6：清理 emergency journal 后新执行放行",
              rc == s.EXIT_OK and mv7d.upserted == ["c-1"], f"rc={rc} up={mv7d.upserted}")


# ---------------------------------------------------------------- R4：URI 逐字符精确
def test_r4_strict_uri() -> None:
    check("R4：精确 URI 通过",
          s.validate_milvus_target("http://127.0.0.1:19531", "graphinsight_chunks_v2") is None)
    for uri, label in (
        ("https://127.0.0.1:19531", "https 变体拒绝"),
        ("http://127.0.0.1:19531/", "尾斜杠路径拒绝"),
        ("http://127.0.0.1:19531?x=1", "query 拒绝"),
        ("HTTP://127.0.0.1:19531", "大小写变体拒绝"),
    ):
        check(f"R4：{label}", s.validate_milvus_target(uri, "graphinsight_chunks_v2") is not None)
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "parsed", Path(td) / "out"
        make_parsed(root, {"d1": [json.dumps({"chunk_id": "c-1", "doc_id": "d1", "text": "t1"}) + "\n"]})
        rc = run_seed(["--kb", KB, "--parsed-root", str(root), "--out", str(out),
                       "--milvus-uri", "http://127.0.0.1:19531/"],
                      milvus=FakeMilvus(fresh_collection()))
        check("R4 e2e：尾斜杠 URI → exit 2 且不产出 plan",
              rc == s.EXIT_FAIL_CLOSED and not (out / "seed-shared-plan.json").exists())


def main() -> int:
    test_safe_id()
    test_deterministic_vector()
    test_collect_parsed()
    test_classify_kb_row()
    test_schema_and_decision()
    test_plan_mode_zero_write()
    test_probe_mode_recorded()
    test_authorize_gate()
    test_execute_happy_and_idempotent()
    test_execute_fail_closed()
    test_rollback_flow()
    test_no_top_level_pymilvus_import()
    test_enforced_milvus_target()
    test_enforced_target_end_to_end()
    test_tenant_project_safety()
    test_rollback_partial_semantics()
    test_manifest_emergency_evidence()
    test_p1a_token_and_manifest_target_gate()
    test_p1b_write_ledger_and_recoverable_fail_manifest()
    test_p1c_ownership()
    test_p1d_manifest_readback_verify()
    test_r1_rerun_preserves_ownership_evidence()
    test_r2_journal_crash_recovery()
    test_r3_delete_response_loss_ledger()
    test_r4_strict_uri()
    test_r5_journal_pending_and_superseded_integrity()
    test_r5_uri_whitespace()
    test_r6_journal_evidence_visibility()

    print(f"\n  passed={len(PASSES)} failed={len(FAILURES)}")
    if FAILURES:
        print("  FAILED:")
        for f in FAILURES:
            print(f"    - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
