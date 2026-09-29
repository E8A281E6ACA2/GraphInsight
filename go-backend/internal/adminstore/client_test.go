package adminstore

import (
	"reflect"
	"testing"
	"time"

	"golang.org/x/crypto/bcrypt"
)

func TestAuthorizedKBsFromBindings(t *testing.T) {
	t.Parallel()

	qaPermissions := map[string]bool{"qa:ask": true, "nl2cypher:use": true}

	tests := []struct {
		name             string
		bindings         []permissionBinding
		permission       map[string]bool
		failOpen         bool
		wantKBIDs        []string
		wantAllKBs       bool
		wantParentScopes []kbParentScope
	}{
		{
			name:       "no binding deny",
			wantKBIDs:  nil,
			wantAllKBs: false,
		},
		{
			name:       "no binding fail open means all kbs",
			failOpen:   true,
			wantKBIDs:  nil,
			wantAllKBs: true,
		},
		{
			name: "global qa binding grants all kbs",
			bindings: []permissionBinding{
				{PermissionCode: "qa:ask", ScopeType: "global"},
			},
			permission: qaPermissions,
			wantAllKBs: true,
		},
		{
			name: "global binding for other permission does not grant qa scope",
			bindings: []permissionBinding{
				{PermissionCode: "job:read", ScopeType: "global"},
			},
			permission: qaPermissions,
			wantAllKBs: false,
		},
		{
			name: "kb scoped bindings give explicit ids normalized and deduplicated",
			bindings: []permissionBinding{
				{PermissionCode: "qa:ask", ScopeType: "kb", KBID: "KB-A"},
				{PermissionCode: "qa:ask", ScopeType: "kb", KBID: "kb-a"},
				{PermissionCode: "nl2cypher:use", ScopeType: "kb", KBID: "kb-b"},
			},
			permission: qaPermissions,
			wantKBIDs:  []string{"kb-a", "kb-b"},
		},
		{
			// M4-R1 FIX #1：tenant/project 绑定不再放大为全部 KB，而是记录父作用域供目录反查。
			name: "tenant or project scoped binding resolves via parent scopes not all kbs",
			bindings: []permissionBinding{
				{PermissionCode: "qa:ask", ScopeType: "tenant", TenantID: "tenant-a"},
				{PermissionCode: "qa:ask", ScopeType: "project", ProjectID: "project-a"},
			},
			permission:       qaPermissions,
			wantAllKBs:       false,
			wantParentScopes: []kbParentScope{{TenantID: "tenant-a", ProjectID: ""}, {TenantID: "", ProjectID: "project-a"}},
		},
		{
			name: "tenant binding with dirty scope is skipped fail-closed",
			bindings: []permissionBinding{
				{PermissionCode: "qa:ask", ScopeType: "tenant", TenantID: "../escape"},
			},
			permission: qaPermissions,
			wantAllKBs: false,
		},
		{
			name: "invalid kb binding is skipped not fatal",
			bindings: []permissionBinding{
				{PermissionCode: "qa:ask", ScopeType: "kb", KBID: "../escape"},
				{PermissionCode: "qa:ask", ScopeType: "kb", KBID: "kb-ok"},
			},
			permission: qaPermissions,
			wantKBIDs:  []string{"kb-ok"},
		},
	}

	for _, tc := range tests {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			gotKBIDs, gotAllKBs, gotParentScopes := authorizedKBsFromBindings(tc.bindings, tc.permission, tc.failOpen)
			if gotAllKBs != tc.wantAllKBs {
				t.Fatalf("expected allKBs=%v, got %v (kbIDs=%v)", tc.wantAllKBs, gotAllKBs, gotKBIDs)
			}
			if tc.wantAllKBs {
				return
			}
			if !reflect.DeepEqual(gotKBIDs, tc.wantKBIDs) {
				t.Fatalf("expected kbIDs %v, got %v", tc.wantKBIDs, gotKBIDs)
			}
			if len(gotParentScopes) != len(tc.wantParentScopes) {
				t.Fatalf("expected parent scopes %v, got %v", tc.wantParentScopes, gotParentScopes)
			}
			for i := range gotParentScopes {
				if gotParentScopes[i] != tc.wantParentScopes[i] {
					t.Fatalf("parent scope %d expected %v, got %v", i, tc.wantParentScopes[i], gotParentScopes[i])
				}
			}
		})
	}
}

