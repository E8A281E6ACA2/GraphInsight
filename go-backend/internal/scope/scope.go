// Package scope 实现知识库作用域契约（冻结）。
//
// 契约来源：docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md §2
// 规则：缺少 kb_id/kb_ids 一律 KB_SCOPE_REQUIRED，无 default KB 兜底；
// header/query/body 同时携带且不一致 → KB_CROSS_SCOPE；
// 请求范围与授权交集为空 → KB_ACCESS_DENIED。
// 字符串错误码与 Python 侧 backend/services/scope_contract.py 保持一致。
package scope

import (
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"
)

// 统一字符串错误码（契约 §2.9）。
const (
	CodeScopeRequired         = "KB_SCOPE_REQUIRED"
	CodeKBNotFound            = "KB_NOT_FOUND"
	CodeAccessDenied          = "KB_ACCESS_DENIED"
	CodeArchived              = "KB_ARCHIVED"
	CodeCrossScope            = "KB_CROSS_SCOPE"
	CodeDuplicateName         = "KB_DUPLICATE_NAME"
	CodeStoragePathInvalid    = "KB_STORAGE_PATH_INVALID"
	CodeScopeInvalid          = "SCOPE_INVALID"
	CodeChunkRevisionConflict = "CHUNK_REVISION_CONFLICT"
	CodeChunkNotFound         = "CHUNK_NOT_FOUND"
	CodeChunkContentEmpty     = "CHUNK_CONTENT_EMPTY"
	CodeReindexScopeRequired  = "REINDEX_SCOPE_REQUIRED"
	CodeIndexUnavailable      = "INDEX_UNAVAILABLE"
)

// ChunkRevision 是 Chunk 版本契约（§2.6，M5 落库），与 Python scope_contract.ChunkRevision 字段逐字一致。
type ChunkRevision struct {
	RevisionID            string  `json:"revision_id"`
	KBID                  string  `json:"kb_id"`
	TenantID              string  `json:"tenant_id"`
	ProjectID             string  `json:"project_id"`
	DocID                 string  `json:"doc_id"`
	ChunkID               string  `json:"chunk_id"`
	SourceContent         string  `json:"source_content"`
	SourceContentHash     string  `json:"source_content_hash"`
	Content               string  `json:"content"`
	ContentHash           string  `json:"content_hash"`
	ContentRevision       int64   `json:"content_revision"`
	RevisionStatus        string  `json:"revision_status"`
	GraphStatus           string  `json:"graph_status"`
	VectorStatus          string  `json:"vector_status"`
	GraphContentRevision  *int64  `json:"graph_content_revision,omitempty"`
	VectorContentRevision *int64  `json:"vector_content_revision,omitempty"`
	RevisionSource        string  `json:"revision_source"`
	SourceVersion         *string `json:"source_version,omitempty"`
	ParserVersion         *string `json:"parser_version,omitempty"`
	EditedBy              *int64  `json:"edited_by,omitempty"`
	EditedAt              string  `json:"edited_at"`
	Reason                *string `json:"reason,omitempty"`
	TraceID               string  `json:"trace_id,omitempty"`
}

// Error 是带统一错误码的 scope 错误，handler 直接映射进统一响应体。
type Error struct {
	Code    string
	Message string
	Status  int
}

func (e *Error) Error() string {
	return fmt.Sprintf("%s: %s", e.Code, e.Message)
}

// HTTP 状态映射（契约 §2.9）。
func httpStatusFor(code string) int {
	switch code {
	case CodeKBNotFound, CodeChunkNotFound:
		return http.StatusNotFound
	case CodeAccessDenied:
		return http.StatusForbidden
	case CodeArchived, CodeDuplicateName, CodeChunkRevisionConflict:
		return http.StatusConflict
	case CodeIndexUnavailable:
		return http.StatusServiceUnavailable
	default:
		return http.StatusBadRequest
	}
}

func newError(code, message string) *Error {
	return &Error{Code: code, Message: message, Status: httpStatusFor(code)}
}

// ErrScopeRequired 缺少 kb 作用域（无兜底，始终拒绝）。
func ErrScopeRequired() *Error {
	return newError(CodeScopeRequired, "请求缺少 kb_id/kb_ids，所有知识库请求必须显式携带作用域")
}

// ErrCrossScope 多来源作用域不一致。
func ErrCrossScope(field string) *Error {
	return newError(CodeCrossScope, "请求作用域不一致: "+field)
}

// ErrAccessDenied 请求范围与授权交集为空。
func ErrAccessDenied(requested []string) *Error {
	return newError(CodeAccessDenied, "请求的知识库不在授权范围内")
}

