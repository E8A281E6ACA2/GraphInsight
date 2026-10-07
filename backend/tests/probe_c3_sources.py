#!/usr/bin/env python3
"""共享目标 KB 的**四来源只读统计**（backfill_versioned 前置报告）。

四来源：
  1. PG `chunk_revisions`（current 行，按 kb）；
  2. Neo4j `(:Chunk)` 节点（按 kb_id）；
  3. Milvus collections / row count / 按 kb 的 chunk；
  4. 解析产物 `parsed_documents/<kb>/<doc>/chunks.jsonl`。

访问方式（**只读**，规避宿主业务协议异常）：
  - PG / Neo4j：`docker exec` 在容器内用 psql / cypher-shell 只读查询；
  - Milvus：同 Docker 网络临时容器（`--rm`，stdin 传脚本）用 urllib 调 RESTful 只读接口；
  - 解析产物：宿主侧只读遍历目录（不写不删）。

禁止：任何写入、建集合、播种、入队、修改配置。任一步失败即在该来源标注
`status=UNVERIFIED`（不得折成 0，也不得据此断言"KB 全空"）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

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


def run(cmd, stdin: str = None, timeout: int = 120):
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def pg_stats(kb: str) -> Dict[str, Any]:
    """PG：chunk_revisions 总数 + 目标 KB 的 current 行数（只读）。"""
    total = run(["docker", "exec", PG_CONTAINER, "psql", "-U", "graphinsight", "-d",
                 "graphinsight_admin", "-t", "-A", "-c",
                 "SELECT count(*) FROM chunk_revisions;"])
    per_kb = run(["docker", "exec", PG_CONTAINER, "psql", "-U", "graphinsight", "-d",
                  "graphinsight_admin", "-t", "-A", "-c",
                  "SELECT kb_id, count(*) FROM chunk_revisions GROUP BY kb_id ORDER BY 2 DESC;"])
    cur = run(["docker", "exec", PG_CONTAINER, "psql", "-U", "graphinsight", "-d",
               "graphinsight_admin", "-t", "-A", "-c",
               f"SELECT count(*) FROM chunk_revisions WHERE kb_id='{kb}' "
               f"AND revision_status='current';"])
    if total[0] != 0 or cur[0] != 0:
        return {"status": "UNVERIFIED", "detail": (total[1] or cur[1])[:300]}
    return {"status": "OK", "total": (total[1] or "").strip(),
            "per_kb": [l for l in (per_kb[1] or "").splitlines() if l.strip()],
            "target_kb_current": (cur[1] or "").strip()}


def neo4j_stats(kb: str) -> Dict[str, Any]:
    """Neo4j：Chunk 总数 + 目标 KB 的 Chunk 数（只读 cypher）。"""
    total = run(["docker", "exec", NEO4J_CONTAINER, "cypher-shell", "-u", "neo4j", "-p",
                 "change-this-password", "--non-interactive", "--format", "plain",
                 "MATCH (c:Chunk) RETURN count(c) AS n;"])
    per_kb = run(["docker", "exec", NEO4J_CONTAINER, "cypher-shell", "-u", "neo4j", "-p",
                  "change-this-password", "--non-interactive", "--format", "plain",
                  "MATCH (c:Chunk) RETURN c.kb_id AS kb, count(c) AS n ORDER BY n DESC;"])
    detail = run(["docker", "exec", NEO4J_CONTAINER, "cypher-shell", "-u", "neo4j", "-p",
                  "change-this-password", "--non-interactive", "--format", "plain",
                  f"MATCH (c:Chunk {{kb_id:'{kb}'}}) RETURN c.chunk_id AS chunk_id, "
                  f"c.doc_id AS doc_id, c.tenant_id AS tenant, c.project_id AS project LIMIT 20;"])
    if total[0] != 0:
        return {"status": "UNVERIFIED", "detail": (total[1] or "")[:300]}
    return {"status": "OK", "total": (total[1] or "").strip().splitlines()[-1:] or ["0"],
            "per_kb": [l for l in (per_kb[1] or "").splitlines() if l.strip()],
            "target_kb_sample": [l for l in (detail[1] or "").splitlines() if l.strip()]}


def milvus_stats(kb: str) -> Dict[str, Any]:
    """Milvus：同网络临时容器（--rm，stdin 传脚本）调 RESTful 只读接口。"""
    script = MILVUS_SCRIPT.format(milvus=MILVUS_HOST, kb=kb)
    rc, out = run(["docker", "run", "-i", "--rm", "--network", NET, PY_IMAGE, "python", "-"],
                  stdin=script, timeout=300)
    if rc != 0:
        return {"status": "UNVERIFIED", "detail": (out or "")[:300]}
    # 只取最后一行 JSON（镜像拉取等噪音在前）
    line = ""
    for l in (out or "").splitlines():
        if l.strip().startswith("{"):
            line = l.strip()
    if not line:
        return {"status": "UNVERIFIED", "detail": (out or "")[:300]}
    try:
        data = json.loads(line)
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNVERIFIED", "detail": f"{type(exc).__name__}: {exc}"}
    return {"status": "OK", **{k: v for k, v in data.items() if k != "collections"},
            "collections": (data.get("collections") or {}).get("data")}


def parsed_stats(kb: str, root: Path) -> Dict[str, Any]:
    """解析产物：parsed_documents/<kb>/<doc>/chunks.jsonl 只读统计。"""
    kb_dir = root / kb
    if not root.exists():
        return {"status": "UNVERIFIED", "detail": f"root missing: {root}"}
    entries: List[Dict[str, Any]] = []
    if kb_dir.is_dir():
        for doc_dir in sorted(kb_dir.iterdir()):
            if not doc_dir.is_dir():
                continue
            chunks = doc_dir / "chunks.jsonl"
            n = 0
            if chunks.is_file():
                n = sum(1 for l in chunks.read_text(encoding="utf-8", errors="replace").splitlines()
                        if l.strip())
            entries.append({"doc": doc_dir.name, "chunks": n,
                            "manifest": (doc_dir / "manifest.json").is_file()})
    all_kbs = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
    return {"status": "OK", "root": str(root), "target_kb_dir_exists": kb_dir.is_dir(),
            "target_kb_docs": entries,
            "target_kb_chunks_total": sum(e["chunks"] for e in entries),
            "all_kb_dirs": all_kbs[:20]}


def main() -> int:
    ap = argparse.ArgumentParser(description="共享目标 KB 四来源只读统计")
    ap.add_argument("--kb", required=True, help="目标 KB id")
    ap.add_argument("--parsed-root", required=True, help="解析产物根目录")
    ap.add_argument("--out", required=True, help="归档目录")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {
        "schema": "gate0-c3-sources-probe-v1",
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "kb_id": args.kb,
        "read_only": True,
        "sources": {},
    }
    print(f"目标 KB: {args.kb}")
    for name, fn in (("pg_chunk_revisions", lambda: pg_stats(args.kb)),
                     ("neo4j_chunks", lambda: neo4j_stats(args.kb)),
                     ("milvus", lambda: milvus_stats(args.kb)),
                     ("parsed_documents", lambda: parsed_stats(args.kb, Path(args.parsed_root)))):
        res = fn()
        report["sources"][name] = res
        print(f"  {name}: status={res.get('status')}")

    # 四来源可读性 + 非空判定（不得把 UNVERIFIED 当 0）
    unverified = [n for n, r in report["sources"].items() if r.get("status") != "OK"]
    report["all_sources_readable"] = not unverified
    report["unverified_sources"] = unverified
    report["c3_ready"] = False  # 由人工结合数据准备方案判定，不在本脚本内下结论
    report["note"] = ("本脚本只做只读统计；任一来源 UNVERIFIED 时不得断言 KB 全空，"
                      "也不得据此启动回填写入。")
    (out_dir / "c3-sources-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print()
    print(f"归档：{out_dir / 'c3-sources-report.json'}")
    print(f"ALL_READABLE: {report['all_sources_readable']} UNVERIFIED: {unverified}")
    return 0 if not unverified else 2


if __name__ == "__main__":
    raise SystemExit(main())