func TestEvaluatePermissionBindingsMatchesPythonAuthzRules(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name       string
		bindings   []permissionBinding
		permission string
		scope      map[string]string
		failOpen   bool
		allowed    bool
		reason     string
	}{
		{
			name:       "no binding deny",
			permission: "graph:read",
			allowed:    false,
			reason:     "no_binding",
		},
		{
			name:       "no binding fail open",
			permission: "graph:read",
			failOpen:   true,
			allowed:    true,
			reason:     "legacy_allow_no_binding",
		},
		{
			name: "permission missing",
			bindings: []permissionBinding{
				{PermissionCode: "job:read", ScopeType: "global"},
			},
			permission: "graph:read",
			allowed:    false,
			reason:     "permission_missing",
		},
		{
			name: "no request scope allows matching permission",
			bindings: []permissionBinding{
				{PermissionCode: "graph:read", ScopeType: "tenant", TenantID: "tenant-1"},
			},
			permission: "graph:read",
			allowed:    true,
			reason:     "allowed_without_scope",
		},
		{
			name: "tenant scope match",
			bindings: []permissionBinding{
				{PermissionCode: "graph:read", ScopeType: "tenant", TenantID: "tenant-1"},
			},
			permission: "graph:read",
			scope:      map[string]string{"x-tenant-id": "tenant-1"},
			allowed:    true,
			reason:     "allowed",
		},
		{
			name: "tenant scope mismatch",
			bindings: []permissionBinding{
				{PermissionCode: "graph:read", ScopeType: "tenant", TenantID: "tenant-1"},
			},
			permission: "graph:read",
			scope:      map[string]string{"x-tenant-id": "tenant-2"},
			allowed:    false,
			reason:     "scope_mismatch",
		},
		{
			name: "global scope match",
			bindings: []permissionBinding{
				{PermissionCode: "graph:read", ScopeType: "global"},
			},
			permission: "graph:read",
			scope:      map[string]string{"x-kb-id": "kb-1"},
			allowed:    true,
			reason:     "allowed",
		},
	}

	for _, tt := range tests {
		tt := tt
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			result := evaluatePermissionBindings(tt.bindings, tt.permission, tt.scope, tt.failOpen)
			if result.Allowed != tt.allowed || result.Reason != tt.reason {
				t.Fatalf("unexpected result: allowed=%v reason=%s", result.Allowed, result.Reason)
			}
		})
	}
}

func TestNormalizeScopeAcceptsHeaderAndCanonicalKeys(t *testing.T) {
	t.Parallel()

	scope := normalizeScope(map[string]string{
		"x-tenant-id": "tenant-1",
		"project_id":  "project-1",
		"x-kb-id":     "kb-1",
	})
	if scope["tenant_id"] != "tenant-1" || scope["project_id"] != "project-1" || scope["kb_id"] != "kb-1" {
		t.Fatalf("unexpected normalized scope: %#v", scope)
	}
}

