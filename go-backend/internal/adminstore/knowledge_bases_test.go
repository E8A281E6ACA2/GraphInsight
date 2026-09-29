package adminstore

import (
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

type fakeKBRow struct {
	values []interface{}
}

func (r *fakeKBRow) Scan(dest ...interface{}) error {
	if len(dest) != len(r.values) {
		return errors.New("fake kb row destination count mismatch")
	}
	for i, target := range dest {
		switch typed := target.(type) {
		case *string:
			value, _ := r.values[i].(string)
			*typed = value
		case *sql.NullString:
			value, ok := r.values[i].(string)
			if !ok || value == "" {
				*typed = sql.NullString{}
			} else {
				*typed = sql.NullString{String: value, Valid: true}
			}
		case *sql.NullInt64:
			value, ok := r.values[i].(int64)
			if !ok {
				*typed = sql.NullInt64{}
			} else {
				*typed = sql.NullInt64{Int64: value, Valid: true}
			}
		case *time.Time:
			value, _ := r.values[i].(time.Time)
			*typed = value
		case *sql.NullTime:
			value, ok := r.values[i].(time.Time)
			if !ok {
				*typed = sql.NullTime{}
			} else {
				*typed = sql.NullTime{Time: value, Valid: true}
			}
		default:
			return errors.New("fake kb row unsupported destination type")
		}
	}
	return nil
}

func TestNormalizeKBListQueryRequiresParentScope(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name      string
		query     KBListQuery
		wantError error
	}{
		{
			name:      "missing tenant",
			query:     KBListQuery{ProjectID: "project-a"},
			wantError: ErrKBParentScopeRequired,
		},
		{
			name:      "missing project",
			query:     KBListQuery{TenantID: "tenant-a"},
			wantError: ErrKBParentScopeRequired,
		},
		{
			name:      "blank parent scope",
			query:     KBListQuery{TenantID: "  ", ProjectID: "project-a"},
			wantError: ErrKBParentScopeRequired,
		},
		{
			name:      "invalid status filter",
			query:     KBListQuery{TenantID: "tenant-a", ProjectID: "project-a", Status: "running"},
			wantError: ErrKBValidation,
		},
	}
	for _, tt := range tests {
		tt := tt
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			err := normalizeKBListQuery(&tt.query)
			if !errors.Is(err, tt.wantError) {
				t.Fatalf("expected %v, got %v", tt.wantError, err)
			}
		})
	}
}

func TestNormalizeKBListQueryNormalizesPagination(t *testing.T) {
	t.Parallel()

	query := KBListQuery{TenantID: " tenant-a ", ProjectID: "project-a", Page: -1, PageSize: 500}
	if err := normalizeKBListQuery(&query); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if query.TenantID != "tenant-a" || query.ProjectID != "project-a" {
		t.Fatalf("expected trimmed scope, got %q/%q", query.TenantID, query.ProjectID)
	}
	if query.Page != 1 || query.PageSize != 200 {
		t.Fatalf("unexpected pagination: page=%d pageSize=%d", query.Page, query.PageSize)
	}

	query = KBListQuery{TenantID: "tenant-a", ProjectID: "project-a", Status: " archived ", Page: 0, PageSize: 0}
	if err := normalizeKBListQuery(&query); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if query.Status != "archived" || query.Page != 1 || query.PageSize != 20 {
		t.Fatalf("unexpected defaults: status=%q page=%d pageSize=%d", query.Status, query.Page, query.PageSize)
	}
}

// 契约 §3.2 结构性例外：目录列表必须始终带 tenant/project 父作用域硬条件，不允许全局列表。
func TestBuildKBListWhereAlwaysScopesParentScope(t *testing.T) {
	t.Parallel()

	where, args := buildKBListWhere(KBListQuery{TenantID: "tenant-a", ProjectID: "project-a"})
	if where != " WHERE kb.tenant_id = $1 AND kb.project_id = $2" {
		t.Fatalf("unexpected where: %s", where)
	}
	if len(args) != 2 || args[0] != "tenant-a" || args[1] != "project-a" {
		t.Fatalf("unexpected args: %#v", args)
	}

	where, args = buildKBListWhere(KBListQuery{TenantID: "tenant-a", ProjectID: "project-a", Status: "archived"})
	if where != " WHERE kb.tenant_id = $1 AND kb.project_id = $2 AND kb.status = $3" {
		t.Fatalf("unexpected where: %s", where)
	}
	if len(args) != 3 || args[2] != "archived" {
		t.Fatalf("unexpected args: %#v", args)
	}
}

