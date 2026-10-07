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
                 fail_query_after_upsert: bool = False):
        self.collections = collections          # name -> {"dim": int, "fields": [...], "rows": {pk: row}}
        self.fail_upsert = fail_upsert
        self.fail_query_after_upsert = fail_query_after_upsert
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
        for r in data:
            self.upserted.append(r["chunk_id"])
            c["rows"][r["chunk_id"]] = dict(r)
        return {"upsert_count": len(data)}

    def delete(self, name, filter_expr):
        c = self.collections[name]
        self.deleted_filters.append(filter_expr)
        ids = [part.strip('"') for part in
               filter_expr.split("chunk_id in [", 1)[1].split("]", 1)[0].split(", ")]
        for pk in list(c["rows"]):
            if pk in set(ids) and f'kb_id == "{KB}"' in filter_expr:
                del c["rows"][pk]
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
        rc = run_seed(argv(), milvus=mv)
        rec = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
        check("upsert 故障：exit 2 且 manifest FAIL_CLOSED",
              rc == s.EXIT_FAIL_CLOSED and rec["status"] == "FAIL_CLOSED")

        # 读回丢一行 → exit 2，manifest 带已写入键（回滚可用）
        make_parsed(root, {"d1": [line, json.dumps({"chunk_id": "c-2", "doc_id": "d1", "text": "t2"}) + "\n"]})
        mv = FakeMilvus(fresh_collection(), fail_query_after_upsert=True)
        rc = run_seed(argv(), milvus=mv)
        rec = json.loads((out / "seed-shared-run.json").read_text(encoding="utf-8"))
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

    print(f"\n  passed={len(PASSES)} failed={len(FAILURES)}")
    if FAILURES:
        print("  FAILED:")
        for f in FAILURES:
            print(f"    - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
