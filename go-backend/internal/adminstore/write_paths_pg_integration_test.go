package adminstore

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/bcrypt"
	"graphinsight/go-backend/internal/config"
)

func s204PG(t *testing.T) *Client {
	t.Helper()
	dsn := os.Getenv("GI_P6_PG_DSN")
	if dsn == "" {
		t.Skip("GI_P6_PG_DSN 未注入：只在 gi-s203 一次性容器网络执行")
	}
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatal("invalid isolated DSN")
	}
	run := strings.TrimPrefix(u.Hostname(), "gi-s203-")
	if (u.Scheme != "postgresql" && u.Scheme != "postgres") || !regexp.MustCompile(`^[a-z0-9]{8,24}$`).MatchString(run) || u.Hostname() != "gi-s203-"+run || u.Port() != "5432" || u.Path != "/gi_s203_"+run || u.RawQuery != "" || u.Fragment != "" {
		t.Fatal("DSN must identify this gi-s203 disposable container")
	}
	c, err := New(config.Config{AdminDatabaseURL: dsn})
	if err != nil {
		t.Fatal("isolated store open failed")
	}
	t.Cleanup(func() { _ = c.Close() })
	var actual string
	if err := c.db.QueryRow("SELECT current_database()").Scan(&actual); err != nil || actual != "gi_s203_"+run {
		t.Fatal("database identity mismatch")
	}
	return c
}

func TestS204UsersPG(t *testing.T) {
	c := s204PG(t)
	ctx := context.Background()
	prefix := fmt.Sprintf("s204-%d", time.Now().UnixNano())
	t.Cleanup(func() {
		_, err := c.db.Exec("DELETE FROM admin_user_role_bindings WHERE user_id IN (SELECT id FROM admin_users WHERE username LIKE $1)", prefix+"%")
		if err != nil {
			t.Error("binding cleanup failed")
		}
		_, err = c.db.Exec("DELETE FROM admin_users WHERE username LIKE $1", prefix+"%")
		if err != nil {
			t.Error("user cleanup failed")
		}
	})
	create := func(t *testing.T, suffix string) UserItem {
		t.Helper()
		u, err := c.CreateUser(ctx, UserCreateRequest{Username: prefix + suffix, Email: prefix + suffix + "@example.test", Password: prefix + "-temporary", FullName: optionalAuditString("Original")})
		if err != nil {
			t.Fatalf("CreateUser failed: %v", err)
		}
		return u
	}
	t.Run("create_hash_and_active", func(t *testing.T) {
		u := create(t, "-create")
		var active bool
		var hash string
		var name string
		if err := c.db.QueryRow("SELECT is_active,password_hash,username FROM admin_users WHERE id=$1", u.ID).Scan(&active, &hash, &name); err != nil {
			t.Fatal(err)
		}
		if !active || !u.IsActive || name != u.Username {
			t.Fatal("creation result differs from stored row")
		}
		if _, err := bcrypt.Cost([]byte(hash)); err != nil {
			t.Fatal("password_hash is not a bcrypt hash")
		}
		if err := bcrypt.CompareHashAndPassword([]byte(hash), []byte(prefix+"-temporary")); err != nil {
			t.Fatal("stored password hash does not match created password")
		}
		t.Log("row exists; is_active=true; bcrypt hash shape verified (value withheld)")
	})
	t.Run("update_preserves_other_fields", func(t *testing.T) {
		u := create(t, "-update")
		name := "Changed"
		result, err := c.UpdateUser(ctx, UserUpdateRequest{UserID: u.ID, FullName: &name})
		if err != nil {
			t.Fatal(err)
		}
		var stored, email, username string
		var active bool
		if err := c.db.QueryRow("SELECT full_name,email,username,is_active FROM admin_users WHERE id=$1", u.ID).Scan(&stored, &email, &username, &active); err != nil {
			t.Fatal(err)
		}
		if stored != name || result.FullName == nil || *result.FullName != name || email != u.Email || username != u.Username || !active {
			t.Fatal("updated row contract failed")
		}
	})
	t.Run("soft_delete_keeps_row", func(t *testing.T) {
		u := create(t, "-soft")
		if err := c.DeleteUser(ctx, UserDeleteRequest{UserID: u.ID, SoftDelete: true}); err != nil {
			t.Fatal(err)
		}
		var active bool
		if err := c.db.QueryRow("SELECT is_active FROM admin_users WHERE id=$1", u.ID).Scan(&active); err != nil || active {
			t.Fatal("soft delete must keep inactive row")
		}
		t.Log("row exists; is_active=false")
	})
	t.Run("hard_delete_removes_row", func(t *testing.T) {
		u := create(t, "-hard")
		if err := c.DeleteUser(ctx, UserDeleteRequest{UserID: u.ID, SoftDelete: false}); err != nil {
			t.Fatal(err)
		}
		var n int
		if err := c.db.QueryRow("SELECT count(*) FROM admin_users WHERE id=$1", u.ID).Scan(&n); err != nil || n != 0 {
			t.Fatal("hard delete left user row")
		}
		t.Log("row count=0")
	})
	t.Run("binding_scope", func(t *testing.T) {
		u := create(t, "-binding")
		tenant, project, kb := prefix+"-tenant", prefix+"-project", prefix+"-kb"
		b, err := c.CreateRbacBinding(ctx, RbacBindingMutationRequest{UserID: u.ID, RoleName: "viewer", ScopeType: "kb", TenantID: &tenant, ProjectID: &project, KBID: &kb})
		if err != nil {
			t.Fatal(err)
		}
		var scope, a, p, k string
		var user int
		if err := c.db.QueryRow("SELECT user_id,scope_type,tenant_id,project_id,kb_id FROM admin_user_role_bindings WHERE id=$1", b.ID).Scan(&user, &scope, &a, &p, &k); err != nil {
			t.Fatal(err)
		}
		if user != u.ID || scope != "kb" || a != tenant || p != project || k != kb {
			t.Fatal("binding row mismatch")
		}
	})
	t.Run("duplicate_username_email", func(t *testing.T) {
		u := create(t, "-duplicate")
		for _, req := range []UserCreateRequest{{Username: u.Username, Email: prefix + "-other@example.test", Password: prefix}, {Username: prefix + "-other", Email: u.Email, Password: prefix}} {
			if _, err := c.CreateUser(ctx, req); !errors.Is(err, ErrUserConflict) {
				t.Fatalf("want ErrUserConflict got %v", err)
			}
		}
		var n int
		if err := c.db.QueryRow("SELECT count(*) FROM admin_users WHERE username=$1 OR email=$2", u.Username, u.Email).Scan(&n); err != nil || n != 1 {
			t.Fatal("conflict changed row count")
		}
	})
	t.Run("delete_missing", func(t *testing.T) {
		u := create(t, "-missing")
		if err := c.DeleteUser(ctx, UserDeleteRequest{UserID: u.ID}); err != nil {
			t.Fatal(err)
		}
		if err := c.DeleteUser(ctx, UserDeleteRequest{UserID: u.ID}); !errors.Is(err, ErrUserNotFound) {
			t.Fatalf("want ErrUserNotFound got %v", err)
		}
		var n int
		if err := c.db.QueryRow("SELECT count(*) FROM admin_users WHERE id=$1", u.ID).Scan(&n); err != nil || n != 0 {
			t.Fatal("missing row count mismatch")
		}
	})
}

