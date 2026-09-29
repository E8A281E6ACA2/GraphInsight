package scope

import (
	"net/http/httptest"
	"strings"
	"testing"
)

func mustError(t *testing.T, err *Error, wantCode string) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error %s, got nil", wantCode)
	}
	if err.Code != wantCode {
		t.Fatalf("expected error code %s, got %s (%s)", wantCode, err.Code, err.Message)
	}
}

func TestNormalizeScopeID(t *testing.T) {
	got, err := NormalizeScopeID("kb_id", " KB-A ")
	if err != nil || got != "kb-a" {
		t.Fatalf("expected kb-a after normalization, got %q err=%v", got, err)
	}
	if _, err := NormalizeScopeID("kb_id", "-abc"); err == nil || err.Code != CodeScopeInvalid {
		t.Fatalf("expected SCOPE_INVALID for leading dash, got %v", err)
	}
	if _, err := NormalizeScopeID("kb_id", "a"); err == nil || err.Code != CodeScopeInvalid {
		t.Fatalf("expected SCOPE_INVALID for single char, got %v", err)
	}
	if _, err := NormalizeScopeID("kb_id", "../etc"); err == nil || err.Code != CodeScopeInvalid {
		t.Fatalf("expected SCOPE_INVALID for path traversal, got %v", err)
	}
	if _, err := NormalizeScopeID("kb_id", strings.Repeat("a", 101)); err == nil || err.Code != CodeScopeInvalid {
		t.Fatalf("expected SCOPE_INVALID for overlong id, got %v", err)
	}
	if got, _ := NormalizeScopeID("kb_id", "  "); got != "" {
		t.Fatalf("expected empty result for blank value, got %q", got)
	}
}

func TestResolveFromRequestMissingScope(t *testing.T) {
	req := httptest.NewRequest("POST", "/api/docqa", nil)
	req.Header.Set("X-Tenant-Id", "tenant-a")
	_, err := ResolveFromRequest(req)
	mustError(t, err, CodeScopeRequired)
}

func TestResolveFromRequestCrossScope(t *testing.T) {
	req := httptest.NewRequest("POST", "/api/docqa?kb_id=kb-b", nil)
	req.Header.Set("X-Kb-Id", "kb-a")
	_, err := ResolveFromRequest(req)
	mustError(t, err, CodeCrossScope)
}

func TestResolveFromRequestNormalizesAndAccepts(t *testing.T) {
	req := httptest.NewRequest("POST", "/api/docqa?kb_id=kb-a", nil)
	req.Header.Set("X-Kb-Id", "KB-A")
	req.Header.Set("X-Tenant-Id", "Tenant-A")
	target, err := ResolveFromRequest(req)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(target.KBIDs) != 1 || target.KBIDs[0] != "kb-a" {
		t.Fatalf("expected [kb-a], got %v", target.KBIDs)
	}
	if target.TenantID != "tenant-a" {
		t.Fatalf("expected tenant-a, got %q", target.TenantID)
	}
}

func TestResolveFromRequestMultiKB(t *testing.T) {
	req := httptest.NewRequest("POST", "/api/docqa", nil)
	req.Header.Set("X-Kb-Ids", "kb-a, kb-b")
	target, err := ResolveFromRequest(req)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(target.KBIDs) != 2 || target.KBIDs[0] != "kb-a" || target.KBIDs[1] != "kb-b" {
		t.Fatalf("expected [kb-a kb-b], got %v", target.KBIDs)
	}
}

func TestResolveFromSourcesBodyInconsistent(t *testing.T) {
	header := scopeSource{kbID: "kb-a"}
	body := scopeSource{kbIDs: []string{"kb-b"}}
	_, err := ResolveFromSources(header, scopeSource{}, body)
	mustError(t, err, CodeCrossScope)
}

