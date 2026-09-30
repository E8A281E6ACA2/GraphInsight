package adminstore

// Python/Go RBAC 权限目录精确对账（Go 侧腿）。
//
// 与 backend/tests/check_rbac_catalog_parity.py 互为反向：本文件在 `go test ./...` 中
// 直接解析 Python 能力层的权限种子源码，断言两侧目录与授予集合逐字段相等。
// 两侧写同一张 admin_permissions 表，任何一侧单独漂移都会让"已授予"与"被强制"分离，
// 所以这里要求精确相等而不是子集包含。

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

const pythonAuthzRelPath = "backend/admin/services/authz_service.py"

var (
	pyPermissionRE = regexp.MustCompile(`\{"code": "([^"]+)", "resource_type": "([^"]+)", "action": "([^"]+)", "description": "([^"]+)"\}`)
	pyRoleBlockRE  = regexp.MustCompile(`(?s)"([a-z_]+)":\s*\[(.*?)\]`)
	quotedItemRE   = regexp.MustCompile(`"([^"]+)"`)
)

// readPythonAuthzSource 从包目录逐级上溯找到仓库根，再读 Python 侧权限种子源码。
// 逐级查找而不是写死 ../../../，是为了让测试在任何挂载 / 复制布局下都能定位到对账对象，
// 找不到就硬失败——对账腿不能被静默跳过。
func readPythonAuthzSource(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("resolve test working directory failed: %v", err)
	}
	for current, depth := dir, 0; depth < 8; depth++ {
		candidate := filepath.Join(current, pythonAuthzRelPath)
		if raw, readErr := os.ReadFile(candidate); readErr == nil {
			return string(raw)
		}
		parent := filepath.Dir(current)
		if parent == current {
			break
		}
		current = parent
	}
	t.Fatalf("python authz source %s not found while walking up from %s", pythonAuthzRelPath, dir)
	return ""
}

func pythonPermissions(t *testing.T, source string) map[string]string {
	t.Helper()
	body := pythonSection(t, source, "PERMISSION_DEFS = [", "]")
	permissions := map[string]string{}
	for _, match := range pyPermissionRE.FindAllStringSubmatch(body, -1) {
		permissions[match[1]] = fmt.Sprintf("%s|%s|%s", match[2], match[3], match[4])
	}
	if len(permissions) == 0 {
		t.Fatalf("no PERMISSION_DEFS entries parsed from python authz source")
	}
	return permissions
}

func pythonRoleGrants(t *testing.T, source string) map[string]map[string]bool {
	t.Helper()
	body := pythonSection(t, source, "ROLE_PERMISSION_CODES = {", "\n}")
	grants := map[string]map[string]bool{}
	for _, match := range pyRoleBlockRE.FindAllStringSubmatch(body, -1) {
		codes := map[string]bool{}
		for _, code := range quotedItemRE.FindAllStringSubmatch(match[2], -1) {
			codes[code[1]] = true
		}
		grants[match[1]] = codes
	}
	if len(grants) == 0 {
		t.Fatalf("no ROLE_PERMISSION_CODES entries parsed from python authz source")
	}
	return grants
}

// pythonSection 截取 from 标记之后到第一个 to 结束符之间的片段，用于按常量名取源码块。
func pythonSection(t *testing.T, source, from, to string) string {
	t.Helper()
	start := strings.Index(source, from)
	if start < 0 {
		t.Fatalf("python authz source is missing %q", from)
	}
	start += len(from)
	end := strings.Index(source[start:], to)
	if end < 0 {
		t.Fatalf("python authz section %q is not terminated", from)
	}
	return source[start : start+end]
}