func TestS204ConfigsUpsertPG(t *testing.T) {
	c := s204PG(t)
	ctx := context.Background()
	category := fmt.Sprintf("s204-%d", time.Now().UnixNano())
	t.Cleanup(func() {
		_, err := c.db.Exec("DELETE FROM admin_configs WHERE category=$1", category)
		if err != nil {
			t.Error("config cleanup failed")
		}
	})
	desc := "preserve-description"
	sensitive := true
	upsert := func(t *testing.T, description *string, value string) ConfigItem {
		t.Helper()
		tx, err := c.db.BeginTx(ctx, nil)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback()
		r, err := upsertConfigInTransaction(ctx, tx, ConfigMutationRequest{Category: category, Key: "setting", Value: value, Description: description, IsSensitive: &sensitive})
		if err != nil {
			t.Fatal(err)
		}
		if err := tx.Commit(); err != nil {
			t.Fatal(err)
		}
		return r
	}
	var id int
	t.Run("insert_version_one_flags", func(t *testing.T) {
		r := upsert(t, &desc, "synthetic-first")
		id = r.ID
		var v int
		var s, e bool
		if err := c.db.QueryRow("SELECT version,is_sensitive,is_encrypted FROM admin_configs WHERE id=$1", id).Scan(&v, &s, &e); err != nil {
			t.Fatal(err)
		}
		if r.Version != 1 || v != 1 || !s || e || !r.IsSensitive || r.IsEncrypted {
			t.Fatal("config initial flags/version mismatch")
		}
		t.Log("version=1 sensitive=true encrypted=false")
	})
	t.Run("update_version_coalesce_description", func(t *testing.T) {
		r := upsert(t, nil, "synthetic-second")
		var v, n int
		var d, value string
		var s, e bool
		if err := c.db.QueryRow("SELECT version,description,value,is_sensitive,is_encrypted FROM admin_configs WHERE id=$1", id).Scan(&v, &d, &value, &s, &e); err != nil {
			t.Fatal(err)
		}
		if err := c.db.QueryRow("SELECT count(*) FROM admin_configs WHERE category=$1 AND key='setting'", category).Scan(&n); err != nil {
			t.Fatal(err)
		}
		if r.ID != id || r.Version != 2 || v != 2 || d != desc || value != "synthetic-second" || n != 1 || !s || e {
			t.Fatal("upsert conflict update contract mismatch")
		}
		t.Log("same row; version=2; nil description preserved; encrypted=false")
	})
}

