#!/usr/bin/env python3
"""共享栈**逐容器重启 + 复验**（最小影响，受控执行）。

默认 `--dry-run`（只打印计划，exit 3，不碰任何容器）；实际执行需显式
`--authorize RESTART-SHARED-AUTHORIZED`。

按 `postgres → neo4j → milvus` 顺序**逐个**重启，每重启一个：
  1. `docker compose -f <file> restart <service>`（**不 down/up**，不删卷）；
  2. 等待该容器 `healthy`（轮询超时即 fail-closed 停止）；
  3. 重跑四类探测：容器内业务 / 同网络业务 / 宿主业务协议 / 宿主裸 TCP；
  4. 记录重启前后结果；
  5. **宿主协议恢复 → 立即停止后续重启**；
  6. 仍失败 → 才重启下一个。

硬禁止（脚本内不提供任何入口）：`compose down/up`、删除卷、重启 Docker Desktop、
C3 写入、canary 授权。

结论口径：只记录观测；根因未定时写 `root_cause=UNDETERMINED`，不臆断具体组件。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.dev.yml"
AUTHORIZE_TOKEN = "RESTART-SHARED-AUTHORIZED"
ORDER = ["postgres", "neo4j", "milvus"]  # 逐个重启顺序

try:
    import probe_shared_connectivity as probe
except Exception:  # noqa: BLE001
    probe = None


def say(line: str = "") -> None:
    print(line, flush=True)


def run(cmd, timeout: int = 120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def health_status(container: str) -> str:
    rc, out = run(["docker", "inspect", container, "--format", "{{.State.Health.Status}}"], timeout=60)
    if rc == 0:
        return (out or "").strip()
    return ""


def wait_healthy(container: str, timeout: int = 300) -> bool:
    """等待 healthy；容器无 healthcheck 时退化为等 running。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = health_status(container)
        if st == "healthy":
            return True
        if not st:
            rc, out = run(["docker", "inspect", container, "--format", "{{.State.Status}}"], timeout=60)
            if rc == 0 and (out or "").strip() == "running":
                return True
        time.sleep(5)
    return False


def probe_all() -> Dict[str, Any]:
    """四类探测：容器内业务 / 同网络业务 / 宿主业务 / 宿主裸 TCP。"""
    if probe is None:
        return {"error": "probe_shared_connectivity 不可用"}
    result: Dict[str, Any] = {}
    for name, info in probe.SERVICES.items():
        svc: Dict[str, Any] = {}
        if name == "postgres":
            svc["in_container"] = probe.docker_exec(info["container"],
                                                    probe._pg_psql("127.0.0.1", info["inner_port"], info))
            svc["same_network"] = probe.same_network(
                probe._pg_psql(info["container"], info["inner_port"], info))
        else:
            path = "/healthz" if name == "milvus" else "/"
            svc["in_container"] = probe.docker_exec(info["container"],
                                                    probe._http_probe("127.0.0.1", info["http_port"], path))
            svc["same_network"] = probe.same_network(
                probe._http_probe(info["container"], info["http_port"], path))
        svc["host_business"] = probe.host_business(name, info)
        svc["host_tcp"] = probe.host_tcp(info["host_port"])
        result[name] = svc
        ly = (bool(svc["in_container"]["ok"] or svc["same_network"]["ok"]),
              svc["host_business"]["ok"], svc["host_tcp"]["ok"])
        say(f"    {name}: healthy={ly[0]} host_business={ly[1]} host_tcp={ly[2]}")
    return result


def host_business_all_ok(snapshot: Dict[str, Any]) -> bool:
    return all(s.get("host_business", {}).get("ok") for s in snapshot.values())


def main() -> int:
    ap = argparse.ArgumentParser(description="共享栈逐容器重启 + 复验（受控）")
    ap.add_argument("--authorize", default="", help=f"需显式传 {AUTHORIZE_TOKEN}")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划（默认）")
    ap.add_argument("--out", required=True, help="归档目录")
    ap.add_argument("--compose-file", default=str(COMPOSE_FILE))
    ap.add_argument("--services", default=",".join(ORDER), help="重启顺序（逗号分隔）")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    services = [s.strip() for s in args.services.split(",") if s.strip()]
    authorized = args.authorize == AUTHORIZE_TOKEN

    record: Dict[str, Any] = {
        "schema": "gate0-shared-restart-probe-v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "compose_file": str(args.compose_file),
        "order": services,
        "authorized": authorized,
        "forbidden": ["compose down/up", "volume rm", "Docker Desktop restart", "C3 write", "canary"],
        "steps": [],
    }

    if not authorized or args.dry_run:
        say("=" * 68)
        say("计划（dry-run）：未取得授权，不重启任何容器")
        say("=" * 68)
        say(f"  compose   : {args.compose_file}")
        say(f"  重启顺序  : {' → '.join(services)}（逐个，非同时）")
        say("  每步      : restart → 等 healthy → 四类探测 → 记录 → 恢复即停")
        say("  硬禁止    : compose down/up、删卷、重启 Docker Desktop、C3 写入、canary")
        say(f"  执行需    : --authorize {AUTHORIZE_TOKEN}")
        record["result"] = "DRY_RUN"
        (out_dir / "restart-probe.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return 3

    # ---- 已授权：逐个重启 ----
    say("重启前基线探测：")
    before = probe_all()
    record["before"] = before

    recovered = False
    for svc in services:
        container = f"graphinsight-{svc}"
        step: Dict[str, Any] = {"service": svc, "container": container}
        say(f"\n[restart] {svc}（{container}）")
        rc, out = run(["docker", "compose", "-f", str(args.compose_file), "restart", svc], timeout=300)
        step["restart_rc"] = rc
        step["restart_detail"] = (out or "").strip()[:300]
        if rc != 0:
            step["result"] = "RESTART_FAILED"
            record["steps"].append(step)
            say(f"  restart 失败 rc={rc} {step['restart_detail'][:150]}")
            break
        ok_health = wait_healthy(container)
        step["healthy"] = ok_health
        if not ok_health:
            step["result"] = "NOT_HEALTHY"
            record["steps"].append(step)
            say("  等待 healthy 超时，停止后续重启（fail-closed）")
            break
        say("  探测：")
        after = probe_all()
        step["after"] = after
        step["host_recovered"] = host_business_all_ok(after)
        step["result"] = "RECOVERED" if step["host_recovered"] else "STILL_FAILING"
        record["steps"].append(step)
        if step["host_recovered"]:
            say(f"  ✔ 宿主业务协议已恢复 → 停止后续重启")
            recovered = True
            break
        say("  宿主业务协议仍失败 → 继续下一个")

    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    record["recovered"] = recovered
    if recovered:
        record["conclusion"] = "逐容器重启后宿主业务协议恢复；可重跑只读 C3 预检"
        record["root_cause"] = "RESOLVED_BY_RESTART"
    else:
        record["conclusion"] = ("逐个重启后宿主业务协议仍失败（服务健康、宿主裸 TCP 通）→ "
                                "仍需进一步定位；按工单应**单独申请** Docker Desktop 重启授权")
        record["root_cause"] = "UNDETERMINED"
    (out_dir / "restart-probe.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    say()
    say(f"归档：{out_dir / 'restart-probe.json'}")
    say(f"RECOVERED: {recovered}")
    say(f"ROOT_CAUSE: {record['root_cause']}")
    return 0 if recovered else 2


if __name__ == "__main__":
    raise SystemExit(main())
