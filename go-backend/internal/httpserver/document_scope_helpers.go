package httpserver

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"path/filepath"
	"strings"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/scope"
)

// adminDocumentStore 是知识库文档注册表存储接口；由 adminstore.Client 实现（M3 交付）。
type adminDocumentStore interface {
	InsertDocument(ctx context.Context, req adminstore.DocumentInsertRequest) (adminstore.DocumentRegistryItem, error)
	GetDocument(ctx context.Context, docID string) (adminstore.DocumentRegistryItem, error)
	ListDocumentsByKB(ctx context.Context, query adminstore.DocumentListQuery) (adminstore.DocumentListResult, error)
	MarkDocumentStatus(ctx context.Context, req adminstore.DocumentStatusUpdate) (adminstore.DocumentRegistryItem, error)
	DeleteDocumentRow(ctx context.Context, docID string, kbID string) error
	DeleteDocumentRowsByKB(ctx context.Context, kbID string) (int64, error)
}

func asAdminDocumentStore(store interface{}) adminDocumentStore {
	typed, _ := store.(adminDocumentStore)
	return typed
}

// 编译期保证 adminstore.Client 满足文档注册表存储接口（与 adminKBStore 相同的窄接口链）。
var _ adminDocumentStore = (*adminstore.Client)(nil)