func TestRbacScopeValidationMatchesPythonRules(t *testing.T) {
	t.Parallel()

	tenantID := "tenant-1"
	projectID := "project-1"
	kbID := "kb-1"
	tests := []struct {
		name string
		req  RbacBindingMutationRequest
		want bool
	}{
		{name: "global no scope field", req: RbacBindingMutationRequest{ScopeType: "global"}, want: true},
		{name: "tenant missing tenant id", req: RbacBindingMutationRequest{ScopeType: "tenant"}, want: false},
		{name: "tenant has tenant id", req: RbacBindingMutationRequest{ScopeType: "tenant", TenantID: &tenantID}, want: true},
		{name: "project missing project id", req: RbacBindingMutationRequest{ScopeType: "project"}, want: false},
		{name: "project has project id", req: RbacBindingMutationRequest{ScopeType: "project", ProjectID: &projectID}, want: true},
		{name: "kb missing kb id", req: RbacBindingMutationRequest{ScopeType: "kb"}, want: false},
		{name: "kb has kb id", req: RbacBindingMutationRequest{ScopeType: "kb", KBID: &kbID}, want: true},
	}
	for _, tt := range tests {
		tt := tt
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := rbacScopeFieldsValid(tt.req); got != tt.want {
				t.Fatalf("expected %v, got %v", tt.want, got)
			}
		})
	}
}

func TestNormalizeRbacScopeType(t *testing.T) {
	t.Parallel()

	if got := normalizeRbacScopeType(""); got != "global" {
		t.Fatalf("expected global fallback, got %s", got)
	}
	if got := normalizeRbacScopeType(" Tenant "); got != "tenant" {
		t.Fatalf("expected tenant, got %s", got)
	}
	if isAllowedRbacScopeType("workspace") {
		t.Fatalf("unexpected allowed custom scope")
	}
}

func TestBuildUserListWhere(t *testing.T) {
	t.Parallel()

	active := true
	where, args := buildUserListWhere(UserListQuery{
		Search:     "alice",
		IsActive:   &active,
		Department: "ops",
	})
	expected := " WHERE (username ILIKE $1 OR email ILIKE $1 OR full_name ILIKE $1) AND is_active = $2 AND department = $3"
	if where != expected {
		t.Fatalf("unexpected where: %s", where)
	}
	if len(args) != 3 || args[0] != "%alice%" || args[1] != true || args[2] != "ops" {
		t.Fatalf("unexpected args: %#v", args)
	}
}

func TestAdminUserOrderColumnAllowlist(t *testing.T) {
	t.Parallel()

	if got := adminUserOrderColumn("email"); got != "email" {
		t.Fatalf("expected email, got %s", got)
	}
	if got := adminUserOrderColumn("created_at; DROP TABLE admin_users"); got != "created_at" {
		t.Fatalf("expected created_at fallback, got %s", got)
	}
}

func TestUserUpdateFields(t *testing.T) {
	t.Parallel()

	email := "user@example.com"
	active := false
	fields := userUpdateFields(UserUpdateRequest{Email: &email, IsActive: &active})
	expected := []string{"email", "is_active"}
	if !reflect.DeepEqual(fields, expected) {
		t.Fatalf("unexpected update fields: %#v", fields)
	}
}

func TestNormalizeUserIDList(t *testing.T) {
	t.Parallel()

	got := normalizeUserIDList([]int{5, -1, 3, 5, 0, 2})
	expected := []int{2, 3, 5}
	if !reflect.DeepEqual(got, expected) {
		t.Fatalf("unexpected ids: %#v", got)
	}
}

func TestHashAdminUserPasswordUsesBcrypt(t *testing.T) {
	t.Parallel()

	hashed, err := hashAdminUserPassword("SmokePass123")
	if err != nil {
		t.Fatalf("hash password: %v", err)
	}
	if err := bcrypt.CompareHashAndPassword([]byte(hashed), []byte("SmokePass123")); err != nil {
		t.Fatalf("bcrypt hash does not verify: %v", err)
	}
	if _, err := hashAdminUserPassword("short"); err == nil {
		t.Fatalf("expected weak password to fail")
	}
}

