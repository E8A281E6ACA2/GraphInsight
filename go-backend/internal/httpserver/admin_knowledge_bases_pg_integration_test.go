package httpserver

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
)

func TestAdminKnowledgeBasesPGCRUD(t *testing.T) {
	dsn := os.Getenv("GI_P6_PG_DSN")
	if dsn == "" {
		t.Skip("GI_P6_PG_DSN 未注入：只在一次性容器网络执行")
	}
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatal("invalid isolated DSN")
	}
	run := strings.TrimPrefix(u.Hostname(), "gi-s203-")
	if (u.Scheme != "postgresql" && u.Scheme != "postgres") || !regexp.MustCompile(`^[a-z0-9]{8,24}$`).MatchString(run) || u.Hostname() != "gi-s203-"+run || u.Port() != "5432" || u.Path != "/gi_s203_"+run || u.RawQuery != "" || u.Fragment != "" {
		t.Fatal("DSN is not an approved gi-s203 container target")
	}
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("open isolated PG failed")
	}
	t.Cleanup(func() { _ = db.Close() })
	var actual string
	if err := db.QueryRow("SELECT current_database()").Scan(&actual); err != nil || actual != "gi_s203_"+run {
		t.Fatal("database identity check failed")
	}
	client, err := adminstore.New(config.Config{AdminDatabaseURL: dsn})
	if err != nil {
		t.Fatal("store initialization failed")
	}
	defer client.Close()
	if err := client.CheckHealth(context.Background()); err != nil {
		t.Fatal("isolated PG unavailable")
	}
	guard := newKBAuthzTestGuard(t, &fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "s203"}})
	handler := buildAdminKnowledgeBasesHandler(newDiscardLogger(), guard, client, client)
	prefix := fmt.Sprintf("bug1-%d", time.Now().UnixNano())
	tenant, project := "bug1-tenant", prefix
	count := func() int {
		var n int
		if err := db.QueryRow("SELECT count(*) FROM knowledge_bases WHERE tenant_id=$1 AND project_id=$2", tenant, project).Scan(&n); err != nil {
			t.Fatal("row count failed")
		}
		return n
	}
	t.Cleanup(func() {
		if _, err := db.Exec("DELETE FROM admin_logs WHERE project_id=$1", project); err != nil {
			t.Error("audit cleanup failed")
		}
		if _, err := db.Exec("DELETE FROM knowledge_bases WHERE tenant_id=$1 AND project_id=$2", tenant, project); err != nil {
			t.Error("KB cleanup failed")
		}
	})
	request := func(t *testing.T, method, path, body string, status int, code string) map[string]interface{} {
		t.Helper()
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "admin@example.com", "test-secret", time.Now().Add(time.Hour)))
		w := httptest.NewRecorder()
		handler(w, r)
		if w.Code != status {
			t.Fatalf("HTTP want=%d got=%d response=%s", status, w.Code, w.Body.String())
		}
		var p struct {
			Data map[string]interface{} `json:"data"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &p); err != nil {
			t.Fatal(err)
		}
		if code != "" && p.Data["error_code"] != code {
			t.Fatalf("error_code want=%s got=%v", code, p.Data["error_code"])
		}
		return p.Data
	}
	list := adminKnowledgeBasesRoute + "?tenant_id=" + tenant + "&project_id=" + project
	create := func(t *testing.T, name string) string {
		data := request(t, http.MethodPost, adminKnowledgeBasesRoute, fmt.Sprintf(`{"tenant_id":%q,"project_id":%q,"name":%q,"slug":"original"}`, tenant, project, name), 201, "")
		id, _ := data["id"].(string)
		if id == "" {
			t.Fatal("missing KB id")
		}
		return id
	}
	row := func(id string, expectedStatus string, expectedSlug *string, description string) {
		t.Helper()
		var status string
		var slug, desc sql.NullString
		if err := db.QueryRow("SELECT status,slug,description FROM knowledge_bases WHERE id=$1", id).Scan(&status, &slug, &desc); err != nil {
			t.Fatal("row read failed")
		}
		if status != expectedStatus || (expectedSlug == nil && slug.Valid) || (expectedSlug != nil && (!slug.Valid || slug.String != *expectedSlug)) || desc.String != description {
			t.Fatalf("row mismatch status=%s slug=%v description=%s", status, slug, desc.String)
		}
		t.Logf("DB status=%s slug=%v description=%s", status, slug, desc.String)
	}
	original, newSlug := "original", "new-slug"
	var id string
	t.Run("01_missing_parent_scope", func(t *testing.T) {
		request(t, "GET", adminKnowledgeBasesRoute, "", 400, "KB_SCOPE_REQUIRED")
		if count() != 0 {
			t.Fatal("unexpected rows")
		}
	})
	t.Run("02_parent_scope_conflict", func(t *testing.T) {
		r := httptest.NewRequest("GET", list, nil)
		r.Header.Set("X-Tenant-ID", "other-tenant")
		w := httptest.NewRecorder()
		handler(w, r)
		if w.Code != 400 || decodeKBError(t, w) != "KB_CROSS_SCOPE" || count() != 0 {
			t.Fatal("scope conflict contract failed")
		}
	})
	t.Run("03_missing_name", func(t *testing.T) {
		request(t, "POST", adminKnowledgeBasesRoute, fmt.Sprintf(`{"tenant_id":%q,"project_id":%q}`, tenant, project), 400, "INVALID_BODY")
		if count() != 0 {
			t.Fatal("unexpected rows")
		}
	})
	t.Run("04_create", func(t *testing.T) {
		id = create(t, prefix+"-main")
		if count() != 1 {
			t.Fatal("missing row")
		}
		row(id, "active", &original, "")
	})
	if id == "" {
		t.Fatal("create prerequisite failed")
	}
	t.Run("05_duplicate", func(t *testing.T) {
		request(t, "POST", adminKnowledgeBasesRoute, fmt.Sprintf(`{"tenant_id":%q,"project_id":%q,"name":%q}`, tenant, project, prefix+"-main"), 409, "KB_DUPLICATE_NAME")
		if count() != 1 {
			t.Fatal("duplicate row written")
		}
	})
	t.Run("06_description_nil_slug", func(t *testing.T) {
		request(t, "PATCH", adminKnowledgeBasesRoute+"/"+id, `{"description":"x"}`, 200, "")
		row(id, "active", &original, "x")
	})
	t.Run("07_slug_nonempty", func(t *testing.T) {
		request(t, "PATCH", adminKnowledgeBasesRoute+"/"+id, `{"slug":"new-slug"}`, 200, "")
		row(id, "active", &newSlug, "x")
	})
	t.Run("08_slug_empty_null", func(t *testing.T) {
		request(t, "PATCH", adminKnowledgeBasesRoute+"/"+id, `{"slug":""}`, 200, "")
		row(id, "active", nil, "x")
	})
	t.Run("09_archive", func(t *testing.T) {
		request(t, "POST", adminKnowledgeBasesRoute+"/"+id+"/archive", `{}`, 200, "")
		row(id, "archived", nil, "x")
	})
	t.Run("10_archived_patch_rejected", func(t *testing.T) {
		request(t, "PATCH", adminKnowledgeBasesRoute+"/"+id, `{"description":"forbidden"}`, 409, "KB_ARCHIVED")
		row(id, "archived", nil, "x")
	})
	t.Run("11_restore", func(t *testing.T) {
		request(t, "PATCH", adminKnowledgeBasesRoute+"/"+id, `{"status":"active"}`, 200, "")
		row(id, "active", nil, "x")
	})
	t.Run("12_invalid_status", func(t *testing.T) {
		// 非法 status 值在 handler 入口即被拒绝（INVALID_BODY 400）；
		// KB_INVALID_STATE（409）表示"当前状态不支持该操作"，如 deleting 态，二者语义不同。
		request(t, "PATCH", adminKnowledgeBasesRoute+"/"+id, `{"status":"invalid"}`, 400, "INVALID_BODY")
		row(id, "active", nil, "x")
	})
	t.Run("13_pagination", func(t *testing.T) {
		create(t, prefix+"-page-a")
		create(t, prefix+"-page-b")
		data := request(t, "GET", list+"&page=2&page_size=2", "", 200, "")
		items, ok := data["items"].([]interface{})
		if !ok || len(items) != 1 || data["total"] != float64(count()) || data["total_pages"] != float64(2) || data["page"] != float64(2) {
			t.Fatalf("pagination mismatch %v", data)
		}
		item := items[0].(map[string]interface{})
		var n int
		if err := db.QueryRow("SELECT count(*) FROM knowledge_bases WHERE id=$1 AND tenant_id=$2 AND project_id=$3", item["id"], tenant, project).Scan(&n); err != nil || n != 1 {
			t.Fatal("page item not found in DB")
		}
		t.Logf("DB total=%d page=2 page_size=2 total_pages=2 items=1", count())
	})
}
