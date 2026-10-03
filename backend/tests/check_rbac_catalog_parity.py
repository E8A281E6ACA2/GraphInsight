#!/usr/bin/env python3
"""Python/Go RBAC 权限目录精确对账守卫。

口径：Go 网关（`go-backend/internal/adminstore/rbac_seed.go`）与 Python 能力层
（`backend/admin/services/authz_service.py`）各自维护一份权限种子目录，两侧写同一张
`admin_permissions` / `admin_role_permissions` 表。任何一侧漂移都会让"权限已授予"与
"权限被强制"分离，因此这里做的是精确对账而不是子集包含：

  1. 权限码集合相等，且每个码的 resource_type / action / description 逐字段相等；
  2. 系统角色名与描述相等，每个角色的授予集合相等；
  3. `graph:admin` 只能授予 super_admin，且 Go 侧 `/api/query` 必须强制它；
  4. 预留 kb 权限码（kb:review / kb:manage / kb:publish）在目录中但不授予任何角色，
     且不得出现在任何强制点；
  5. 两侧所有强制点使用的权限码都必须已在目录中注册（否则线上只会 fail-closed）。

两侧都用静态源码解析，不导入模块，避免依赖数据库与运行时配置。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
GO_SEED = REPO / "go-backend" / "internal" / "adminstore" / "rbac_seed.go"
GO_HANDLERS = REPO / "go-backend" / "internal" / "httpserver" / "handlers.go"
PY_AUTHZ = ROOT / "admin" / "services" / "authz_service.py"

RESERVED_KB_PERMISSIONS = ("kb:review", "kb:manage", "kb:publish")
ADMIN_ONLY_PERMISSIONS = ("graph:admin",)

# 允许出现预留码的文件：种子目录本身与本守卫（声明口径所需）。
RESERVED_ALLOWLIST = {
    "go-backend/internal/adminstore/rbac_seed.go",
    "backend/admin/services/authz_service.py",
    "backend/tests/check_rbac_catalog_parity.py",
    "go-backend/internal/adminstore/rbac_seed_parity_test.go",
}


class Check:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, name: str, condition: bool, detail: str) -> None:
        if condition:
            self.passed += 1
            print(f"CHECK {name} PASS {detail}")
            return
        self.failed.append(name)
        print(f"CHECK {name} FAIL {detail}")


def _go_block(source: str, marker: str) -> str:
    start = source.index(marker)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1 : index]
    raise AssertionError(f"unbalanced block for {marker!r}")


def _go_role_blocks(source: str) -> dict[str, list[str]]:
    body = _go_block(source, "var rolePermissionSeeds = map[string][]string")
    roles: dict[str, list[str]] = {}
    for match in re.finditer(r'"([a-z_]+)":\s*\{(.*?)\}', body, re.S):
        roles[match.group(1)] = re.findall(r'"([^"]+)"', match.group(2))
    return roles


def parse_go_catalog() -> tuple[dict[str, tuple[str, str, str]], dict[str, set[str]], dict[str, str]]:
    source = GO_SEED.read_text(encoding="utf-8")
    permissions: dict[str, tuple[str, str, str]] = {}
    for match in re.finditer(
        r'\{Code:\s*"([^"]+)",\s*ResourceType:\s*"([^"]+)",\s*Action:\s*"([^"]+)",\s*Description:\s*"([^"]+)"\}',
        source,
    ):
        permissions[match.group(1)] = (match.group(2), match.group(3), match.group(4))
    roles = {name: set(codes) for name, codes in _go_role_blocks(source).items()}
    role_descriptions = {
        match.group(1): match.group(2)
        for match in re.finditer(r'"([a-z_]+)":\s*"([^"]+)",', _go_block(source, "var systemRoleDescriptions = map[string]string"))
    }
    return permissions, roles, role_descriptions


def _python_literal(source: str, name: str) -> object:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"missing python constant {name}")


def parse_python_catalog() -> tuple[dict[str, tuple[str, str, str]], dict[str, set[str]], dict[str, str]]:
    source = PY_AUTHZ.read_text(encoding="utf-8")
    defs = _python_literal(source, "PERMISSION_DEFS")
    permissions = {
        item["code"]: (item["resource_type"], item["action"], item["description"]) for item in defs
    }
    role_map = _python_literal(source, "ROLE_PERMISSION_CODES")
    roles = {name: set(codes) for name, codes in role_map.items()}
    role_descriptions = dict(_python_literal(source, "SYSTEM_ROLES"))
    return permissions, roles, role_descriptions


def _go_enforced_permissions() -> set[str]:
    source = GO_HANDLERS.read_text(encoding="utf-8")
    return set(re.findall(r'guard\.wrap\(\s*"([^"]+)"', source))


def _python_enforced_permissions() -> set[str]:
    codes: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        codes.update(re.findall(r'require_(?:admin_)?permission\(\s*"([^"]+)"', text))
    return codes


SCANNABLE_SUFFIXES = (".go", ".py", ".ts")
# 扫描范围只取 git 跟踪的源文件；rglob 兜底时排除忽略目录与嵌套 worktree，
# 避免 .claude/worktrees、study/、虚拟环境里的副本被误判成预留码强制点。
EXCLUDE_DIR_TOKENS = (
    ".git",
    "node_modules",
    ".claude",
    ".worktrees",
    "worktrees",
    "study",
    ".venv",
    "venv",
)


def _scannable_source_files() -> list[Path]:
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(REPO), "ls-files", "-z"], text=True, errors="replace"
        )
    except Exception:  # noqa: BLE001 - git 不可用时走兜底扫描
        raw = None
    if raw:
        files = []
        for rel in raw.split("\0"):
            if not rel or not rel.endswith(SCANNABLE_SUFFIXES):
                continue
            path = REPO / rel
            if path.is_file():
                files.append(path)
        if files:
            return sorted(files)
    fallback = []
    for suffix in SCANNABLE_SUFFIXES:
        for path in REPO.rglob(f"*{suffix}"):
            if any(tok in path.parts for tok in EXCLUDE_DIR_TOKENS):
                continue
            fallback.append(path)
    return sorted(fallback)


def _reserved_code_references() -> list[str]:
    hits: list[str] = []
    for path in _scannable_source_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in RESERVED_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for code in RESERVED_KB_PERMISSIONS:
            if f'"{code}"' in text or f"'{code}'" in text or f"`{code}`" in text:
                hits.append(f"{rel}:{code}")
    return hits


def main() -> int:
    check = Check()
    go_perms, go_roles, go_role_desc = parse_go_catalog()
    py_perms, py_roles, py_role_desc = parse_python_catalog()

    check.ok(
        "catalog_sizes_parsed",
        len(go_perms) >= 10 and len(py_perms) >= 10,
        f"go={len(go_perms)} python={len(py_perms)}",
    )
    check.ok(
        "permission_codes_equal",
        set(go_perms) == set(py_perms),
        f"only_go={sorted(set(go_perms) - set(py_perms))} only_python={sorted(set(py_perms) - set(go_perms))}",
    )
    field_mismatch = [
        code
        for code in set(go_perms) & set(py_perms)
        if go_perms[code] != py_perms[code]
    ]
    check.ok(
        "permission_fields_equal",
        not field_mismatch,
        f"mismatched={field_mismatch} detail={[ (c, go_perms[c], py_perms[c]) for c in field_mismatch ]}",
    )
    check.ok(
        "role_names_equal",
        set(go_roles) == set(py_roles),
        f"only_go={sorted(set(go_roles) - set(py_roles))} only_python={sorted(set(py_roles) - set(go_roles))}",
    )
    check.ok(
        "role_descriptions_equal",
        go_role_desc == py_role_desc,
        f"go={len(go_role_desc)} python={len(py_role_desc)} diff={sorted(set(go_role_desc.items()) ^ set(py_role_desc.items()))}",
    )
    grant_diff = [
        f"{role}: only_go={sorted(go_roles[role] - py_roles[role])} only_python={sorted(py_roles[role] - go_roles[role])}"
        for role in set(go_roles) & set(py_roles)
        if go_roles[role] != py_roles[role]
    ]
    check.ok("role_grants_equal", not grant_diff, f"diff={grant_diff}")

    unknown_to_catalog = sorted(
        code
        for code in (_go_enforced_permissions() | _python_enforced_permissions())
        if code not in go_perms or code not in py_perms
    )
    check.ok(
        "enforced_codes_are_seeded",
        not unknown_to_catalog,
        f"unregistered={unknown_to_catalog}",
    )

    for code in ADMIN_ONLY_PERMISSIONS:
        granted = sorted(role for role, codes in go_roles.items() if code in codes)
        granted_py = sorted(role for role, codes in py_roles.items() if code in codes)
        check.ok(
            f"{code}_super_admin_only",
            granted == ["super_admin"] and granted_py == ["super_admin"],
            f"go_roles={granted} python_roles={granted_py}",
        )
    check.ok(
        "graph_admin_enforced_on_query",
        'guard.wrap("graph:admin"' in GO_HANDLERS.read_text(encoding="utf-8"),
        f"handlers.go route owner check",
    )

    for code in RESERVED_KB_PERMISSIONS:
        check.ok(
            f"reserved_{code}_in_catalog",
            code in go_perms and code in py_perms,
            "present on both sides",
        )
        holders = sorted([r for r, c in go_roles.items() if code in c] + [r for r, c in py_roles.items() if code in c])
        check.ok(f"reserved_{code}_granted_nowhere", not holders, f"holders={holders}")
    references = _reserved_code_references()
    check.ok("reserved_codes_not_enforced", not references, f"references={references}")

    print(
        "RBAC_CATALOG_PARITY_SUMMARY "
        f"permissions={len(go_perms)}/{len(py_perms)} roles={len(go_roles)}/{len(py_roles)} "
        f"passed={check.passed} failed={len(check.failed)}"
    )
    return 0 if not check.failed else 1


if __name__ == "__main__":
    sys.exit(main())
