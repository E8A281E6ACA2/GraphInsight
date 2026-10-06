"""门禁子项的**离线服务替身**：在导入 `main:app`、触发 startup 之前安装，保证零真实外连。

背景（2026-10-06 复核）：统一门禁里 DocQA / NL2Cypher 子项导入 app 时会走
`services.neo4j_service._connect()` 真连 Neo4j（`::1:7687` / `127.0.0.1:7687`），拿到鉴权失败——
也就是说"total=22 failed=0"当时并不等于"全程离线"。仅靠"没写连接代码"不是隔离。

用法（必须放在 `from main import app` **之前**）：

    from _offline_services import install_offline_doubles
    install_offline_doubles()
    from main import app

替身策略：
  - Neo4j：`neo4j.GraphDatabase.driver` 换成不建连接的假驱动；`session()` 返回的 session 一旦被
    真正使用（run）就抛 `OfflineServiceUsedError`——**不静默返回空结果**，避免"替身把缺陷变成绿色"。
  - 其余真实服务（PostgreSQL / Milvus）不在导入期连接；若某子项确实触发连接，由
    `tests/netguard/sitecustomize.py` 的网络护栏拦截并让该子项判红（runner 检测 NEUTRALIZED_NETWORK）。
    需要连库的用例应显式使用隔离库（临时 SQLite / 假 client），不得依赖真实服务。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_INSTALLED = False


class OfflineServiceUsedError(RuntimeError):
    """离线替身被真正使用（说明该用例需要显式替身实现，而不是真实服务）。"""


class _OfflineSession:
    def __enter__(self) -> "_OfflineSession":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def run(self, *args: Any, **kwargs: Any) -> Any:
        raise OfflineServiceUsedError(
            "离线替身：本用例试图真正查询 Neo4j —— 请显式注入替身实现或改查隔离数据源"
        )

    def close(self) -> None:
        return None


class OfflineNeo4jDriver:
    """不建立任何连接的 Neo4j 驱动替身。"""

    def session(self, **kwargs: Any) -> _OfflineSession:
        return _OfflineSession()

    def verify_connectivity(self, **kwargs: Any) -> None:
        return None

    def close(self) -> None:
        return None


def install_offline_doubles() -> None:
    """安装离线替身（幂等）。必须在导入 app 之前调用。"""
    global _INSTALLED
    if _INSTALLED:
        return
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    try:
        import neo4j

        neo4j.GraphDatabase.driver = staticmethod(  # type: ignore[assignment]
            lambda *args, **kwargs: OfflineNeo4jDriver()
        )
    except Exception:  # noqa: BLE001 - 缺少 neo4j 依赖时无需替身
        pass
    _INSTALLED = True