func TestBuildLogWhere(t *testing.T) {
	t.Parallel()

	userID := 7
	start := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	end := time.Date(2026, 6, 2, 0, 0, 0, 0, time.UTC)
	where, args := buildLogWhere(LogListQuery{
		UserID:    &userID,
		Action:    "login",
		Resource:  "auth",
		Status:    "success",
		TraceID:   "trace-1",
		StartDate: &start,
		EndDate:   &end,
		IPAddress: "127.0.0.1",
	})
	expectedWhere := " WHERE l.user_id = $1 AND l.action = $2 AND l.resource = $3 AND l.status = $4 AND l.trace_id = $5 AND l.created_at >= $6 AND l.created_at <= $7 AND l.ip_address = $8"
	if where != expectedWhere {
		t.Fatalf("unexpected where: %s", where)
	}
	expectedArgs := []interface{}{7, "login", "auth", "success", "trace-1", start, end, "127.0.0.1"}
	if !reflect.DeepEqual(args, expectedArgs) {
		t.Fatalf("unexpected args: %#v", args)
	}
}

func TestClassifyLogSeverityMatchesPythonRules(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name         string
		status       string
		action       string
		errorMessage *string
		want         string
	}{
		{name: "failed", status: "failed", action: "login", want: "error"},
		{name: "error message", status: "success", action: "login", errorMessage: stringPointer("bad"), want: "error"},
		{name: "delete action", status: "success", action: "user_delete", want: "warn"},
		{name: "cleanup action", status: "success", action: "logs_cleanup", want: "warn"},
		{name: "normal", status: "success", action: "login", want: "info"},
	}
	for _, tt := range tests {
		tt := tt
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := classifyLogSeverity(tt.status, tt.action, tt.errorMessage); got != tt.want {
				t.Fatalf("expected %s, got %s", tt.want, got)
			}
		})
	}
}

func TestParseLogDetails(t *testing.T) {
	t.Parallel()

	parsed := parseLogDetails(stringPointer(`{"a":1}`))
	asMap, ok := parsed.(map[string]interface{})
	if !ok || asMap["a"] != float64(1) {
		t.Fatalf("unexpected parsed details: %#v", parsed)
	}
	raw := parseLogDetails(stringPointer("not-json"))
	rawMap, ok := raw.(map[string]string)
	if !ok || rawMap["raw"] != "not-json" {
		t.Fatalf("unexpected raw details: %#v", raw)
	}
	if parseLogDetails(nil) != nil {
		t.Fatalf("expected nil details")
	}
}

func TestBuildQATraceWhere(t *testing.T) {
	t.Parallel()

	operatorID := 3
	where, args := buildQATraceWhere(QATraceListQuery{
		QAType:     "docqa",
		Status:     "success",
		TraceID:    "trace-1",
		OperatorID: &operatorID,
		Keyword:    "neo4j",
	})
	expectedWhere := " WHERE q.qa_type = $1 AND q.status = $2 AND q.trace_id = $3 AND q.operator_id = $4 AND (q.question ILIKE $5 OR q.answer_preview ILIKE $5)"
	if where != expectedWhere {
		t.Fatalf("unexpected where: %s", where)
	}
	expectedArgs := []interface{}{"docqa", "success", "trace-1", 3, "%neo4j%"}
	if !reflect.DeepEqual(args, expectedArgs) {
		t.Fatalf("unexpected args: %#v", args)
	}
}

func TestExtractQATokenUsage(t *testing.T) {
	t.Parallel()

	usage := extractQATokenUsage(stringPointer(`{"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}`))
	if usage.PromptTokens != 11 || usage.CompletionTokens != 7 || usage.TotalTokens != 18 {
		t.Fatalf("unexpected usage: %#v", usage)
	}
	fallback := extractQATokenUsage(stringPointer(`{"usage":{"prompt_tokens":11,"completion_tokens":7}}`))
	if fallback.TotalTokens != 18 {
		t.Fatalf("expected fallback total tokens, got %#v", fallback)
	}
	empty := extractQATokenUsage(stringPointer(`{"no_usage":true}`))
	if empty.TotalTokens != 0 || empty.PromptTokens != 0 || empty.CompletionTokens != 0 {
		t.Fatalf("expected empty usage, got %#v", empty)
	}
}

func TestNormalizeQATracePagination(t *testing.T) {
	t.Parallel()

	page, pageSize := normalizeQATracePagination(-1, 1000)
	if page != 1 || pageSize != 200 {
		t.Fatalf("unexpected pagination: page=%d pageSize=%d", page, pageSize)
	}
}