func TestRBACPermissionCatalogMatchesPythonSeed(t *testing.T) {
	source := readPythonAuthzSource(t)
	pyPermissions := pythonPermissions(t, source)

	if len(permissionSeeds) != len(pyPermissions) {
		t.Fatalf("permission catalog size mismatch: go=%d python=%d", len(permissionSeeds), len(pyPermissions))
	}
	goPermissions := map[string]string{}
	for _, seed := range permissionSeeds {
		if _, duplicate := goPermissions[seed.Code]; duplicate {
			t.Fatalf("duplicate go permission code %s", seed.Code)
		}
		goPermissions[seed.Code] = fmt.Sprintf("%s|%s|%s", seed.ResourceType, seed.Action, seed.Description)
	}

	var mismatch []string
	for code, want := range pyPermissions {
		if got, ok := goPermissions[code]; !ok {
			mismatch = append(mismatch, fmt.Sprintf("%s missing on go side", code))
			continue
		} else if got != want {
			mismatch = append(mismatch, fmt.Sprintf("%s go=%q python=%q", code, got, want))
		}
	}
	for code := range goPermissions {
		if _, ok := pyPermissions[code]; !ok {
			mismatch = append(mismatch, fmt.Sprintf("%s missing on python side", code))
		}
	}
	sort.Strings(mismatch)
	if len(mismatch) > 0 {
		t.Fatalf("rbac permission catalog drift:\n  %s", strings.Join(mismatch, "\n  "))
	}
}

func TestRBACRoleGrantsMatchPythonSeed(t *testing.T) {
	source := readPythonAuthzSource(t)
	pyGrants := pythonRoleGrants(t, source)

	var mismatch []string
	for role, pyCodes := range pyGrants {
		goCodes, ok := rolePermissionSeeds[role]
		if !ok {
			mismatch = append(mismatch, fmt.Sprintf("role %s missing on go side", role))
			continue
		}
		set := map[string]bool{}
		for _, code := range goCodes {
			set[code] = true
		}
		for code := range pyCodes {
			if !set[code] {
				mismatch = append(mismatch, fmt.Sprintf("%s: %s granted only on python side", role, code))
			}
		}
		for code := range set {
			if !pyCodes[code] {
				mismatch = append(mismatch, fmt.Sprintf("%s: %s granted only on go side", role, code))
			}
		}
	}
	for role := range rolePermissionSeeds {
		if _, ok := pyGrants[role]; !ok {
			mismatch = append(mismatch, fmt.Sprintf("role %s missing on python side", role))
		}
	}
	sort.Strings(mismatch)
	if len(mismatch) > 0 {
		t.Fatalf("rbac role grant drift:\n  %s", strings.Join(mismatch, "\n  "))
	}
}

func TestGraphAdminPermissionIsSuperAdminOnly(t *testing.T) {
	source := readPythonAuthzSource(t)
	pyGrants := pythonRoleGrants(t, source)

	if _, ok := pythonPermissions(t, source)["graph:admin"]; !ok {
		t.Fatalf("graph:admin must be registered in the python permission catalog")
	}
	if !pyGrants["super_admin"]["graph:admin"] {
		t.Fatalf("graph:admin must be granted to super_admin on the python side")
	}
	for role, codes := range rolePermissionSeeds {
		for _, code := range codes {
			if code != "graph:admin" {
				continue
			}
			if role != "super_admin" {
				t.Fatalf("graph:admin must not be granted to %s", role)
			}
		}
	}
	for role, codes := range pyGrants {
		if codes["graph:admin"] && role != "super_admin" {
			t.Fatalf("graph:admin must not be granted to python role %s", role)
		}
	}
}

func TestReservedKBPermissionsAreSeededButNeverGranted(t *testing.T) {
	source := readPythonAuthzSource(t)
	pyPermissions := pythonPermissions(t, source)
	pyGrants := pythonRoleGrants(t, source)

	reserved := []string{"kb:review", "kb:manage", "kb:publish"}
	for _, code := range reserved {
		if _, ok := pyPermissions[code]; !ok {
			t.Fatalf("reserved permission %s must stay registered in the python catalog", code)
		}
		found := false
		for _, seed := range permissionSeeds {
			if seed.Code == code {
				found = true
				if seed.ResourceType != "kb" {
					t.Fatalf("reserved permission %s must keep resource_type kb, got %s", code, seed.ResourceType)
				}
			}
		}
		if !found {
			t.Fatalf("reserved permission %s must stay registered in the go catalog", code)
		}
		for role, codes := range rolePermissionSeeds {
			for _, granted := range codes {
				if granted == code {
					t.Fatalf("reserved permission %s must not be granted to go role %s", code, role)
				}
			}
		}
		for role, codes := range pyGrants {
			if codes[code] {
				t.Fatalf("reserved permission %s must not be granted to python role %s", code, role)
			}
		}
	}
}
