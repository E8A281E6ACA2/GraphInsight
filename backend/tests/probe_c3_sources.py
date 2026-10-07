#!/usr/bin/env python3
"""共享目标 KB 的**四来源只读统计**（backfill_versioned 前置报告）。

四来源：
  1. PG `chunk_revisions`（current 行，按 kb）；
  2. Neo4j `(:Chunk)` 节点（按 kb_id）；
  3. Milvus collections / row count / 按 kb 的 chunk；
  4. 解析产物 `parsed_documents/<kb>/<doc>/chunks.jsonl`。

访问方式（**只读**）：
  - PG / Neo4j：`docker exec` 在容器内用 psql / cypher-shell 只读查询；
  - Milvus：同 Docker 网络临时容器（`--rm`，stdin 传脚本）用 urllib 调 RESTful 只读接口；
  - 解析产物：宿主侧只读遍历目录（不写不删）。

**fail-closed 判定（本轮修复）**：判定逻辑抽成纯函数 `classify_pg / classify_neo4j /
classify_milvus`，任一支查询非成功即整来源 `UNVERIFIED`：
  - PG/Neo4j：三个子查询任一 `rc != 0` → UNVERIFIED；
  - Milvus：任一响应 `code != 0`、含 `error`、或不是合法 JSON → UNVERIFIED
    （此前"业务错误 JSON 但进程退出码 0"会被误标 OK，属 fail-open）。

禁止：任何写入、建集合、播种、入队、修改配置。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NET = "graphinsight_default"
PG_CONTAINER = "graphinsight-postgres"
NEO4J_CONTAINER = "graphinsight-neo4j"
MILVUS_HOST = "graphinsight-milvus"
PY_IMAGE = "python:3.11-slim"

MILVUS_SCRIPT = r'''
import json, urllib.request, urllib.error
BASE = "http://{milvus}:19530"
KB = "{kb}"
def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                headers={{"Content-Type": "application/json"}})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
    except Exception as e:
        return {{"error": type(e).__name__ + ": " + str(e)[:200]}}
out = {{"collections": post("/v2/vectordb/collections/list", {{}})}}
out["stats"] = {{}}
out["by_kb"] = {{}}
for c in (out["collections"].get("data") or []):
    out["stats"][c] = post("/v2/vectordb/collections/get_stats", {{"collectionName": c}})
    try:
        post("/v2/vectordb/collections/load", {{"collectionName": c}})
    except Exception:
        pass
    out["by_kb"][c] = post("/v2/vectordb/entities/query",
        {{"collectionName": c, "filter": 'kb_id == "' + KB + '"',
          "outputFields": ["chunk_id"], "limit": 200}})
print(json.dumps(out, ensure_ascii=False))
'''

# (rc, out) 形状，便于纯函数判定与负向测试
Pair = Tuple[int, str]


def run(cmd, stdin: str = None, timeout: int = 120) -> Pair:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 纯判定（可测）
def classify_pg(total: Pair, per_kb: Pair, current: Pair) -> Dict[str, Any]:
    """PG 三支查询**全部**成功才 OK；任一 rc != 0 → UNVERIFIED（fail-closed）。"""
    if total[0] != 0 or per_kb[0] != 0 or current[0] != 0:
        bad = [n for n, r in (("total", total), ("per_kb", per_kb), ("current", current))
               if r[0] != 0]
        detail = next((r[1] for n, r in (("total", total), ("per_kb", per_kb), ("current", current))
                       if r[0] != 0), "")
        return {"status": "UNVERIFIED", "failed_queries": bad, "detail": detail[:300]}
    return {"status": "OK",
            "total": (total[1] or "").strip(),
            "per_kb": [l for l in (per_kb[1] or "").splitlines() if l.strip()],
            "target_kb_current": (current[1] or "").strip()}


def classify_neo4j(total: Pair, per_kb: Pair, sample: Pair) -> Dict[str, Any]:
    """Neo4j 三支查询全部成功才 OK；分组/目标 KB 查询失败也必须 UNVERIFIED。"""
    if total[0] != 0 or per_kb[0] != 0 or sample[0] != 0:
        bad = [n for n, r in (("total", total), ("per_kb", per_kb), ("sample", sample))
               if r[0] != 0]
        detail = next((r[1] for n, r in (("total", total), ("per_kb", per_kb), ("sample", sample))
                       if r[0] != 0), "")
        return {"status": "UNVERIFIED", "failed_queries": bad, "detail": detail[:300]}
    lines = [l for l in (total[1] or "").splitlines() if l.strip()]
    return {"status": "OK",
            "total": lines[-1:] or ["0"],
            "per_kb": [l for l in (per_kb[1] or "").splitlines() if l.strip()],
            "target_kb_sample": [l for l in (sample[1] or "").splitlines() if l.strip()]}


def _resp_code(resp: Any) -> Optional[int]:
    """取 Milvus 响应码：非 dict、含 error、无合法 code 均返回 None（视为失败）。"""
    if not isinstance(resp, dict):
        return None
    if "error" in resp:
        return None
    code = resp.get("code")
    # Milvus 的业务码必须是 JSON 整数 0；例如 0.5、"0"、True 都是
    # 不可验证的响应，不能通过 int() 强转后误判为成功。
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def classify_milvus(data: Any) -> Dict[str, Any]:
    """Milvus：collections / 每个 stats / 每个 by_kb 的 **code 必须全为 0**。

    此前只看容器进程退出码，业务错误 JSON（`code != 0`）会被误标 OK —— 属 fail-open。
    """
    if not isinstance(data, dict):
        return {"status": "UNVERIFIED", "detail": f"payload not a dict: {type(data).__name__}"}
    collections = data.get("collections")
    coll_code = _resp_code(collections)
    if coll_code != 0:
        return {"status": "UNVERIFIED", "detail": f"collections code={coll_code} resp={str(collections)[:200]}"}
    if not isinstance(collections.get("data"), list):
        return {"status": "UNVERIFIED", "detail": "collections missing list data"}
    names = list(collections["data"])
    stats = data.get("stats")
    by_kb = data.get("by_kb")
    if not isinstance(stats, dict) or not isinstance(by_kb, dict):
        return {"status": "UNVERIFIED", "detail": "missing stats/by_kb response maps"}
    missing_stats = [str(name) for name in names if name not in stats]
    missing_by_kb = [str(name) for name in names if name not in by_kb]
    if missing_stats or missing_by_kb:
        return {
            "status": "UNVERIFIED",
            "detail": f"missing sub-queries stats={missing_stats} by_kb={missing_by_kb}",
        }
    for name, resp in list(stats.items()) + list(by_kb.items()):
        c = _resp_code(resp)
        if c != 0:
            return {"status": "UNVERIFIED",
                    "detail": f"sub-query {name} code={c} resp={str(resp)[:200]}"}
    return {"status": "OK",
            "collections": list(names),
            "stats": stats,
            "by_kb": by_kb,
            "v3_exists": any("v3" in str(n) for n in names)}


# ---------------------------------------------------------------- 采集（只读）
def pg_stats(kb: str) -> Dict[str, Any]:
    total = run(["docker", "exec", PG_CONTAINER, "psql", "-U", "graphinsight", "-d",
                 "graphinsight_admin", "-t", "-A", "-c", "SELECT count(*) FROM chunk_revisions;"])
    per_kb = run(["docker", "exec", PG_CONTAINER, "psql", "-U", "graphinsight", "-d",
                  "graphinsight_admin", "-t", "-A", "-c",
                  "SELECT kb_id, count(*) FROM chunk_revisions GROUP BY kb_id ORDER BY 2 DESC;"])
    cur = run(["docker", "exec", PG_CONTAINER, "psql", "-U", "graphinsight", "-d",
               "graphinsight_admin", "-t", "-A", "-c",
               f"SELECT count(*) FROM chunk_revisions WHERE kb_id='{kb}' "
               f"AND revision_status='current';"])
    return classify_pg(total, per_kb, cur)


def neo4j_stats(kb: str) -> Dict[str, Any]:
    base = ["docker", "exec", NEO4J_CONTAINER, "cypher-shell", "-u", "neo4j", "-p",
            "change-this-password", "--non-interactive", "--format", "plain"]
    total = run(base + ["MATCH (c:Chunk) RETURN count(c) AS n;"])
    per_kb = run(base + ["MATCH (c:Chunk) RETURN c.kb_id AS kb, count(c) AS n ORDER BY n DESC;"])
    sample = run(base + [f"MATCH (c:Chunk {{kb_id:'{kb}'}}) RETURN c.chunk_id AS chunk_id, "
                         f"c.doc_id AS doc_id LIMIT 20;"])
    return classify_neo4j(total, per_kb, sample)


def milvus_stats(kb: str) -> Dict[str, Any]:
    script = MILVUS_SCRIPT.format(milvus=MILVUS_HOST, kb=kb)
    rc, out = run(["docker", "run", "-i", "--rm", "--network", NET, PY_IMAGE, "python", "-"],
                  stdin=script, timeout=300)
    if rc != 0:
        return {"status": "UNVERIFIED", "detail": (out or "")[:300]}
    line = ""
    for l in (out or "").splitlines():
        if l.strip().startswith("{"):
            line = l.strip()
    if not line:
        return {"status": "UNVERIFIED", "detail": f"no JSON payload: {(out or '')[:200]}"}
    try:
        data = json.loads(line)
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNVERIFIED", "detail": f"{type(exc).__name__}: {exc}"}
    return classify_milvus(data)


def parsed_stats(kb: str, root: Path) -> Dict[str, Any]:
    """解析产物：parsed_documents/<kb>/<doc>/chunks.jsonl 只读统计（失败即 UNVERIFIED）。"""
    try:
        if not root.exists():
            return {"status": "UNVERIFIED", "detail": f"root missing: {root}"}
        kb_dir = root / kb
        entries: List[Dict[str, Any]] = []
        if kb_dir.is_dir():
            for doc_dir in sorted(p for p in kb_dir.iterdir() if p.is_dir()):
                chunks = doc_dir / "chunks.jsonl"
                n, bad = 0, 0
                if chunks.is_file():
                    for l in chunks.read_text(encoding="utf-8", errors="replace").splitlines():
                        if not l.strip():
                            continue
                        try:
                            obj = json.loads(l)
                            if isinstance(obj, dict) and str(obj.get("chunk_id") or "").strip():
                                n += 1
                            else:
                                bad += 1
                        except Exception:  # noqa: BLE001
                            bad += 1
                entries.append({"doc": doc_dir.name, "chunks": n, "invalid_lines": bad,
                                "manifest": (doc_dir / "manifest.json").is_file()})
        all_kbs = sorted(p.name for p in root.iterdir() if p.is_dir())
        return {"status": "OK", "root": str(root), "target_kb_dir_exists": kb_dir.is_dir(),
                "target_kb_docs": entries,
                "target_kb_chunks_total": sum(e["chunks"] for e in entries),
                "all_kb_dirs": all_kbs[:20]}
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNVERIFIED", "detail": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser(description="共享目标 KB 四来源只读统计")
    ap.add_argument("--kb", required=True)
    ap.add_argument("--parsed-root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {
        "schema": "gate0-c3-sources-probe-v2",
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "kb_id": args.kb,
        "parsed_root": args.parsed_root,
        "read_only": True,
        "sources": {},
    }
    print(f"目标 KB: {args.kb}")
    print(f"解析产物根（受控）: {args.parsed_root}")
    for name, fn in (("pg_chunk_revisions", lambda: pg_stats(args.kb)),
                     ("neo4j_chunks", lambda: neo4j_stats(args.kb)),
                     ("milvus", lambda: milvus_stats(args.kb)),
                     ("parsed_documents", lambda: parsed_stats(args.kb, Path(args.parsed_root)))):
        res = fn()
        report["sources"][name] = res
        print(f"  {name}: status={res.get('status')}")

    unverified = [n for n, r in report["sources"].items() if r.get("status") != "OK"]
    report["all_sources_readable"] = not unverified
    report["unverified_sources"] = unverified
    report["c3_ready"] = False
    report["note"] = ("只读统计；任一来源 UNVERIFIED 不得断言 KB 全空，也不得据此启动回填写入。"
                      "c3_ready 恒为 False：本脚本不下达可执行结论。")
    (out_dir / "c3-sources-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print()
    print(f"归档：{out_dir / 'c3-sources-report.json'}")
    print(f"ALL_READABLE: {report['all_sources_readable']} UNVERIFIED: {unverified}")
    return 0 if not unverified else 2


if __name__ == "__main__":
    raise SystemExit(main())
