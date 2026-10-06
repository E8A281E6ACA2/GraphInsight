"""门禁子进程的网络护栏（由 run_unified_boundary_guards.py 注入到 PYTHONPATH 自动加载）。

为什么需要：2026-10-06 复核发现统一门禁的 DocQA / NL2Cypher 子项在导入 `main:app` 时**真的尝试
连接 Neo4j**（收到鉴权失败），因此"total=22 failed=0"当时并不能称为"全程离线"。只靠"没写连接代码"
不是隔离，必须让**意外外连本身直接失败**。

契约（三条，逐条可解释）：
  1. **禁止一切非回环连接**（外网 / 局域网）——命中即抛
     `RuntimeError("NEUTRALIZED_NETWORK:<host>:<port>")`，子进程失败、runner 依据该标记判红。
  2. **禁止连接真实服务端口**（回环也算）：默认拒绝 `GUARD_NET_DENY_PORTS`
     （PostgreSQL 5432/5434、Redis 6379、Neo4j 7474/7687、Milvus 9003/19530/19531/29530/29531 等）。
     这正是复核观察到的"DocQA/NL2Cypher 触发真实 Neo4j 鉴权失败"那类连接。
  3. **放行其余回环**：Windows 上 `asyncio` 的 ProactorEventLoop 依赖
     `socket.socketpair()`（内部走 127.0.0.1 临时端口）自建管道；全禁会连测试框架一起打掉。
     因此仅按"端口deny"收紧，而不是无差别封禁回环。

白名单：`GUARD_NET_ALLOW`（逗号分隔 `host` 或 `host:port`）可显式放行，须在评审中说明理由。
DNS（`getaddrinfo`）不拦：只拦真正的连接动作。

**违规留痕（P2 加固）**：拦截时**同步**把违规记录追加到独立文件（`GUARD_NET_VIOLATIONS`），
并在导入期写一条 `guard_loaded` 到 `GUARD_NET_STATUS`。这两件事都不依赖调用方"打印什么"：
若某处把拦截异常静默 try/except 掉，输出里不会出现标记，但**违规文件仍有记录**——runner 校验
该文件与加载状态，不能只靠 stdout 标记（否则"捕获异常且不打印"就能假绿）。

覆盖范围（如实声明）：本护栏只作用于 **Python `socket` 路径**（`socket.socket.connect/connect_ex`、
`socket.create_connection`）。原生扩展（如某些 C 驱动）或子进程自带网络栈不在其覆盖内，不得据此宣称
"所有网络调用都被隔离"。
"""
import json
import os
import socket
import time

_MARKER = "NEUTRALIZED_NETWORK"
_VIOLATION_FILE = (os.environ.get("GUARD_NET_VIOLATIONS") or "").strip()
_STATUS_FILE = (os.environ.get("GUARD_NET_STATUS") or "").strip()


def _append_record(path: str, record: dict) -> bool:
    """追加一条 JSON 行；返回是否成功。留痕失败不得影响拦截动作本身（拦截优先）。"""
    if not path:
        return False
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False



DEFAULT_DENY_PORTS = {
    "5432", "5433", "5434",  # PostgreSQL（含项目 compose 栈 5434）
    "6379",                  # Redis
    "7474", "7687",          # Neo4j HTTP / Bolt
    "9003", "19530", "19531", "29530", "29531",  # Milvus（含项目 19531 与一次性栈）
    "9200", "11434",         # Elasticsearch / 本地模型服务（防误连）
}
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def _parse_csv(raw: str) -> set:
    return {item.strip().lower() for item in (raw or "").split(",") if item.strip()}


_ALLOW = _parse_csv(os.environ.get("GUARD_NET_ALLOW", ""))
_deny_env = os.environ.get("GUARD_NET_DENY_PORTS")
_DENY_PORTS = _parse_csv(_deny_env) if _deny_env is not None else set(DEFAULT_DENY_PORTS)


def _host_port(address) -> tuple:
    if isinstance(address, tuple) and len(address) >= 2:
        return str(address[0]), int(address[1]) if str(address[1]).isdigit() else address[1]
    return str(address), None


def _is_allowed(host: str, port) -> bool:
    """放行条件：显式白名单，或（回环地址且端口不在真实服务端口 deny 列表）。"""
    host = (host or "").lower()
    if f"{host}:{port}" in _ALLOW or host in _ALLOW:
        return True
    if host in _LOOPBACK and str(port) not in _DENY_PORTS:
        return True
    return False


class NeutralizedNetworkError(RuntimeError):
    """门禁子进程内的意外外连（护栏拦截）。"""


def _blocked(address) -> "NeutralizedNetworkError":
    host, port = _host_port(address)
    # **拦截即留痕**（独立文件，先写再抛）：即使调用方把异常 try/except 掉且不打印，
    # 违规依然可被 runner 检出——"捕获异常且不打印"不能构成假绿。
    written = _append_record(_VIOLATION_FILE, {
        "event": "blocked_connect",
        "host": host,
        "port": port,
        "pid": os.getpid(),
        "time": time.time(),
    })
    if not written:
        # 留痕写不进去（路径不可写等）：回退到 stderr 标记。runner 会把
        # "出标记但无留痕"判红——留痕写入失败不得构成通过。
        try:
            import sys as _sys

            _sys.stderr.write(f"{_MARKER}:{host}:{port} (violation_record_write_failed)\n")
        except Exception:  # noqa: BLE001
            pass
    return NeutralizedNetworkError(f"{_MARKER}:{host}:{port}")


_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_create_connection = socket.create_connection


def _guarded_connect(self, address):  # noqa: ANN001 - 与标准库同签名
    host, port = _host_port(address)
    if not _is_allowed(host, port):
        raise _blocked(address)
    return _orig_connect(self, address)


def _guarded_connect_ex(self, address):  # noqa: ANN001
    host, port = _host_port(address)
    if not _is_allowed(host, port):
        raise _blocked(address)
    return _orig_connect_ex(self, address)


def _guarded_create_connection(address, *args, **kwargs):  # noqa: ANN001
    host, port = _host_port(address)
    if not _is_allowed(host, port):
        raise _blocked(address)
    return _orig_create_connection(address, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.create_connection = _guarded_create_connection

# 导入即留痕（放在补丁之后：此时常量与策略已就绪）——runner 据此确认子进程**确实加载了护栏**，
# 没加载 = 隔离不成立 → 判红（不能因为"没打印标记"就当作通过）。
_append_record(_STATUS_FILE, {
    "event": "guard_loaded",
    "pid": os.getpid(),
    "deny_ports": sorted(str(port) for port in _DENY_PORTS),
    "allow": sorted(_ALLOW),
    "time": time.time(),
})
