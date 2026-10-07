#!/usr/bin/env python3
"""共享目标 KB 的**受控数据准备**（`SEED-SHARED`）。

数据准备内容（**不自行播种，需单独授权**）：
  ① 确认目标 KB 已在 PG `knowledge_bases` 登记且 status=active；
  ② 播种 Neo4j `(:Chunk)`：kb_id / doc_id / tenant_id / project_id / text / parser_version；
  ③ 写入 Milvus **v2 primary 向量**（deterministic，dim 与生产一致）——
     作为 `backfill_versioned` 的**读源**（§16.1 不变量要求 v2 已持有该 chunk）；
  ④ 播种 PG `chunk_revisions` current 行：revision=1、graph_status=indexed、
     vector_status=pending（待回填收敛）；
  ⑤ 配置**单 KB write scope**：`MILVUS_WRITE_SCOPE_KB_IDS` 仅目标 KB + 核对 tenant/project。

运行模式：
  - 默认（无授权）：**只读预检** + 打印准备清单，exit 3（零写入）。
  - `--authorize SEED-SHARED-AUTHORIZED`：执行数据准备（写入共享环境），
    任一步失败 fail-closed exit 2。

硬禁止（脚本内无入口）：建 v3 集合、回填写入、入队、修改共享配置、push。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

AUTHORIZE_TOKEN = "SEED-SHARED-AUTHORIZED"
DEFAULT_PARSED_ROOT = "E:/projects/GraphInsight/backend/parsed_documents"


def load_env_file(path: Path) -> Dict[str, str]:
    """只读解析 `KEY=VALUE` 行（跳过注释/空行）。"""
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out


def collect_parsed(kb: str, root: Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """只读枚举 parsed_documents/<kb>/<doc>/chunks.jsonl 的有效 chunk（不写不删）。"""
    kb_dir = root / kb
    if not root.exists():
        return [], f"root missing: {root}"
    if not kb_dir.is_dir():
        return [], f"kb dir missing: {kb_dir}"
    out: List[Dict[str, Any]] = []
    for doc_dir in sorted(p for p in kb_dir.iterdir() if p.is_dir()):
        chunks_path = doc_dir / "chunks.jsonl"
        if not chunks_path.is_file():
            continue
        for line in chunks_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                out.append({"doc": doc_dir.name, "valid": False, "error": str(exc)[:120]})
                continue
            if isinstance(obj, dict) and str(obj.get("chunk_id") or "").strip():
                out.append({"doc": doc_dir.name, "chunk_id": str(obj["chunk_id"]),
                            "doc_id": str(obj.get("doc_id") or doc_dir.name),
                            "text": str(obj.get("text") or ""), "valid": True})
            else:
                out.append({"doc": doc_dir.name, "valid": False,
                            "error": "missing chunk_id"})
    return out, None


def main() -> int:
    ap = argparse.ArgumentParser(description="SEED-SHARED 数据准备方案（默认只读）")
    ap.add_argument("--kb", required=True)
    ap.add_argument("--parsed-root", default=DEFAULT_PARSED_ROOT)
    ap.add_argument("--authorize", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks, err = collect_parsed(args.kb, Path(args.parsed_root))
    valid = [c for c in chunks if c.get("valid")]
    invalid = [c for c in chunks if not c.get("valid")]

    plan = {
        "kb_id": args.kb,
        "parsed_root": args.parsed_root,
        "steps": [
            "① 确认目标 KB 已在 PG knowledge_bases 登记且 status=active",
            "② 播种 Neo4j (:Chunk)：kb_id/doc_id/tenant_id/project_id/text/parser_version",
            "③ 写入 Milvus v2 primary 向量（deterministic，dim=4）——作为 backfill 读源",
            "④ 播种 PG chunk_revisions current 行：revision=1、graph_status=indexed、vector_status=pending",
            "⑤ 配置单 KB write scope：MILVUS_WRITE_SCOPE_KB_IDS 仅目标 KB + 核对 tenant/project",
        ],
    }
    record: Dict[str, Any] = {
        "schema": "gate0-seed-shared-plan-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kb_id": args.kb,
        "parsed_root": args.parsed_root,
        "parsed": {
            "doc_count": len({c["doc"] for c in valid}),
            "chunks_total": len(valid),
            "invalid_lines": len(invalid),
        },
        "seed_plan": plan,
        "authorization_ladder": [
            "阶段 1（本阶段，未授权）：仅生成清单 → 申请 SEED-SHARED",
            "阶段 2（SEED-SHARED 授权后）：执行 ②③④⑤",
            "阶段 3（数据准备完成后）：申请 CREATE-V3（建显式 INT64 v3 schema + 向量索引）",
            "阶段 4：申请 CONFIG-SHARED（共享 vector 能力配置）",
            "阶段 5（最后）：申请 BACKFILL-WRITE（单向回填，只补 v3）",
        ],
        "executed": False,
        "result": "PLAN_ONLY",
    }

    out_file = out_dir / "seed-shared-plan.json"
    out_file.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print("SEED-SHARED 数据准备方案（**仅方案，未执行任何写入**）")
    print(f"  目标 KB   : {args.kb}")
    print(f"  解析产物根: {args.parsed_root}")
    print(f"  解析产物  : {record['parsed']['doc_count']} 个文档目录 / "
          f"{record['parsed']['chunks_total']} 条有效 chunk")
    print()
    print("  准备步骤：")
    for line in plan["steps"]:
        print(f"    {line}")
    print()
    print("  授权阶梯：")
    for line in record["authorization_ladder"]:
        print(f"    {line}")
    print()
    print(f"  归档：{out_file}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
