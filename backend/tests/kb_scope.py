"""烟测脚本共用的 KB 作用域夹具。

M4-R1 契约（docs/ENTERPRISE_BACKEND_API_SPEC.md §3.1）：知识库请求必须显式携带 kb 作用域，
无 default KB 兜底。烟测脚本若不主动带作用域，拿到的是 400 KB_SCOPE_REQUIRED —— 那是契约
要求，不是接口通过，也不能当成被测流程的结论。

口径与 run_perf_probe.py 保持一致：先按调用账号真实可见的 KB 目录发现 active 库并注入；
账号看不到任何 active KB 就打印具名阻塞行并非零退出，绝不降级成"跳过即通过"。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def discover_active_kb(base_url: str, token: str, *, timeout: float = 20.0) -> str:
    """返回调用账号可见的第一个 active kb_id，没有可见库时返回空串。"""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/knowledge-bases",
        method="GET",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ValueError):
        return ""
    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    for item in items or []:
        if isinstance(item, dict) and str(item.get("status")) == "active" and item.get("kb_id"):
            return str(item["kb_id"])
    return ""


def require_active_kb(script: str, base_url: str, token: str, *, timeout: float = 20.0) -> str:
    kb_id = discover_active_kb(base_url, token, timeout=timeout)
    if not kb_id:
        print(
            f"KB_SCOPE_BLOCKED script={script} reason=no_visible_active_kb "
            "hint=run backend/scripts/seed_e2e_local_stack.py"
        )
        raise SystemExit(1)
    print(f"KB_SCOPE_READY script={script} kb_id={kb_id} source=discover")
    return kb_id