func TestValidateKBCreateRequest(t *testing.T) {
	t.Parallel()

	valid := KBCreateRequest{
		ID:            "kb-uuid",
		TenantID:      "tenant-a",
		ProjectID:     "project-a",
		Name:          "农业试验知识库",
		StoragePrefix: "tenant-a/project-a/kb-uuid",
	}
	if err := validateKBCreateRequest(valid); err != nil {
		t.Fatalf("expected valid request, got %v", err)
	}

	tests := []struct {
		name string
		mut  func(req KBCreateRequest) KBCreateRequest
	}{
		{"missing id", func(req KBCreateRequest) KBCreateRequest { req.ID = " "; return req }},
		{"missing tenant", func(req KBCreateRequest) KBCreateRequest { req.TenantID = ""; return req }},
		{"missing project", func(req KBCreateRequest) KBCreateRequest { req.ProjectID = ""; return req }},
		{"overlong tenant", func(req KBCreateRequest) KBCreateRequest { req.TenantID = string(make([]byte, 101)); return req }},
		{"missing name", func(req KBCreateRequest) KBCreateRequest { req.Name = "   "; return req }},
		{"overlong name", func(req KBCreateRequest) KBCreateRequest { req.Name = string(make([]rune, 201)); return req }},
		{"overlong slug", func(req KBCreateRequest) KBCreateRequest {
			slug := string(make([]rune, 101))
			req.Slug = &slug
			return req
		}},
		{"missing storage prefix", func(req KBCreateRequest) KBCreateRequest { req.StoragePrefix = ""; return req }},
		{"overlong storage prefix", func(req KBCreateRequest) KBCreateRequest { req.StoragePrefix = string(make([]byte, 256)); return req }},
	}
	for _, tt := range tests {
		tt := tt
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if err := validateKBCreateRequest(tt.mut(valid)); !errors.Is(err, ErrKBValidation) {
				t.Fatalf("expected ErrKBValidation, got %v", err)
			}
		})
	}
}

func TestValidateKBUpdateRequest(t *testing.T) {
	t.Parallel()

	kbID := "kb-uuid"
	active := KBStatusActive
	archived := KBStatusArchived
	deleting := KBStatusDeleting

	if err := validateKBUpdateRequest(KBUpdateRequest{KBID: kbID}); err != nil {
		t.Fatalf("expected empty update valid, got %v", err)
	}
	if err := validateKBUpdateRequest(KBUpdateRequest{KBID: kbID, Status: &active}); err != nil {
		t.Fatalf("expected restore status valid, got %v", err)
	}
	if err := validateKBUpdateRequest(KBUpdateRequest{KBID: kbID, Status: &archived}); err != nil {
		t.Fatalf("expected archive status valid, got %v", err)
	}

	tests := []struct {
		name string
		req  KBUpdateRequest
	}{
		{"missing kb id", KBUpdateRequest{KBID: " "}},
		{"empty name", KBUpdateRequest{KBID: kbID, Name: stringPointer("  ")}},
		{"overlong name", KBUpdateRequest{KBID: kbID, Name: stringPointer(string(make([]rune, 201)))}},
		{"overlong slug", KBUpdateRequest{KBID: kbID, Slug: stringPointer(string(make([]rune, 101)))}},
		{"deleting status not allowed via update", KBUpdateRequest{KBID: kbID, Status: &deleting}},
		{"unknown status", KBUpdateRequest{KBID: kbID, Status: stringPointer("paused")}},
	}
	for _, tt := range tests {
		tt := tt
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if err := validateKBUpdateRequest(tt.req); !errors.Is(err, ErrKBValidation) {
				t.Fatalf("expected ErrKBValidation, got %v", err)
			}
		})
	}
}

