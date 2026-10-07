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


def host_listener(port: int) -> Dict[str, Any]:
    """① 宿主端口监听进程（谁在宿主侧持有该端口）。"""
    ps = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue; "
        f"if (-not $c) {{ Write-Output 'NO_LISTENER' }} else {{ "
        f"foreach ($x in $c) {{ $p = Get-Process -Id $x.OwningProcess -ErrorAction SilentlyContinue; "
        f"Write-Output ($x.LocalAddress + ':' + $x.LocalPort + ' pid=' + $x.OwningProcess + "
        f"' proc=' + $p.ProcessName) }} }}"
    )
    rc, out = run(["powershell", "-NoProfile", "-Command", ps], timeout=90)
    detail = out.strip()[:400]
    return {"ok": bool(detail) and "NO_LISTENER" not in detail, "rc": rc, "detail": detail}


def docker_port_mapping(container: str) -> Dict[str, Any]:
    """② Docker 端口映射（容器端口 → 宿主绑定），只读 inspect。"""
    rc, out = run(["docker", "inspect", container, "--format", "{{json .NetworkSettings.Ports}}"], timeout=60)
    return {"ok": rc == 0, "rc": rc, "detail": out.strip()[:400]}


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
        # ① 宿主端口监听进程 + ② Docker 端口映射（只读）
        svc["host_listener"] = host_listener(host_port)
        svc["docker_port_mapping"] = docker_port_mapping(info["container"])
        # ⑤ 宿主业务客户端（含裸 TCP 对照）
        svc["host_mapped_port"] = host_business(name, info)
        svc["host_tcp"] = host_tcp(host_port)
        # 分层：服务健康 / 业务协议 / 裸 TCP —— 三者必须分开陈述，不得混为一谈
        svc["layers"] = {
            "service_healthy": bool(svc["in_container"]["ok"] or svc["same_network"]["ok"]),
            "business_in_container": svc["in_container"]["ok"],
            "business_same_network": svc["same_network"]["ok"],
            "business_host": svc["host_mapped_port"]["ok"],
            "tcp_host": svc["host_tcp"]["ok"],
        }
        result["services"][name] = svc
        ly = svc["layers"]
        print(f"  {name}: healthy={ly['service_healthy']} "
              f"business(in/same/host)={ly['business_in_container']}/"
              f"{ly['business_same_network']}/{ly['business_host']} "
              f"tcp_host={ly['tcp_host']}")

    # 结论：**分层陈述**（服务健康 / 业务协议 / 裸 TCP），根因未定绝不臆断具体组件
    svcs = result["services"].values()
    all_healthy = all(s["layers"]["service_healthy"] for s in svcs)
    all_host_biz_fail = all(not s["layers"]["business_host"] for s in svcs)
    all_host_tcp_ok = all(s["layers"]["tcp_host"] for s in svcs)
    result["layers_summary"] = {
        "all_services_healthy": all_healthy,
        "all_host_business_fail": all_host_biz_fail,
        "all_host_tcp_ok": all_host_tcp_ok,
    }
    if all_healthy and all_host_biz_fail and all_host_tcp_ok:
        conclusion = (
            "分层观测：① 服务健康（容器内/同网络业务层可达）；② 宿主裸 TCP 可建连；"
            "③ 宿主业务协议握手失败。→ 现象为「宿主访问路径的业务协议层异常」，"
            "根因未定（可能涉及端口转发、代理/防火墙、协议中间件等；"
            "本探测不足以定位到具体组件，不得据此断言某一组件故障）。"
        )
        root_cause = "UNDETERMINED"
    elif all_healthy and not all_host_biz_fail:
        conclusion = "服务健康且部分/全部宿主业务握手成功：宿主访问路径未见异常"
        root_cause = "NONE"
    else:
        conclusion = "服务健康性本身存疑（容器内/同网络亦失败）：需先确认服务状态，再谈宿主路径"
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
