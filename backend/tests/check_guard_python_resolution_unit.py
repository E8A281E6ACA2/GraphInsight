#!/usr/bin/env python3
"""统一门禁解释器探测（_resolve_python）离线单元契约。

背景（2026-10-24）：`run_unified_boundary_guards.py` 原先只探测 POSIX 路径
`backend/.venv/bin/python`。Windows 的 venv 布局是 `backend/.venv/Scripts/python.exe`，
于是 Windows 上会静默回退到宿主 Python（未装 backend/requirements.txt），把"环境未就绪"
误报成大量依赖缺失 / 版本兼容性失败（实测 21 项伪失败）。

本检查只覆盖解释器**解析逻辑**，纯 stdlib、零网络、零子进程、不触碰真实
`backend/.venv`：所有候选路径都在临时目录里构造。

覆盖矩阵：
1. `GUARD_PYTHON` 指向存在的解释器 → 直接采用（优先于任何 venv 候选）；
2. `GUARD_PYTHON` 指向不存在的路径 → RuntimeError（显式指定不静默降级）；
3. `GUARD_PYTHON` 为空 / 纯空白 → 等价于未设置，继续探测候选；
4. 仅 POSIX `bin/python` 存在 → 采用（Linux/POSIX 既有行为不变）；
5. POSIX 与 Windows `Scripts/python.exe` 同时存在 → POSIX 优先；
6. 仅 Windows `Scripts/python.exe` 存在 → 可被识别（本次修复目标）；
7. 两种 venv 路径都不存在 → 回退当前解释器，并向 stderr 打出含全部探测路径的警告；
8. 默认候选清单的内容与顺序（`bin/python` → `Scripts/python.exe`，均位于 `backend/.venv`）。

运行：`python -u backend/tests/check_guard_python_resolution_unit.py`
退出码：0 = 全部通过；1 = 存在失败。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_unified_boundary_guards as g  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}" + (f"（{detail}）" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"（{detail}）" if detail else ""))


@contextlib.contextmanager
def guard_python(value: str | None):
    """临时设置 / 清除 GUARD_PYTHON。"""
    saved = os.environ.get("GUARD_PYTHON")
    try:
        if value is None:
            os.environ.pop("GUARD_PYTHON", None)
        else:
            os.environ["GUARD_PYTHON"] = value
        yield
    finally:
        if saved is None:
            os.environ.pop("GUARD_PYTHON", None)
        else:
            os.environ["GUARD_PYTHON"] = saved


def _make_venv(root: Path, *, posix: bool, windows: bool) -> tuple[Path, Path]:
    """在临时 root 下按需构造 venv 布局，返回 (posix 候选, windows 候选)。"""
    posix_python = root / ".venv" / "bin" / "python"
    windows_python = root / ".venv" / "Scripts" / "python.exe"
    if posix:
        posix_python.parent.mkdir(parents=True, exist_ok=True)
        posix_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if windows:
        windows_python.parent.mkdir(parents=True, exist_ok=True)
        windows_python.write_text("@echo off\r\n", encoding="utf-8")
    return posix_python, windows_python


def test_default_candidates() -> None:
    """默认候选清单：POSIX 优先、Windows 其次，均位于 backend/.venv。"""
    expected = (
        g.ROOT / ".venv" / "bin" / "python",
        g.ROOT / ".venv" / "Scripts" / "python.exe",
    )
    check("默认候选顺序为 (bin/python, Scripts/python.exe)",
          g.VENV_PYTHON_CANDIDATES == expected,
          str(g.VENV_PYTHON_CANDIDATES))
    check("默认候选覆盖 Windows Scripts/python.exe",
          g.PYTHON_EXE_WINDOWS.name == "python.exe"
          and g.PYTHON_EXE_WINDOWS.parent.name == "Scripts",
          str(g.PYTHON_EXE_WINDOWS))
    check("POSIX 常量未被改变",
          g.PYTHON_EXE == g.ROOT / ".venv" / "bin" / "python", str(g.PYTHON_EXE))


def test_guard_python_override() -> None:
    """GUARD_PYTHON 显式覆盖：存在即用，且优先于存在的 venv 候选。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        posix_python, windows_python = _make_venv(root, posix=True, windows=True)
        candidates = (posix_python, windows_python)
        external = root / "external" / "python.exe"
        external.parent.mkdir(parents=True, exist_ok=True)
        external.write_text("@echo off\r\n", encoding="utf-8")

        with guard_python(str(external)):
            resolved = g._resolve_python(candidates, current=Path(sys.executable))
        check("GUARD_PYTHON 指向存在的解释器 → 直接采用",
              resolved == external, str(resolved))
        check("GUARD_PYTHON 优先于已存在的 venv 候选",
              resolved != posix_python and resolved != windows_python, str(resolved))