func TestS204RbacSeedPG(t *testing.T) {
	c := s204PG(t)
	ctx := context.Background()
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	// Transaction-level rollback restores the empty seed fixture in this disposable database.
	for _, table := range []string{"admin_user_role_bindings", "admin_role_permissions", "admin_permissions", "admin_roles"} {
		if _, err := tx.ExecContext(ctx, "DELETE FROM "+table); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := ensureRbacSeedDataTx(ctx, tx); err != nil {
		t.Fatal(err)
	}
	counts := func() [3]int {
		var r [3]int
		for i, table := range []string{"admin_roles", "admin_permissions", "admin_role_permissions"} {
			if err := tx.QueryRowContext(ctx, "SELECT count(*) FROM "+table).Scan(&r[i]); err != nil {
				t.Fatal(err)
			}
		}
		return r
	}
	before := counts()
	expectedBindings := 0
	for _, codes := range rolePermissionSeeds {
		expectedBindings += len(codes)
	}
	if before[0] != len(systemRoleDescriptions) || before[1] != len(permissionSeeds) || before[2] != expectedBindings {
		t.Fatalf("seed count mismatch roles=%d permissions=%d bindings=%d wantBindings=%d", before[0], before[1], before[2], expectedBindings)
	}
	for role, codes := range rolePermissionSeeds {
		var got []string
		rows, err := tx.QueryContext(ctx, `SELECT p.code FROM admin_role_permissions rp JOIN admin_roles r ON r.id=rp.role_id JOIN admin_permissions p ON p.id=rp.permission_id WHERE r.name=$1 ORDER BY p.code`, role)
		if err != nil {
			t.Fatal(err)
		}
		for rows.Next() {
			var code string
			if err := rows.Scan(&code); err != nil {
				rows.Close()
				t.Fatal(err)
			}
			got = append(got, code)
		}
		rows.Close()
		want := append([]string(nil), codes...)
		sort.Strings(want)
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("role %s permissions mismatch got=%v want=%v", role, got, want)
		}
	}
	if _, err := tx.ExecContext(ctx, "UPDATE admin_roles SET description='drift' WHERE name='viewer'"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.ExecContext(ctx, "UPDATE admin_permissions SET description='drift' WHERE code='kb:read'"); err != nil {
		t.Fatal(err)
	}
	if _, err := ensureRbacSeedDataTx(ctx, tx); err != nil {
		t.Fatal(err)
	}
	after := counts()
	if before != after {
		t.Fatal("seed created duplicates")
	}
	var roleDesc, permDesc string
	if err := tx.QueryRowContext(ctx, "SELECT description FROM admin_roles WHERE name='viewer'").Scan(&roleDesc); err != nil {
		t.Fatal(err)
	}
	if err := tx.QueryRowContext(ctx, "SELECT description FROM admin_permissions WHERE code='kb:read'").Scan(&permDesc); err != nil {
		t.Fatal(err)
	}
	if roleDesc != systemRoleDescriptions["viewer"] || permDesc == "drift" {
		t.Fatal("ON CONFLICT UPDATE not applied")
	}
	t.Logf("first/repeat counts=%v; drift repaired; transaction rolled back", after)
}

func TestS204ReindexPartialIndexPG(t *testing.T) {
	c := s204PG(t)
	ctx := context.Background()
	prefix := fmt.Sprintf("s204-%d", time.Now().UnixNano())
	kb, err := c.CreateKnowledgeBase(ctx, KBCreateRequest{ID: prefix + "-kb", TenantID: "s204-tenant", ProjectID: prefix, Name: prefix, StoragePrefix: "s204/" + prefix})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, query := range []string{"DELETE FROM admin_logs WHERE kb_id=$1", "DELETE FROM admin_jobs WHERE kb_id=$1", "DELETE FROM knowledge_bases WHERE id=$1"} {
			if _, err := c.db.Exec(query, kb.ID); err != nil {
				t.Error("reindex fixture cleanup failed")
			}
		}
	})
	kb2, err := c.CreateKnowledgeBase(ctx, KBCreateRequest{ID: prefix + "-kb2", TenantID: "s204-tenant", ProjectID: prefix + "-two", Name: prefix + "-two", StoragePrefix: "s204/" + prefix + "-two"})
	if err != nil {
		t.Fatal(err)
	}
	req := ReindexEnqueueRequest{Targets: []ReindexTarget{{KBID: kb.ID, DocID: prefix + "-doc", TenantID: kb.TenantID, ProjectID: kb.ProjectID, ChunkID: prefix + "-chunk", TargetRevision: 1}}, Source: "s204", TraceID: prefix, MaxRetries: 3}
	first, err := c.EnqueueReindexChunks(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	second, err := c.EnqueueReindexChunks(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if first.Created != 1 || second.Reused != 1 || len(first.Jobs) != 1 || len(second.Jobs) != 1 || first.Jobs[0].Job.ID != second.Jobs[0].Job.ID {
		t.Fatal("same hash was not reused")
	}
	var n int
	if err := c.db.QueryRow("SELECT count(*) FROM admin_jobs WHERE kb_id=$1 AND targets_hash=$2", kb.ID, first.Jobs[0].TargetsHash).Scan(&n); err != nil || n != 1 {
		t.Fatal("dedupe count mismatch")
	}
	for i := 0; i < 2; i++ {
		if _, err := c.db.Exec("INSERT INTO admin_jobs(job_type,status,kb_id,payload,retry_count,max_retries,targets_hash) VALUES('reindex_chunks','pending',$1,'{}',0,3,NULL)", kb.ID); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.db.QueryRow("SELECT count(*) FROM admin_jobs WHERE kb_id=$1 AND targets_hash IS NULL", kb.ID).Scan(&n); err != nil || n != 2 {
		t.Fatal("NULL rows incorrectly deduplicated")
	}
	t.Log("same hash: rows=1, reused ID; NULL hash: rows=2")
	var columns, predicate string
	if err := c.db.QueryRow(`SELECT string_agg(a.attname, ',' ORDER BY k.ord), pg_get_expr(i.indpred,i.indrelid) FROM pg_index i JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum,ord) ON true JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE i.indrelid='admin_jobs'::regclass AND i.indisunique AND pg_get_expr(i.indpred,i.indrelid) LIKE '%targets_hash%' GROUP BY i.indexrelid,i.indpred,i.indrelid HAVING string_agg(a.attname, ',' ORDER BY k.ord) = 'job_type,kb_id,targets_hash'`).Scan(&columns, &predicate); err != nil || columns != "job_type,kb_id,targets_hash" || strings.ToLower(strings.TrimSpace(predicate)) != "(targets_hash is not null)" {
		t.Fatalf("partial unique index contract mismatch columns=%q predicate=%q err=%v", columns, predicate, err)
	}
	var cross int
	if err := c.db.QueryRow("SELECT count(*) FROM admin_jobs WHERE targets_hash=$1 AND kb_id<>$2", first.Jobs[0].TargetsHash, kb.ID).Scan(&cross); err != nil {
		t.Fatal(err)
	}
	if cross != 0 {
		t.Fatal("unexpected cross-KB fixture")
	}
	if _, err := c.db.Exec("INSERT INTO admin_jobs(job_type,status,tenant_id,project_id,kb_id,payload,retry_count,max_retries,targets_hash) VALUES('reindex_chunks','pending','s204-tenant',$1,$2,'{}',0,3,$3)", kb2.ProjectID, kb2.ID, first.Jobs[0].TargetsHash); err != nil {
		t.Fatal("different KB same hash was rejected")
	}
	t.Cleanup(func() {
		_, _ = c.db.Exec("DELETE FROM admin_jobs WHERE kb_id=$1", kb2.ID)
		_, _ = c.db.Exec("DELETE FROM knowledge_bases WHERE id=$1", kb2.ID)
	})
	if _, err := c.db.Exec("INSERT INTO admin_jobs(job_type,status,tenant_id,project_id,kb_id,payload,retry_count,max_retries,targets_hash) VALUES('build_graph','pending','s204-tenant', $1, $2, '{}',0,3,$3)", prefix, kb.ID, first.Jobs[0].TargetsHash); err != nil {
		t.Fatal("different job_type same hash was rejected")
	}
	if err := c.db.QueryRow("SELECT count(*) FROM admin_jobs WHERE targets_hash=$1 AND (kb_id=$2 OR kb_id=$3)", first.Jobs[0].TargetsHash, kb.ID, kb2.ID).Scan(&cross); err != nil || cross != 3 {
		t.Fatal("cross-KB/job-type index row count mismatch")
	}
	t.Log("exact index=(job_type,kb_id,targets_hash), predicate=(targets_hash IS NOT NULL); same hash across KB/job type: 3 rows")
}
