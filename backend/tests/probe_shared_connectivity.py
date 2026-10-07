#!/usr/bin/env python3
"""共享环境连通性**只读**定位：对比三条访问路径的业务握手。

路径：
  A **容器内**（in-container）：`docker exec <container>` 在容器内连自身服务端口；
  B **同 Docker 网络**（same-network）：临时容器 `--rm --network <net>` 连 `<容器名>:<内部端口>`；
  C **宿主映射端口**（host-mapped）：宿主机连 `127.0.0.1:<映射端口>`。

三条路径对比可区分"服务本身故障"与"宿主访问路径故障"。**本脚本只做只读握手**
（`SELECT 1` / TCP `nc -z` / driver 只读调用），不写任何业务数据、不重启任何容器。

结论口径：只记录观测到的现象；**根因未定**时明确写 `root_cause=UNDETERMINED`，
不臆断为某个具体组件（如 Docker Desktop 转发）故障。
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NET = "graphinsight_default"
PROBE_IMAGE = "postgres:16-alpine"  # 本地已有，含 psql/nc/wget

PG_PASSWORD = "graphinsight-dev-password"
SERVICES = {
    "postgres": {"container": "graphinsight-postgres", "inner_port": 5432, "host_port": 5434,
                 "db": "graphinsight_admin", "user": "graphinsight", "http_port": None},
    "neo4j": {"container": "graphinsight-neo4j", "inner_port": 7687, "host_port": 7687,
              "http_port": 7474},
    "milvus": {"container": "graphinsight-milvus", "inner_port": 19530, "host_port": 19531,
               "http_port": 9091},
}


def _pg_psql(host: str, port: int, info: Dict[str, Any]) -> str:
    """psql 只读探测（**必须带密码**，否则 fe_sendauth: no password 会被误判为连通性问题）。"""
    return (f"PGPASSWORD={PG_PASSWORD} psql -h {host} -p {port} -U {info['user']} "
            f"-d {info['db']} -c 'SELECT 1' -t -A")


def _http_probe(host: str, port: int, path: str = "/") -> str:
    """HTTP 业务层探测（容器内/同网络两路径通用；wget 在 alpine/neo4j/milvus 镜像里常见）。"""
    return f"wget -q -T 5 -O - http://{host}:{port}{path} 2>/dev/null | head -c 120"


def run(cmd, timeout: int = 60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def docker_exec(container: str, shell_cmd: str) -> Dict[str, Any]:
    rc, out = run(["docker", "exec", container, "sh", "-c", shell_cmd])
    return {"ok": rc == 0, "rc": rc, "detail": out.strip()[:400]}


def same_network(shell_cmd: str) -> Dict[str, Any]:
    """临时容器（--rm，自动清理）在同网络里执行探测。"""
    rc, out = run(["docker", "run", "--rm", "--network", NET, "--entrypoint", "sh",
                   PROBE_IMAGE, "-c", shell_cmd], timeout=180)
    return {"ok": rc == 0, "rc": rc, "detail": out.strip()[:400]}


def host_tcp(port: int) -> Dict[str, Any]:
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect(("127.0.0.1", port))
        return {"ok": True, "rc": 0, "detail": f"TCP 127.0.0.1:{port} connect OK"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rc": -1, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        s.close()


def host_business(service: str, info: Dict[str, Any]) -> Dict[str, Any]:
    """宿主业务握手（只读）。"""
    try:
        if service == "postgres":
            import psycopg

            conn = psycopg.connect(
                f"postgresql://{info['user']}:graphinsight-dev-password@127.0.0.1:{info['host_port']}/{info['db']}",
                sslmode="disable", connect_timeout=5)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            conn.close()
            return {"ok": True, "rc": 0, "detail": "SELECT 1 OK"}
        if service == "neo4j":
            from neo4j import GraphDatabase

            d = GraphDatabase.driver(f"bolt://127.0.0.1:{info['host_port']}",
                                     auth=("neo4j", "change-this-password"))
            d.verify_connectivity()
            d.close()
            return {"ok": True, "rc": 0, "detail": "bolt verify_connectivity OK"}
        if service == "milvus":
            from pymilvus import MilvusClient

            c = MilvusClient(uri=f"http://127.0.0.1:{info['host_port']}", timeout=5)
            c.list_collections()
            return {"ok": True, "rc": 0, "detail": "list_collections OK"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rc": -1, "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return {"ok": False, "rc": -1, "detail": "unknown service"}


def main() -> int:
    ap = argparse.ArgumentParser(description="共享环境连通性只读定位（三路径对比）")
    ap.add_argument("--out", required=True, help="归档目录（写入 connectivity-probe.json）")
    ap.add_argument("--skip-same-network", action="store_true",
                    help="跳过同网络路径（不创建临时容器）")
    args = ap.parse_args()

    result: Dict[str, Any] = {
        "schema": "gate0-shared-connectivity-probe-v1",
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "network": NET,
        "read_only": True,
        "services": {},
    }

    for name, info in SERVICES.items():
        svc: Dict[str, Any] = {}
        inner, host_port = info["inner_port"], info["host_port"]
        http_port = info.get("http_port")
        # A 容器内（业务层：PG 用 psql 只读查询，其它用 HTTP health）
        if name == "postgres":
            svc["in_container"] = docker_exec(info["container"], _pg_psql("127.0.0.1", inner, info))
            svc["in_container_probe"] = f"psql 127.0.0.1:{inner} SELECT 1"
        else:
            path = "/healthz" if name == "milvus" else "/"
            svc["in_container"] = docker_exec(info["container"], _http_probe("127.0.0.1", http_port, path))
            svc["in_container_probe"] = f"http 127.0.0.1:{http_port}{path}"
        # B 同网络（容器名:内部端口）
        if args.skip_same_network:
            svc["same_network"] = {"ok": None, "rc": None, "detail": "skipped"}
            svc["same_network_probe"] = "skipped"
        else:
            if name == "postgres":
                svc["same_network"] = same_network(_pg_psql(info["container"], inner, info))
                svc["same_network_probe"] = f"psql {info['container']}:{inner} SELECT 1"
            else:
                path = "/healthz" if name == "milvus" else "/"
                svc["same_network"] = same_network(_http_probe(info["container"], http_port, path))
                svc["same_network_probe"] = f"http {info['container']}:{http_port}{path}"
        # C 宿主
        svc["host_mapped_port"] = host_business(name, info)
        svc["host_tcp"] = host_tcp(host_port)
        result["services"][name] = svc
        a = svc["in_container"]["ok"]
        b = svc["same_network"]["ok"]
        c = svc["host_mapped_port"]["ok"]
        print(f"  {name}: in_container={a} same_network={b} host_business={c} "
              f"host_tcp={svc['host_tcp']['ok']}")

    # 结论：只描述现象，根因未定不臆断
    any_host_fail = any(not s["host_mapped_port"]["ok"] for s in result["services"].values())
    all_inner_ok = all(s["in_container"]["ok"] for s in result["services"].values())
    if any_host_fail and all_inner_ok:
        conclusion = ("容器内可访问、宿主映射端口业务握手失败 → 观测为「宿主访问路径异常」，"
                      "根因未定（可能涉及端口转发/代理/防火墙等，需进一步定位）")
        root_cause = "UNDETERMINED"
    elif not any_host_fail:
        conclusion = "三条路径业务握手均成功"
        root_cause = "NONE"
    else:
        conclusion = "容器内亦失败：可能是服务本身异常，需先看服务状态"
        root_cause = "UNDETERMINED"
    result["conclusion"] = conclusion
    result["root_cause"] = root_cause

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "connectivity-probe.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print()
    print(f"归档：{out_dir / 'connectivity-probe.json'}")
    print(f"CONCLUSION: {conclusion}")
    print(f"ROOT_CAUSE: {root_cause}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