def test_guard_python_missing() -> None:
    """GUARD_PYTHON 指向不存在路径：必须报错，不得静默降级到候选或当前解释器。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        posix_python, windows_python = _make_venv(root, posix=True, windows=True)
        candidates = (posix_python, windows_python)
        missing = root / "nope" / "python.exe"
        raised: Exception | None = None
        with guard_python(str(missing)):
            try:
                g._resolve_python(candidates, current=Path(sys.executable))
            except Exception as exc:  # noqa: BLE001
                raised = exc
        check("GUARD_PYTHON 指向不存在的路径 → RuntimeError",
              isinstance(raised, RuntimeError), type(raised).__name__)
        check("错误消息包含缺失路径",
              raised is not None and str(missing) in str(raised), str(raised)[:120])


def test_guard_python_blank_means_unset() -> None:
    """空 / 纯空白的 GUARD_PYTHON 视为未设置，继续探测候选。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        posix_python, windows_python = _make_venv(root, posix=True, windows=True)
        candidates = (posix_python, windows_python)
        for blank in ("", "   "):
            with guard_python(blank):
                resolved = g._resolve_python(candidates, current=Path(sys.executable))
            check(f"GUARD_PYTHON={blank!r} 视为未设置 → 走候选探测",
                  resolved == posix_python, str(resolved))


def test_posix_candidate_preferred() -> None:
    """仅 POSIX bin/python 存在 / 两者都存在：POSIX 优先（Linux 行为不变）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        posix_python, windows_python = _make_venv(root, posix=True, windows=True)
        with guard_python(None):
            both = g._resolve_python((posix_python, windows_python),
                                     current=Path(sys.executable))
        check("两者都存在 → POSIX bin/python 优先", both == posix_python, str(both))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        posix_python, windows_python = _make_venv(root, posix=True, windows=False)
        with guard_python(None):
            only_posix = g._resolve_python((posix_python, windows_python),
                                           current=Path(sys.executable))
        check("仅 POSIX bin/python 存在 → 采用（既有行为不变）",
              only_posix == posix_python, str(only_posix))
        check("仅 POSIX 场景不回退当前解释器",
              only_posix != Path(sys.executable), str(only_posix))


def test_windows_candidate_detected() -> None:
    """仅 Windows Scripts/python.exe 存在：必须被识别（本次修复目标）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        posix_python, windows_python = _make_venv(root, posix=False, windows=True)
        with guard_python(None):
            resolved = g._resolve_python((posix_python, windows_python),
                                         current=Path(sys.executable))
        check("仅 Windows Scripts/python.exe 存在 → 被识别",
              resolved == windows_python, str(resolved))
        check("Windows 场景不回退当前解释器",
              resolved != Path(sys.executable), str(resolved))
        check("Windows 场景选中路径确实是 Scripts/python.exe",
              resolved.parent.name == "Scripts" and resolved.name == "python.exe",
              str(resolved))


