package scope

import (
	"strings"
	"testing"
)

func TestNewUUIDFormat(t *testing.T) {
	t.Parallel()

	id := NewUUID()
	if len(id) != 36 {
		t.Fatalf("expected 36-char uuid, got %q (len %d)", id, len(id))
	}
	if id[8] != '-' || id[13] != '-' || id[18] != '-' || id[23] != '-' {
		t.Fatalf("expected dashed uuid layout, got %q", id)
	}
	if id[14] != '4' {
		t.Fatalf("expected version 4 nibble at index 14, got %q", id[14])
	}
	switch id[19] {
	case '8', '9', 'a', 'b':
	default:
		t.Fatalf("expected RFC 4122 variant nibble at index 19, got %q", id[19])
	}
	if strings.ToLower(id) != id {
		t.Fatalf("expected lowercase hex, got %q", id)
	}
	for _, part := range strings.Split(id, "-") {
		if part == "" {
			t.Fatalf("expected non-empty hex group in %q", id)
		}
	}
}

func TestNewUUIDIsUniqueInProcess(t *testing.T) {
	t.Parallel()

	seen := make(map[string]struct{}, 1000)
	for i := 0; i < 1000; i++ {
		id := NewUUID()
		if _, exists := seen[id]; exists {
			t.Fatalf("duplicate uuid generated: %s", id)
		}
		seen[id] = struct{}{}
	}
}