func TestResolveRequestWithBody(t *testing.T) {
	t.Run("body consistent with header normalizes and merges", func(t *testing.T) {
		req := httptest.NewRequest("POST", "/api/docqa?kb_id=kb-a", nil)
		req.Header.Set("X-Kb-Id", "KB-A")
		req.Header.Set("X-Tenant-Id", "tenant-a")
		body := BodySource("", "", "kb-a", nil)
		target, err := ResolveRequestWithBody(req, body)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(target.KBIDs) != 1 || target.KBIDs[0] != "kb-a" {
			t.Fatalf("expected [kb-a], got %v", target.KBIDs)
		}
		if target.TenantID != "tenant-a" {
			t.Fatalf("expected tenant-a, got %q", target.TenantID)
		}
	})

	t.Run("body kb_ids conflicting with header kb_id", func(t *testing.T) {
		req := httptest.NewRequest("POST", "/api/docqa", nil)
		req.Header.Set("X-Kb-Id", "kb-a")
		body := BodySource("", "", "", []string{"kb-b", "kb-c"})
		_, err := ResolveRequestWithBody(req, body)
		mustError(t, err, CodeCrossScope)
	})

	t.Run("body tenant conflicting with header tenant", func(t *testing.T) {
		req := httptest.NewRequest("POST", "/api/docqa?kb_id=kb-a", nil)
		req.Header.Set("X-Tenant-Id", "tenant-a")
		body := BodySource("tenant-b", "", "", nil)
		_, err := ResolveRequestWithBody(req, body)
		mustError(t, err, CodeCrossScope)
	})

	t.Run("missing scope everywhere", func(t *testing.T) {
		req := httptest.NewRequest("POST", "/api/docqa", nil)
		_, err := ResolveRequestWithBody(req, BodySource("", "", "", nil))
		mustError(t, err, CodeScopeRequired)
	})

	t.Run("query and body multi-kb agree", func(t *testing.T) {
		req := httptest.NewRequest("POST", "/api/docqa?kb_ids=kb-a,kb-b", nil)
		body := BodySource("", "", "", []string{"kb-a", "kb-b"})
		target, err := ResolveRequestWithBody(req, body)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(target.KBIDs) != 2 {
			t.Fatalf("expected 2 kb ids, got %v", target.KBIDs)
		}
	})
}

func TestEffectiveKBIDs(t *testing.T) {
	target := &SearchTarget{KBIDs: []string{"kb-a", "kb-b"}}
	effective, err := target.EffectiveKBIDs([]string{"kb-b", "kb-c"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(effective) != 1 || effective[0] != "kb-b" {
		t.Fatalf("expected [kb-b], got %v", effective)
	}

	denied := &SearchTarget{KBIDs: []string{"kb-x"}}
	_, err = denied.EffectiveKBIDs([]string{"kb-a"})
	mustError(t, err, CodeAccessDenied)

	empty := &SearchTarget{}
	_, err = empty.EffectiveKBIDs([]string{"kb-a"})
	mustError(t, err, CodeScopeRequired)
}

func TestEntityAndRelationKeys(t *testing.T) {
	keyA, err := EntityKey("kb-a", "品种A", "品种")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	keyB, _ := EntityKey("kb-b", "品种A", "品种")
	if keyA == keyB {
		t.Fatalf("same-name entities in different KBs must not share entity_key")
	}
	again, _ := EntityKey("kb-a", "品种a", "品种")
	if again != keyA {
		t.Fatalf("entity_key must be stable under name case normalization")
	}

	relA, _ := RelationKey("kb-a", keyA, "表现出", keyB, "chunk-1")
	relB, _ := RelationKey("kb-b", keyA, "表现出", keyB, "chunk-1")
	if relA == relB {
		t.Fatalf("same keys in different KBs must not share relation_key")
	}
}

func TestErrorHTTPStatus(t *testing.T) {
	cases := map[string]int{
		CodeScopeRequired:         400,
		CodeKBNotFound:            404,
		CodeAccessDenied:          403,
		CodeArchived:              409,
		CodeCrossScope:            400,
		CodeScopeInvalid:          400,
		CodeDuplicateName:         409,
		CodeChunkRevisionConflict: 409,
	}
	for code, want := range cases {
		e := newError(code, "x")
		if e.Status != want {
			t.Fatalf("error %s expected status %d, got %d", code, want, e.Status)
		}
	}
}