def test_fallback_only_when_no_candidate() -> None:
    """两种 venv 路径都不存在：回退当前解释器并打警告。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        posix_python, windows_python = _make_venv(root, posix=False, windows=False)
        sentinel = root / "host" / "python.exe"
        stderr = io.StringIO()
        with guard_python(None), contextlib.redirect_stderr(stderr):
            resolved = g._resolve_python((posix_python, windows_python), current=sentinel)
        err = stderr.getvalue()
        check("两者都不存在 → 回退当前解释器", resolved == sentinel, str(resolved))
        check("回退时打出 [warn] 警告", "[warn]" in err, err.strip()[:120])
        check("警告列出全部探测路径",
              str(posix_python) in err and str(windows_python) in err, err.strip()[:160])
        check("回退时提示需安装 backend/requirements.txt",
              "requirements.txt" in err, err.strip()[:120])


def test_main_uses_resolver() -> None:
    """main() 必须经由 _resolve_python 取解释器（防回退成硬编码 POSIX 路径）。"""
    source = Path(g.__file__).read_text(encoding="utf-8")
    check("main() 调用 _resolve_python()",
          "python_bin = _resolve_python()" in source)
    check("源码不再硬编码单一 POSIX 探测",
          "if PYTHON_EXE.exists():\n        return PYTHON_EXE" not in source)


def test_migration_guard_rule_scope() -> None:
    """静态守卫规则的作用域（2026-10-24 授权修订后的正 / 负向对照）。

    `check_migration_cleanup_guards.py` 必须仍禁止"跑错环境"（其它 venv、系统解释器、
    硬编码 Windows 路径），同时允许 `backend/.venv` 下的平台原生布局。
    """
    import check_migration_cleanup_guards as m  # noqa: PLC0415

    posix = 'ROOT / ".venv" / "bin" / "python"'
    windows = 'ROOT / ".venv" / "Scripts" / "python.exe"'
    real_source = m._source
    cases = (
        ("允许：POSIX + Windows 候选都在 .venv 下",
         f'PYTHON_EXE = {posix}\nPYTHON_EXE_WINDOWS = {windows}\n', False),
        ("允许：仅 POSIX 候选（其它 entrypoint 现状）", f'PYTHON_EXE = {posix}\n', False),
        ("拒绝：Scripts 脱离 .venv",
         f'PYTHON_EXE = {posix}\nWIN = ROOT / "venv" / "Scripts" / "python.exe"\n', True),
        ("拒绝：硬编码系统 python.exe",
         f'PYTHON_EXE = {posix}\nPY = "C:/Python313/python.exe"\n', True),
        ("拒绝：整段 .venv/Scripts 字面量",
         f'PYTHON_EXE = {posix}\nPY = ".venv/Scripts/python.exe"\n', True),
        ("拒绝：丢失 POSIX 主候选", f'PYTHON_EXE_WINDOWS = {windows}\n', True),
        ("拒绝：回退到 backend/venv",
         f'PYTHON_EXE = {posix}\nALT = "backend/venv/bin/python"\n', True),
    )
    try:
        for name, src, expect_reject in cases:
            m._source = (lambda s: (lambda _p: s))(src)
            rejected = False
            try:
                m.test_linux_backend_tooling_uses_dot_venv_only()
            except AssertionError:
                rejected = True
            check(name, rejected == expect_reject,
                  "已拒绝" if rejected else "已放行")
    finally:
        m._source = real_source


def main() -> int:
    print("统一门禁解释器探测离线契约（Windows Scripts/python.exe 自动识别）")
    test_default_candidates()
    test_guard_python_override()
    test_guard_python_missing()
    test_guard_python_blank_means_unset()
    test_posix_candidate_preferred()
    test_windows_candidate_detected()
    test_fallback_only_when_no_candidate()
    test_main_uses_resolver()
    test_migration_guard_rule_scope()
    print(f"\n  passed={PASSED} failed={FAILED}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