func TestBuildJobWhere(t *testing.T) {
	t.Parallel()

	where, args := buildJobWhere(JobListQuery{
		JobType:   "build_graph",
		Status:    "running",
		TenantID:  "tenant-1",
		ProjectID: "project-1",
		KBID:      "kb-1",
	})
	expectedWhere := " WHERE j.job_type = $1 AND j.status = $2 AND j.tenant_id = $3 AND j.project_id = $4 AND j.kb_id = $5"
	if where != expectedWhere {
		t.Fatalf("unexpected where: %s", where)
	}
	expectedArgs := []interface{}{"build_graph", "running", "tenant-1", "project-1", "kb-1"}
	if !reflect.DeepEqual(args, expectedArgs) {
		t.Fatalf("unexpected args: %#v", args)
	}
}

func TestParseObjectJSONForJobs(t *testing.T) {
	t.Parallel()

	empty := parseObjectJSONOrEmpty(nil)
	if len(empty) != 0 {
		t.Fatalf("expected empty map, got %#v", empty)
	}
	object := parseObjectJSONOrNil(stringPointer(`{"source":"test"}`))
	if object["source"] != "test" {
		t.Fatalf("unexpected object: %#v", object)
	}
	array := parseObjectJSONOrNil(stringPointer(`[1,2]`))
	if _, ok := array["raw"].([]interface{}); !ok {
		t.Fatalf("expected raw array, got %#v", array)
	}
	raw := parseObjectJSONOrNil(stringPointer(`not-json`))
	if raw["raw"] != "not-json" {
		t.Fatalf("expected raw string, got %#v", raw)
	}
}

func TestNormalizeJobPagination(t *testing.T) {
	t.Parallel()

	page, pageSize := normalizeJobPagination(0, 300)
	if page != 1 || pageSize != 200 {
		t.Fatalf("unexpected job pagination: page=%d pageSize=%d", page, pageSize)
	}
	page, pageSize = normalizeJobLogPagination(0, 300)
	if page != 1 || pageSize != 200 {
		t.Fatalf("unexpected job log pagination: page=%d pageSize=%d", page, pageSize)
	}
}

func TestBuildConfigWhere(t *testing.T) {
	t.Parallel()

	sensitive := true
	where, args := buildConfigWhere(ConfigListQuery{
		Category:    "ai_service",
		Key:         "api",
		IsSensitive: &sensitive,
	})
	expectedWhere := " WHERE c.category = $1 AND c.key LIKE $2 AND c.is_sensitive = $3"
	if where != expectedWhere {
		t.Fatalf("unexpected where: %s", where)
	}
	expectedArgs := []interface{}{"ai_service", "%api%", true}
	if !reflect.DeepEqual(args, expectedArgs) {
		t.Fatalf("unexpected args: %#v", args)
	}
}

func TestSensitiveConfigKeyName(t *testing.T) {
	t.Parallel()

	for _, key := range []string{"api_key", "password", "secret_token"} {
		if !isSensitiveConfigKeyName(key) {
			t.Fatalf("expected sensitive key: %s", key)
		}
	}
	if isSensitiveConfigKeyName("api_key_configured") {
		t.Fatalf("configured marker should not be sensitive")
	}
	if isSensitiveConfigKeyName("model") {
		t.Fatalf("model should not be sensitive")
	}
	for _, key := range []string{"max_tokens", "max_output_tokens", "context_window"} {
		if isSensitiveConfigKeyName(key) {
			t.Fatalf("token limit config should not be sensitive: %s", key)
		}
	}
}

func TestNormalizeConfigPagination(t *testing.T) {
	t.Parallel()

	page, pageSize := normalizeConfigPagination(0, 500)
	if page != 1 || pageSize != 100 {
		t.Fatalf("unexpected config pagination: page=%d pageSize=%d", page, pageSize)
	}
}

func stringPointer(value string) *string {
	return &value
}