// scopeIDPattern 契约 §2.1（D5 规格）：先 trim + 小写归一，再校验。
var scopeIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{1,99}$`)

// NormalizeScopeID trim + 小写归一 + 格式校验；空值返回空串，非法值返回 SCOPE_INVALID 错误。
func NormalizeScopeID(kind, value string) (string, *Error) {
	normalized := strings.ToLower(strings.TrimSpace(value))
	if normalized == "" {
		return "", nil
	}
	if !scopeIDPattern.MatchString(normalized) {
		return "", newError(CodeScopeInvalid, fmt.Sprintf("%s 格式非法: %q", kind, value))
	}
	return normalized, nil
}

// Scope 是一次请求解析出的作用域三元组。
type Scope struct {
	TenantID  string
	ProjectID string
	KBID      string
}

// SearchTarget 是检索目标（契约 §2.4）。P0 只实现 KBIDs/DocumentIDs，其余为保留字段。
type SearchTarget struct {
	TenantID    string
	ProjectID   string
	KBIDs       []string
	DocumentIDs []string
	FolderIDs   []string // 保留字段，暂不实现
	TagIDs      []string // 保留字段，暂不实现
}

// splitScopeList 解析逗号分隔的多值 scope（如 x-kb-ids: kb-a,kb-b）。
func splitScopeList(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

// scopeSource 是一个作用域来源（header/query/body）的字段视图。
type scopeSource struct {
	tenantID  string
	projectID string
	kbID      string
	kbIDs     []string
}

func mergeSingleValue(kind string, values []string) (string, *Error) {
	seen := ""
	for _, value := range values {
		normalized, err := NormalizeScopeID(kind, value)
		if err != nil {
			return "", err
		}
		if normalized == "" {
			continue
		}
		if seen != "" && normalized != seen {
			return "", ErrCrossScope(kind)
		}
		if seen == "" {
			seen = normalized
		}
	}
	return seen, nil
}

func resolveKBSets(sources []scopeSource) ([]string, *Error) {
	var sets [][]string
	for _, source := range sources {
		raw := make([]string, 0, 2)
		if strings.TrimSpace(source.kbID) != "" {
			raw = append(raw, source.kbID)
		}
		raw = append(raw, source.kbIDs...)
		normalized := make([]string, 0, len(raw))
		for _, value := range raw {
			item, err := NormalizeScopeID("kb_id", value)
			if err != nil {
				return nil, err
			}
			if item == "" {
				continue
			}
			duplicate := false
			for _, existing := range normalized {
				if existing == item {
					duplicate = true
					break
				}
			}
			if !duplicate {
				normalized = append(normalized, item)
			}
		}
		if len(normalized) > 0 && !sameStringSet(normalized, sets) {
			sets = append(sets, normalized)
		}
	}
	if len(sets) > 1 {
		return nil, ErrCrossScope("kb_id/kb_ids")
	}
	if len(sets) == 0 {
		return nil, ErrScopeRequired()
	}
	return sets[0], nil
}

func sameStringSet(candidate []string, sets [][]string) bool {
	for _, set := range sets {
		if len(set) != len(candidate) {
			continue
		}
		counts := make(map[string]int, len(set))
		for _, item := range set {
			counts[item]++
		}
		match := true
		for _, item := range candidate {
			counts[item]--
			if counts[item] < 0 {
				match = false
				break
			}
		}
		if match {
			return true
		}
	}
	return false
}

// ResolveFromRequest 从 header/query 解析 SearchTarget（严格模式）。
// body 一致性由各 handler 解码后调用 ResolveFromSources 校验。
// 缺失 kb → KB_SCOPE_REQUIRED；header/query 不一致 → KB_CROSS_SCOPE。
func ResolveFromRequest(r *http.Request) (*SearchTarget, *Error) {
	header := scopeSource{
		tenantID:  r.Header.Get("x-tenant-id"),
		projectID: r.Header.Get("x-project-id"),
		kbID:      r.Header.Get("x-kb-id"),
		kbIDs:     splitScopeList(r.Header.Get("x-kb-ids")),
	}
	values := r.URL.Query()
	query := scopeSource{
		tenantID:  values.Get("tenant_id"),
		projectID: values.Get("project_id"),
		kbID:      values.Get("kb_id"),
		kbIDs:     splitScopeList(values.Get("kb_ids")),
	}
	return ResolveFromSources(header, query, scopeSource{})
}

// BodySource 供 handler 解码请求体后构造第三个作用域来源（契约 §3.2：
// header/query/body 三来源严格一致）。空字段表示该来源未携带对应作用域。
func BodySource(tenantID string, projectID string, kbID string, kbIDs []string) scopeSource {
	return scopeSource{
		tenantID:  tenantID,
		projectID: projectID,
		kbID:      kbID,
		kbIDs:     kbIDs,
	}
}

// ResolveRequestWithBody 从 header/query + handler 解码出的 body 来源解析 SearchTarget。
// 三来源同时携带且不一致 → KB_CROSS_SCOPE；缺失 kb → KB_SCOPE_REQUIRED。
func ResolveRequestWithBody(r *http.Request, body scopeSource) (*SearchTarget, *Error) {
	header := scopeSource{
		tenantID:  r.Header.Get("x-tenant-id"),
		projectID: r.Header.Get("x-project-id"),
		kbID:      r.Header.Get("x-kb-id"),
		kbIDs:     splitScopeList(r.Header.Get("x-kb-ids")),
	}
	values := r.URL.Query()
	query := scopeSource{
		tenantID:  values.Get("tenant_id"),
		projectID: values.Get("project_id"),
		kbID:      values.Get("kb_id"),
		kbIDs:     splitScopeList(values.Get("kb_ids")),
	}
	return ResolveFromSources(header, query, body)
}

// ResolveFromSources 三来源严格解析（body 由 handler 解码后构造传入）。
func ResolveFromSources(header, query, body scopeSource) (*SearchTarget, *Error) {
	sources := []scopeSource{header, query, body}
	tenantID, err := mergeSingleValue("tenant_id", []string{header.tenantID, query.tenantID, body.tenantID})
	if err != nil {
		return nil, err
	}
	projectID, err := mergeSingleValue("project_id", []string{header.projectID, query.projectID, body.projectID})
	if err != nil {
		return nil, err
	}
	kbIDs, err := resolveKBSets(sources)
	if err != nil {
		return nil, err
	}
	// DocumentIDs 由知识库 handler 解码 body 后显式填充（doc_ids 语义收编，契约 §2.4），
	// 不经由 scope header 传递。
	return &SearchTarget{
		TenantID:  tenantID,
		ProjectID: projectID,
		KBIDs:     kbIDs,
	}, nil
}

// EffectiveKBIDs 计算请求范围 ∩ 授权范围（契约 §2.4）。
// 授权集合为空或交集为空 → KB_ACCESS_DENIED；调用方保证 target.KBIDs 非空。
func (t *SearchTarget) EffectiveKBIDs(authorizedKBIDs []string) ([]string, *Error) {
	if len(t.KBIDs) == 0 {
		return nil, ErrScopeRequired()
	}
	authorized := make(map[string]bool, len(authorizedKBIDs))
	for _, item := range authorizedKBIDs {
		normalized, err := NormalizeScopeID("kb_id", item)
		if err != nil {
			return nil, err
		}
		if normalized != "" {
			authorized[normalized] = true
		}
	}
	effective := make([]string, 0, len(t.KBIDs))
	for _, item := range t.KBIDs {
		if authorized[item] {
			effective = append(effective, item)
		}
	}
	if len(effective) == 0 {
		return nil, ErrAccessDenied(t.KBIDs)
	}
	return effective, nil
}

// Grant 是知识库授权授予（契约 §2.5）。P0 复用 admin_user_role_bindings，不新建表。
type Grant struct {
	KBID         string
	SubjectType  string // user | api_key | application
	SubjectID    string
	Role         string // viewer | editor | admin
	Capabilities []string
}

// EntityKey 知识库作用域内稳定实体键（契约 §2.2）。禁止跨 kb 合并同名实体。
func EntityKey(kbID, normalizedName, entityType string) (string, error) {
	normalizedKB, err := NormalizeScopeID("kb_id", kbID)
	if err != nil {
		return "", err
	}
	material := strings.Join([]string{
		normalizedKB,
		strings.ToLower(strings.TrimSpace(normalizedName)),
		strings.ToLower(strings.TrimSpace(entityType)),
	}, "|")
	return shortSHA256(material), nil
}

// RelationKey 知识库作用域内稳定关系键（契约 §2.2）。
func RelationKey(kbID, subjectKey, predicate, objectKey, evidenceChunkID string) (string, error) {
	normalizedKB, err := NormalizeScopeID("kb_id", kbID)
	if err != nil {
		return "", err
	}
	material := strings.Join([]string{
		normalizedKB,
		subjectKey,
		strings.ToLower(strings.TrimSpace(predicate)),
		objectKey,
		evidenceChunkID,
	}, "|")
	return shortSHA256(material), nil
}

func shortSHA256(material string) string {
	sum := sha256.Sum256([]byte(material))
	return fmt.Sprintf("%x", sum)[:32]
}

// ParseQueryString 供测试与非 HTTP 场景构造 query 视图。
func ParseQueryString(rawQuery string) url.Values {
	values, _ := url.ParseQuery(rawQuery)
	return values
}