func TestScanKnowledgeBaseItemMapsColumns(t *testing.T) {
	t.Parallel()

	createdAt := time.Date(2026, 9, 27, 8, 0, 0, 0, time.UTC)
	updatedAt := createdAt.Add(time.Hour)
	archivedAt := createdAt.Add(2 * time.Hour)
	row := &fakeKBRow{values: []interface{}{
		"kb-uuid",
		"tenant-a",
		"project-a",
		"农业试验知识库",
		"agri-kb",
		"农业试验论文和技术报告",
		KBStatusActive,
		"tenant-a/project-a/kb-uuid",
		`{"provider":"native"}`,
		`{"mode":"graph_hybrid"}`,
		`{"source":"m2-test"}`,
		int64(7),
		int64(9),
		createdAt,
		updatedAt,
		archivedAt,
	}}
	item, err := scanKnowledgeBaseItem(row)
	if err != nil {
		t.Fatalf("scan failed: %v", err)
	}
	if item.ID != "kb-uuid" || item.TenantID != "tenant-a" || item.ProjectID != "project-a" {
		t.Fatalf("unexpected identity: %#v", item)
	}
	if item.Slug == nil || *item.Slug != "agri-kb" || item.Description == nil {
		t.Fatalf("unexpected text fields: %#v", item)
	}
	if item.ParserProfile["provider"] != "native" || item.RetrievalProfile["mode"] != "graph_hybrid" {
		t.Fatalf("unexpected profiles: %#v %#v", item.ParserProfile, item.RetrievalProfile)
	}
	if item.Metadata["source"] != "m2-test" {
		t.Fatalf("unexpected metadata: %#v", item.Metadata)
	}
	if item.CreatedBy == nil || *item.CreatedBy != 7 || item.UpdatedBy == nil || *item.UpdatedBy != 9 {
		t.Fatalf("unexpected operators: %#v %#v", item.CreatedBy, item.UpdatedBy)
	}
	if item.CreatedAt != createdAt || item.UpdatedAt == nil || item.ArchivedAt == nil {
		t.Fatalf("unexpected timestamps: %#v", item)
	}

	empty := &fakeKBRow{values: []interface{}{
		"kb-uuid", "tenant-a", "project-a", "kb", "", "", KBStatusActive, "prefix", "", "", "", int64(0), int64(0), createdAt, nil, nil,
	}}
	item, err = scanKnowledgeBaseItem(empty)
	if err != nil {
		t.Fatalf("scan empty row failed: %v", err)
	}
	if item.Slug != nil || item.ParserProfile != nil || item.Metadata != nil {
		t.Fatalf("expected nil nullable fields, got %#v", item)
	}
	if item.UpdatedAt != nil || item.ArchivedAt != nil {
		t.Fatalf("expected nil timestamps, got %#v", item)
	}
}

func TestEncodeKBProfile(t *testing.T) {
	t.Parallel()

	encoded, err := encodeKBProfile(nil)
	if encoded != nil || err != nil {
		t.Fatalf("expected nil profile for empty input, got %v err=%v", encoded, err)
	}
	encoded, err = encodeKBProfile(map[string]interface{}{"top_k": 8})
	if err != nil {
		t.Fatalf("encode failed: %v", err)
	}
	if *encoded != `{"top_k":8}` {
		t.Fatalf("unexpected encoded profile: %s", *encoded)
	}
}

func TestMapKnowledgeBaseConstraintError(t *testing.T) {
	t.Parallel()

	duplicate := &pgconn.PgError{Code: "23505", ConstraintName: "uq_knowledge_base_scope_name"}
	if !errors.Is(mapKnowledgeBaseConstraintError("insert failed", duplicate), ErrKBDuplicateName) {
		t.Fatalf("expected unique violation mapped to ErrKBDuplicateName")
	}
	pk := &pgconn.PgError{Code: "23505", ConstraintName: "knowledge_bases_pkey"}
	if errors.Is(mapKnowledgeBaseConstraintError("insert failed", pk), ErrKBDuplicateName) {
		t.Fatalf("primary key violation must not map to ErrKBDuplicateName")
	}
	other := errors.New("connection refused")
	if errors.Is(mapKnowledgeBaseConstraintError("insert failed", other), ErrKBDuplicateName) {
		t.Fatalf("unrelated error must not map to ErrKBDuplicateName")
	}
}

func TestIsValidKBStatus(t *testing.T) {
	t.Parallel()

	for _, status := range []string{"active", " archived ", "deleting"} {
		if !isValidKBStatus(status) {
			t.Fatalf("expected %q to be valid", status)
		}
	}
	for _, status := range []string{"", "running", "paused", "ACTIVE"} {
		if isValidKBStatus(status) {
			t.Fatalf("expected %q to be invalid", status)
		}
	}
}