// resolveAuthorizedKBForRequest 是业务文档路由（/api/documents*）的公共前置（契约 §2/§3）：
//  1. 解析 kb 作用域（header x-kb-id / query kb_id，经 internal/scope 严格规范化）：
//     缺失 → KB_SCOPE_REQUIRED；非法 → SCOPE_INVALID；多值/不一致 → KB_CROSS_SCOPE。
//  2. 加载 KB（adminstore 为权威）：不存在 → 404 KB_NOT_FOUND；
//     archived → 409 KB_ARCHIVED；deleting → 409 KB_INVALID_STATE。
//  3. 请求自带 tenant/project/kb scope 与 KB 行不一致 → KB_CROSS_SCOPE。
//  4. 携带 {tenant, project, kb} 完整作用域执行第二阶段权限检查（M2 checkPermissionWithScope），
//     拒绝 → 403 KB_ACCESS_DENIED。
func resolveAuthorizedKBForRequest(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	permission string,
) (adminstore.KnowledgeBaseItem, bool) {
	if kbStore == nil {
		logger.Error("knowledge base store unavailable for document route")
		WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	kbID, scopeErr := resolveSingleKBScopeFromRequest(r)
	if scopeErr != nil {
		writeScopeError(w, scopeErr)
		return adminstore.KnowledgeBaseItem{}, false
	}
	item, err := kbStore.GetKnowledgeBase(r.Context(), kbID)
	if errors.Is(err, adminstore.ErrKBNotFound) {
		WriteJSON(w, http.StatusNotFound, "知识库不存在", map[string]string{"error_code": scope.CodeKBNotFound})
		return adminstore.KnowledgeBaseItem{}, false
	}
	if err != nil {
		logger.Error("get knowledge base failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "查询知识库失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	switch item.Status {
	case adminstore.KBStatusArchived:
		WriteJSON(w, http.StatusConflict, "知识库已归档，禁止该操作", map[string]string{"error_code": scope.CodeArchived})
		return adminstore.KnowledgeBaseItem{}, false
	case adminstore.KBStatusDeleting:
		WriteJSON(w, http.StatusConflict, "知识库正在删除，禁止该操作", map[string]string{"error_code": "KB_INVALID_STATE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	if scopeErr := ensureKBRequestScopeMatches(r, item); scopeErr != nil {
		writeScopeError(w, scopeErr)
		return adminstore.KnowledgeBaseItem{}, false
	}
	if !guard.checkPermissionWithScope(r, permission, kbScopeMap(item)) {
		writeScopeError(w, scope.ErrAccessDenied(nil))
		return adminstore.KnowledgeBaseItem{}, false
	}
	return item, true
}

// authorizeGraphKBReadScope 是普通图谱工作台只读接口（/api/graph/schema、/api/expand、
// /api/node/{id}）的公共前置（M4-R1 FIX #3，契约 §3/§10.3、D1）：
//  1. 解析并强制单一 KB 作用域（header x-kb-id / query kb_id；expand 额外并入 body kb_id 并做一致性校验）。
//  2. 加载 KB 行（adminstore 为权威）：不存在 → 404 KB_NOT_FOUND。
//  3. 请求自带 tenant/project 与 KB 行不一致 → KB_CROSS_SCOPE。
//  4. 携带 {tenant, project, kb} 完整作用域执行第二阶段 graph:read 权限检查，拒绝 → 403 KB_ACCESS_DENIED。
//
// 读取类接口不阻断 archived/deleting（仅鉴权），与任务读取保持同一口径。
func authorizeGraphKBReadScope(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	bodyKBID string,
) (adminstore.KnowledgeBaseItem, bool) {
	if kbStore == nil {
		logger.Error("knowledge base store unavailable for graph read route")
		WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	kbID, scopeErr := resolveSingleKBScopeFromRequest(r)
	if scopeErr != nil {
		if scopeErr.Code != scope.CodeScopeRequired || strings.TrimSpace(bodyKBID) == "" {
			writeScopeError(w, scopeErr)
			return adminstore.KnowledgeBaseItem{}, false
		}
		// header/query 未提供，但请求体携带 kb_id（expand）：以 body 为准。
		normalized, normalizeErr := scope.NormalizeScopeID("kb_id", bodyKBID)
		if normalizeErr != nil {
			writeScopeError(w, normalizeErr)
			return adminstore.KnowledgeBaseItem{}, false
		}
		kbID = normalized
	} else if strings.TrimSpace(bodyKBID) != "" {
		normalized, normalizeErr := scope.NormalizeScopeID("kb_id", bodyKBID)
		if normalizeErr != nil {
			writeScopeError(w, normalizeErr)
			return adminstore.KnowledgeBaseItem{}, false
		}
		if normalized != "" && normalized != kbID {
			writeScopeError(w, scope.ErrCrossScope("kb_id"))
			return adminstore.KnowledgeBaseItem{}, false
		}
	}
	if kbID == "" {
		writeScopeError(w, scope.ErrScopeRequired())
		return adminstore.KnowledgeBaseItem{}, false
	}
	item, err := kbStore.GetKnowledgeBase(r.Context(), kbID)
	if errors.Is(err, adminstore.ErrKBNotFound) {
		WriteJSON(w, http.StatusNotFound, "知识库不存在", map[string]string{"error_code": scope.CodeKBNotFound})
		return adminstore.KnowledgeBaseItem{}, false
	}
	if err != nil {
		logger.Error("get knowledge base for graph read route failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "查询知识库失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	if scopeErr := ensureKBRequestScopeMatches(r, item); scopeErr != nil {
		writeScopeError(w, scopeErr)
		return adminstore.KnowledgeBaseItem{}, false
	}
	if !guard.checkPermissionWithScope(r, "graph:read", kbScopeMap(item)) {
		writeScopeError(w, scope.ErrAccessDenied(nil))
		return adminstore.KnowledgeBaseItem{}, false
	}
	return item, true
}

// resolveSingleKBScopeFromRequest 解析业务文档路由的单一 kb 作用域。
// 复用 scope.ResolveFromRequest（header/query 双来源一致性检查），但文档路由
// 只允许恰好一个 kb（kb_ids 多值视为跨作用域）。
func resolveSingleKBScopeFromRequest(r *http.Request) (string, *scope.Error) {
	target, scopeErr := scope.ResolveFromRequest(r)
	if scopeErr != nil {
		return "", scopeErr
	}
	if target == nil || len(target.KBIDs) == 0 {
		return "", scope.ErrScopeRequired()
	}
	if len(target.KBIDs) > 1 {
		return "", scope.ErrCrossScope("kb_id/kb_ids")
	}
	return target.KBIDs[0], nil
}

// resolveJobKBScope 归一化任务创建的 kb 作用域（契约 §2：header/query/body 多来源
// 一致性检查）。candidates 依次为 outer kb_id、payload kb_id、header、query。
func resolveJobKBScope(candidates ...string) (string, *scope.Error) {
	normalized := ""
	for _, candidate := range candidates {
		value, err := scope.NormalizeScopeID("kb_id", candidate)
		if err != nil {
			return "", err
		}
		if value == "" {
			continue
		}
		if normalized != "" && normalized != value {
			return "", scope.ErrCrossScope("kb_id")
		}
		if normalized == "" {
			normalized = value
		}
	}
	if normalized == "" {
		return "", scope.ErrScopeRequired()
	}
	return normalized, nil
}

// ensureKBDocumentRoot 确认 KB 存储根目录（DocumentStoragePath / storage_prefix）存在。
func ensureKBDocumentRoot(cfg config.Config, kb adminstore.KnowledgeBaseItem) (string, *scope.Error) {
	root, scopeErr := kbDocumentStorageRoot(cfg, kb)
	if scopeErr != nil {
		return "", scopeErr
	}
	if _, err := ensureDir(root); err != nil {
		return "", documentStorageError("创建知识库存储目录失败")
	}
	return root, nil
}

// kbDocumentStorageRoot 计算物理根目录：DocumentStoragePath / kb.StoragePrefix。
func kbDocumentStorageRoot(cfg config.Config, kb adminstore.KnowledgeBaseItem) (string, *scope.Error) {
	if err := validateStoragePrefix(kb.StoragePrefix); err != nil {
		return "", err
	}
	root := filepath.Clean(filepath.Join(filepath.Clean(strings.TrimSpace(cfg.DocumentStoragePath)), filepath.FromSlash(kb.StoragePrefix)))
	return root, nil
}

// resolveDocumentPhysicalPath 解析文档物理路径：
// DocumentStoragePath / kb.StoragePrefix / relative_path，并做严格逃逸校验
// （与 backend/services/document_registry.py resolve_document_file_path 同规则）。
func resolveDocumentPhysicalPath(cfg config.Config, kb adminstore.KnowledgeBaseItem, relativePath string) (string, *scope.Error) {
	root, scopeErr := kbDocumentStorageRoot(cfg, kb)
	if scopeErr != nil {
		return "", scopeErr
	}
	if err := validateDocumentRelativePath(relativePath); err != nil {
		return "", err
	}
	target := filepath.Clean(filepath.Join(root, filepath.FromSlash(strings.TrimSpace(relativePath))))
	rel, relErr := filepath.Rel(root, target)
	if relErr != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", documentStorageError("文档路径越界")
	}
	return target, nil
}

// validateStoragePrefix 与 validateDocumentRelativePath 使用同一套路径安全规则
// （storage_prefix 由 M2 创建："{tenant_id}/{project_id}/{kb_id}"）。
func validateStoragePrefix(value string) *scope.Error {
	if err := validateRelativeSegments(value); err != nil {
		return documentStorageError("知识库存储前缀非法")
	}
	return nil
}

func validateDocumentRelativePath(value string) *scope.Error {
	if err := validateRelativeSegments(value); err != nil {
		return documentStorageError("文档相对路径非法")
	}
	return nil
}

// validateRelativeSegments 是 Python assert_safe_relative 的 Go 镜像：
// 禁止空值、绝对路径、反斜杠、NUL、盘符、"."/".." 片段。
func validateRelativeSegments(value string) error {
	raw := strings.TrimSpace(value)
	if raw == "" {
		return errors.New("empty relative path")
	}
	if strings.HasPrefix(raw, "/") || strings.HasPrefix(raw, "\\") ||
		strings.Contains(raw, "\\") || strings.ContainsRune(raw, '\x00') {
		return errors.New("invalid relative path characters")
	}
	if len(raw) >= 2 && raw[1] == ':' {
		c := raw[0]
		if (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') {
			return errors.New("windows drive letter not allowed")
		}
	}
	cleaned := strings.Trim(raw, "/")
	if cleaned == "" {
		return errors.New("empty relative path")
	}
	for _, part := range strings.Split(cleaned, "/") {
		if part == "" || part == "." || part == ".." {
			return errors.New("path escape not allowed")
		}
	}
	return nil
}

// sanitizeDocumentRelativeName 将上传文件名规整为 storage_prefix 下的单段相对路径名：
// 去掉任何目录成分（含 Windows 反斜杠），拒绝空名/NUL。
func sanitizeDocumentRelativeName(name string) string {
	trimmed := strings.ReplaceAll(strings.TrimSpace(name), "\\", "/")
	if idx := strings.LastIndex(trimmed, "/"); idx >= 0 {
		trimmed = trimmed[idx+1:]
	}
	trimmed = strings.TrimSpace(trimmed)
	if trimmed == "" || trimmed == "." || trimmed == ".." || strings.ContainsRune(trimmed, '\x00') {
		return ""
	}
	return trimmed
}

func documentStorageError(message string) *scope.Error {
	return &scope.Error{
		Code:    scope.CodeStoragePathInvalid,
		Message: message,
		Status:  http.StatusBadRequest,
	}
}
