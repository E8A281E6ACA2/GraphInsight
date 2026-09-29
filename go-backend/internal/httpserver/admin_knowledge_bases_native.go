package httpserver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"unicode/utf8"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/scope"
)

// adminKBStore 是知识库控制面存储接口；由 adminstore.Client 实现（M2 交付）。
type adminKBStore interface {
	ListKnowledgeBases(ctx context.Context, query adminstore.KBListQuery) (adminstore.KBListResult, error)
	GetKnowledgeBase(ctx context.Context, kbID string) (adminstore.KnowledgeBaseItem, error)
	CreateKnowledgeBase(ctx context.Context, req adminstore.KBCreateRequest) (adminstore.KnowledgeBaseItem, error)
	UpdateKnowledgeBase(ctx context.Context, req adminstore.KBUpdateRequest) (adminstore.KnowledgeBaseItem, error)
	ArchiveKnowledgeBase(ctx context.Context, req adminstore.KBArchiveRequest) (adminstore.KnowledgeBaseItem, error)
	DeleteKnowledgeBase(ctx context.Context, req adminstore.KBDeleteRequest) (adminstore.KnowledgeBaseItem, error)
}

func asAdminKBStore(store interface{}) adminKBStore {
	typed, _ := store.(adminKBStore)
	return typed
}

// 编译期保证 adminstore.Client 满足知识库存储接口（与 adminJobStore/adminLogStore 相同的窄接口链）。
var _ adminKBStore = (*adminstore.Client)(nil)

const adminKnowledgeBasesRoute = "/api/v1/admin/knowledge-bases"

type adminKBCreatePayload struct {
	TenantID         *string                `json:"tenant_id"`
	ProjectID        *string                `json:"project_id"`
	Name             string                 `json:"name"`
	Slug             *string                `json:"slug"`
	Description      *string                `json:"description"`
	ParserProfile    map[string]interface{} `json:"parser_profile"`
	RetrievalProfile map[string]interface{} `json:"retrieval_profile"`
}

type adminKBPatchPayload struct {
	Name             *string                 `json:"name"`
	Slug             *string                 `json:"slug"`
	Description      *string                 `json:"description"`
	Status           *string                 `json:"status"`
	ParserProfile    *map[string]interface{} `json:"parser_profile"`
	RetrievalProfile *map[string]interface{} `json:"retrieval_profile"`
}

// buildAdminKnowledgeBasesHandler 路由：
//
//	GET/POST /api/v1/admin/knowledge-bases           — 父作用域（tenant/project）目录操作
//	GET/PATCH/DELETE /api/v1/admin/knowledge-bases/{kb_id}
//	POST /api/v1/admin/knowledge-bases/{kb_id}/archive — kb 作用域资源操作
func buildAdminKnowledgeBasesHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	logStore adminLogStore,
) http.HandlerFunc {
	listHandler := buildAdminKBListNativeHandler(logger, guard, kbStore)
	createHandler := buildAdminKBCreateNativeHandler(logger, guard, kbStore, logStore)
	detailHandler := buildAdminKBDetailNativeHandler(logger, guard, kbStore, logStore)
	patchHandler := buildAdminKBPatchNativeHandler(logger, guard, kbStore, logStore)
	archiveHandler := buildAdminKBArchiveNativeHandler(logger, guard, kbStore, logStore)
	deleteHandler := buildAdminKBDeleteNativeHandler(logger, guard, kbStore, logStore)
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == adminKnowledgeBasesRoute:
			switch r.Method {
			case http.MethodGet:
				listHandler(w, r)
			case http.MethodPost:
				createHandler(w, r)
			default:
				WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			}
		case strings.HasPrefix(r.URL.Path, adminKnowledgeBasesRoute+"/"):
			rest := strings.TrimPrefix(r.URL.Path, adminKnowledgeBasesRoute+"/")
			rest = strings.TrimSuffix(rest, "/")
			if rest == "" {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if strings.HasSuffix(rest, "/archive") {
				if r.Method != http.MethodPost {
					WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
					return
				}
				archiveHandler(w, r)
				return
			}
			if strings.Contains(rest, "/") {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			switch r.Method {
			case http.MethodGet:
				detailHandler(w, r)
			case http.MethodPatch:
				patchHandler(w, r)
			case http.MethodDelete:
				deleteHandler(w, r)
			default:
				WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			}
		default:
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	})
}

// buildAdminKBListNativeHandler GET /api/v1/admin/knowledge-bases（kb:read）。
// 契约 §3.2 结构性例外：目录列表以已授权的 tenant/project 父作用域查询；
// 缺失父作用域返回 KB_SCOPE_REQUIRED，不是全局列表，也没有 default 兜底。
// 授权顺序（FIX #1）：先解析三来源有效父作用域，再以该作用域执行 CheckPermission，
// 授权必须评估在查询实际指向的父作用域上。
func buildAdminKBListNativeHandler(logger *slog.Logger, guard businessPermissionGuard, kbStore adminKBStore) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if kbStore == nil {
			logger.Error("admin knowledge base store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		tenantID, projectID, scopeErr := normalizeKBParentScope(r, "", "")
		if scopeErr != nil {
			writeScopeError(w, scopeErr)
			return
		}
		if !guard.allowRequestWithScope(w, r, "kb:read", kbParentScopeMap(tenantID, projectID)) {
			return
		}
		query := adminstore.KBListQuery{
			TenantID:  tenantID,
			ProjectID: projectID,
			Status:    strings.TrimSpace(r.URL.Query().Get("status")),
			Page:      boundedIntQuery(r, "page", 1, 1, 1_000_000),
			PageSize:  boundedIntQuery(r, "page_size", 20, 1, 200),
		}
		result, err := kbStore.ListKnowledgeBases(r.Context(), query)
		if errors.Is(err, adminstore.ErrKBValidation) {
			WriteJSON(w, http.StatusBadRequest, "status 过滤值非法", map[string]string{"error_code": "INVALID_QUERY"})
			return
		}
		if errors.Is(err, adminstore.ErrKBParentScopeRequired) {
			writeScopeError(w, kbParentScopeRequiredError())
			return
		}
		if err != nil {
			logger.Error("list knowledge bases failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "查询知识库列表失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		totalPages := 0
		if query.PageSize > 0 && result.Total > 0 {
			totalPages = (result.Total + query.PageSize - 1) / query.PageSize
		}
		WriteJSON(w, http.StatusOK, "获取成功", adminPaginatedData{
			Items:      result.Items,
			Total:      result.Total,
			Page:       query.Page,
			PageSize:   query.PageSize,
			TotalPages: totalPages,
		})
	})
}

// buildAdminKBCreateNativeHandler POST /api/v1/admin/knowledge-bases（kb:write）。
// 授权顺序（FIX #1，契约 §3.2）：先解码 body 并解析 header/query/body 三来源的
// 有效父作用域（不一致 → KB_CROSS_SCOPE，缺失 → KB_SCOPE_REQUIRED），随后以该
// 有效父作用域执行 CheckPermission——授权必须评估在写入实际目标的作用域上，
// 防止调用方借 header 作用域过闸、body 携带未授权父作用域走私写入。
func buildAdminKBCreateNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	logStore adminLogStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if kbStore == nil {
			logger.Error("admin knowledge base store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		var payload adminKBCreatePayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
			return
		}
		bodyTenantID := ""
		if payload.TenantID != nil {
			bodyTenantID = *payload.TenantID
		}
		bodyProjectID := ""
		if payload.ProjectID != nil {
			bodyProjectID = *payload.ProjectID
		}
		tenantID, projectID, scopeErr := normalizeKBParentScope(r, bodyTenantID, bodyProjectID)
		if scopeErr != nil {
			writeScopeError(w, scopeErr)
			return
		}
		if !guard.allowRequestWithScope(w, r, "kb:write", kbParentScopeMap(tenantID, projectID)) {
			return
		}
		req, ok := buildKBCreateRequest(w, r, tenantID, projectID, payload)
		if !ok {
			return
		}
		item, err := kbStore.CreateKnowledgeBase(r.Context(), req)
		if errors.Is(err, adminstore.ErrKBDuplicateName) {
			WriteJSON(w, http.StatusConflict, "同名知识库已存在", map[string]string{"error_code": scope.CodeDuplicateName})
			return
		}
		if errors.Is(err, adminstore.ErrKBValidation) {
			WriteJSON(w, http.StatusBadRequest, "知识库参数错误", map[string]string{"error_code": "INVALID_BODY"})
			return
		}
		if err != nil {
			logger.Error("create knowledge base failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "创建知识库失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		recordKBAudit(r, logger, logStore, "kb_created", item, map[string]interface{}{
			"name":           item.Name,
			"status":         item.Status,
			"storage_prefix": item.StoragePrefix,
		})
		WriteJSON(w, http.StatusCreated, "知识库已创建", item)
	})
}

func buildKBCreateRequest(w http.ResponseWriter, r *http.Request, tenantID string, projectID string, payload adminKBCreatePayload) (adminstore.KBCreateRequest, bool) {
	name := strings.TrimSpace(payload.Name)
	if name == "" || utf8.RuneCountInString(name) > 200 {
		WriteJSON(w, http.StatusBadRequest, "名称长度必须为 1-200 个字符", map[string]string{"error_code": "INVALID_BODY"})
		return adminstore.KBCreateRequest{}, false
	}
	var slug *string
	if payload.Slug != nil {
		trimmed := strings.TrimSpace(*payload.Slug)
		if utf8.RuneCountInString(trimmed) > 100 {
			WriteJSON(w, http.StatusBadRequest, "slug 长度不能超过 100 个字符", map[string]string{"error_code": "INVALID_BODY"})
			return adminstore.KBCreateRequest{}, false
		}
		if trimmed != "" {
			slug = &trimmed
		}
	}
	var description *string
	if payload.Description != nil {
		trimmed := strings.TrimSpace(*payload.Description)
		if trimmed != "" {
			description = &trimmed
		}
	}
	kbID := scope.NewUUID()
	return adminstore.KBCreateRequest{
		ID:               kbID,
		TenantID:         tenantID,
		ProjectID:        projectID,
		Name:             name,
		Slug:             slug,
		Description:      description,
		StoragePrefix:    fmt.Sprintf("%s/%s/%s", tenantID, projectID, kbID),
		ParserProfile:    payload.ParserProfile,
		RetrievalProfile: payload.RetrievalProfile,
		CreatedBy:        optionalIntHeader(r, "x-auth-user-id"),
	}, true
}

// buildAdminKBDetailNativeHandler GET /api/v1/admin/knowledge-bases/{kb_id}（kb:read）。
// 先加载 KB（不存在返回 KB_NOT_FOUND），再携带 {tenant, project, kb} 完整作用域二次鉴权。
func buildAdminKBDetailNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	logStore adminLogStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("kb:read", func(w http.ResponseWriter, r *http.Request) {
		if kbStore == nil {
			logger.Error("admin knowledge base store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		item, ok := loadAuthorizedKnowledgeBase(w, r, logger, guard, kbStore, logStore, "kb:read")
		if !ok {
			return
		}
		WriteJSON(w, http.StatusOK, "获取成功", item)
	}))
}

// buildAdminKBPatchNativeHandler PATCH /api/v1/admin/knowledge-bases/{kb_id}（kb:write）。
// 归档库仅允许恢复（status="active"）；其余写请求返回 KB_ARCHIVED。
func buildAdminKBPatchNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	logStore adminLogStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("kb:write", func(w http.ResponseWriter, r *http.Request) {
		if kbStore == nil {
			logger.Error("admin knowledge base store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		current, ok := loadAuthorizedKnowledgeBase(w, r, logger, guard, kbStore, logStore, "kb:write")
		if !ok {
			return
		}
		var payload adminKBPatchPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
			return
		}
		req, ok := buildKBUpdateRequest(w, r, current, payload)
		if !ok {
			return
		}
		item, err := kbStore.UpdateKnowledgeBase(r.Context(), req)
		if !writeKBMutationError(w, logger, err) {
			return
		}
		recordKBAudit(r, logger, logStore, "kb_updated", item, map[string]interface{}{
			"updated_fields":  kbUpdatedFields(payload),
			"previous_status": current.Status,
		})
		WriteJSON(w, http.StatusOK, "知识库已更新", item)
	}))
}

// buildAdminKBArchiveNativeHandler POST /api/v1/admin/knowledge-bases/{kb_id}/archive（kb:delete）。
func buildAdminKBArchiveNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	logStore adminLogStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("kb:delete", func(w http.ResponseWriter, r *http.Request) {
		if kbStore == nil {
			logger.Error("admin knowledge base store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		if _, ok := loadAuthorizedKnowledgeBase(w, r, logger, guard, kbStore, logStore, "kb:delete"); !ok {
			return
		}
		item, err := kbStore.ArchiveKnowledgeBase(r.Context(), adminstore.KBArchiveRequest{
			KBID:      parseKBResourceID(r.URL.Path),
			UpdatedBy: optionalIntHeader(r, "x-auth-user-id"),
		})
		if !writeKBMutationError(w, logger, err) {
			return
		}
		recordKBAudit(r, logger, logStore, "kb_archived", item, map[string]interface{}{
			"status": item.Status,
		})
		WriteJSON(w, http.StatusOK, "知识库已归档", item)
	}))
}

// buildAdminKBDeleteNativeHandler DELETE /api/v1/admin/knowledge-bases/{kb_id}（kb:delete）。
// M2 仅做软删除（status='deleting'），不执行任何文件/图谱/向量数据删除；重复删除幂等成功。
func buildAdminKBDeleteNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	logStore adminLogStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("kb:delete", func(w http.ResponseWriter, r *http.Request) {
		if kbStore == nil {
			logger.Error("admin knowledge base store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		if _, ok := loadAuthorizedKnowledgeBase(w, r, logger, guard, kbStore, logStore, "kb:delete"); !ok {
			return
		}
		item, err := kbStore.DeleteKnowledgeBase(r.Context(), adminstore.KBDeleteRequest{
			KBID:      parseKBResourceID(r.URL.Path),
			UpdatedBy: optionalIntHeader(r, "x-auth-user-id"),
		})
		if !writeKBMutationError(w, logger, err) {
			return
		}
		recordKBAudit(r, logger, logStore, "kb_deleted", item, map[string]interface{}{
			"soft_delete": true,
			"status":      item.Status,
		})
		WriteJSON(w, http.StatusOK, "知识库已标记删除，数据清理由任务中心异步执行", item)
	}))
}

// loadAuthorizedKnowledgeBase 是 kb-scoped 路由的公共前置：
//  1. 解析路径 kb_id（非法路径 404）。
//  2. 加载 KB（不存在返回 404 KB_NOT_FOUND）。
//  3. 请求自带 scope 与资源不一致时返回 KB_CROSS_SCOPE / SCOPE_INVALID（契约 §3.2）。
//  4. 携带 {tenant, project, kb} 完整作用域执行第二阶段权限检查，拒绝返回 403 KB_ACCESS_DENIED。
func loadAuthorizedKnowledgeBase(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	logStore adminLogStore,
	permission string,
) (adminstore.KnowledgeBaseItem, bool) {
	kbID := parseKBResourceID(r.URL.Path)
	if kbID == "" {
		WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
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
	if scopeErr := ensureKBRequestScopeMatches(r, item); scopeErr != nil {
		writeScopeError(w, scopeErr)
		return adminstore.KnowledgeBaseItem{}, false
	}
	if !guard.checkPermissionWithScope(r, permission, kbScopeMap(item)) {
		writeKBAccessDenied(w, r, logger, logStore, permission, item)
		return adminstore.KnowledgeBaseItem{}, false
	}
	return item, true
}

func buildKBUpdateRequest(w http.ResponseWriter, r *http.Request, current adminstore.KnowledgeBaseItem, payload adminKBPatchPayload) (adminstore.KBUpdateRequest, bool) {
	req := adminstore.KBUpdateRequest{
		KBID:      current.ID,
		UpdatedBy: optionalIntHeader(r, "x-auth-user-id"),
	}
	if payload.Name != nil {
		name := strings.TrimSpace(*payload.Name)
		if name == "" || utf8.RuneCountInString(name) > 200 {
			WriteJSON(w, http.StatusBadRequest, "名称长度必须为 1-200 个字符", map[string]string{"error_code": "INVALID_BODY"})
			return adminstore.KBUpdateRequest{}, false
		}
		req.Name = &name
	}
	if payload.Slug != nil {
		slug := strings.TrimSpace(*payload.Slug)
		if utf8.RuneCountInString(slug) > 100 {
			WriteJSON(w, http.StatusBadRequest, "slug 长度不能超过 100 个字符", map[string]string{"error_code": "INVALID_BODY"})
			return adminstore.KBUpdateRequest{}, false
		}
		req.Slug = &slug
	}
	if payload.Description != nil {
		description := strings.TrimSpace(*payload.Description)
		req.Description = &description
	}
	if payload.ParserProfile != nil {
		req.ParserProfile = payload.ParserProfile
	}
	if payload.RetrievalProfile != nil {
		req.RetrievalProfile = payload.RetrievalProfile
	}
	if payload.Status != nil {
		status := strings.TrimSpace(*payload.Status)
		if status != adminstore.KBStatusActive && status != adminstore.KBStatusArchived {
			WriteJSON(w, http.StatusBadRequest, "status 仅允许 active/archived", map[string]string{"error_code": "INVALID_BODY"})
			return adminstore.KBUpdateRequest{}, false
		}
		req.Status = &status
	}
	return req, true
}

func writeKBMutationError(w http.ResponseWriter, logger *slog.Logger, err error) bool {
	if err == nil {
		return true
	}
	if errors.Is(err, adminstore.ErrKBNotFound) {
		WriteJSON(w, http.StatusNotFound, "知识库不存在", map[string]string{"error_code": scope.CodeKBNotFound})
		return false
	}
	if errors.Is(err, adminstore.ErrKBDuplicateName) {
		WriteJSON(w, http.StatusConflict, "同名知识库已存在", map[string]string{"error_code": scope.CodeDuplicateName})
		return false
	}
	if errors.Is(err, adminstore.ErrKBArchived) {
		WriteJSON(w, http.StatusConflict, "知识库已归档，禁止写入", map[string]string{"error_code": scope.CodeArchived})
		return false
	}
	if errors.Is(err, adminstore.ErrKBInvalidState) {
		WriteJSON(w, http.StatusConflict, "知识库当前状态不支持该操作", map[string]string{"error_code": "KB_INVALID_STATE"})
		return false
	}
	if errors.Is(err, adminstore.ErrKBValidation) {
		WriteJSON(w, http.StatusBadRequest, "知识库参数错误", map[string]string{"error_code": "INVALID_BODY"})
		return false
	}
	logger.Error("knowledge base mutation failed", "error", err.Error())
	WriteJSON(w, http.StatusServiceUnavailable, "知识库操作失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
	return false
}

// parseKBResourceID 解析 /api/v1/admin/knowledge-bases/{kb_id} 中的 kb_id。
// 动作子路由（M2 仅 /archive）先剥离动作后缀；含其他子路径或为空时返回空串。
func parseKBResourceID(path string) string {
	rest := strings.TrimPrefix(path, adminKnowledgeBasesRoute+"/")
	rest = strings.TrimSuffix(rest, "/")
	rest = strings.TrimSuffix(rest, "/archive")
	if rest == "" || strings.Contains(rest, "/") {
		return ""
	}
	return rest
}

// normalizeKBParentScope 归一化父作用域（header x-*-id / query / body 三来源，
// trim + 小写 + 格式校验；非空来源必须一致）。list/create 的结构性例外（契约 §3.2）：
// 资源本身尚无 kb_id，以已授权 tenant/project 为父作用域；缺失返回 KB_SCOPE_REQUIRED。
func normalizeKBParentScope(r *http.Request, bodyTenantID string, bodyProjectID string) (string, string, *scope.Error) {
	query := r.URL.Query()
	return resolveKBParentScopeSources(
		r.Header.Get("x-tenant-id"),
		r.Header.Get("x-project-id"),
		query.Get("tenant_id"),
		query.Get("project_id"),
		bodyTenantID,
		bodyProjectID,
	)
}

// resolveKBParentScopeSources 是 normalizeKBParentScope 的纯函数内核（可单测）：
// header/query/body 三来源严格一致合并。任何来源不一致 → KB_CROSS_SCOPE；
// 三来源全空 → KB_SCOPE_REQUIRED（kbParentScopeRequiredError）。
func resolveKBParentScopeSources(headerTenantID, headerProjectID, queryTenantID, queryProjectID, bodyTenantID, bodyProjectID string) (string, string, *scope.Error) {
	tenantID, err := mergeKBParentScopeValue("tenant_id", []string{
		headerTenantID,
		queryTenantID,
		bodyTenantID,
	})
	if err != nil {
		return "", "", err
	}
	projectID, err := mergeKBParentScopeValue("project_id", []string{
		headerProjectID,
		queryProjectID,
		bodyProjectID,
	})
	if err != nil {
		return "", "", err
	}
	return tenantID, projectID, nil
}

// kbParentScopeMap 把有效父作用域映射为 CheckPermission 的 scope 键
// （授权必须评估在该作用域上，FIX #1）。
func kbParentScopeMap(tenantID string, projectID string) map[string]string {
	return map[string]string{
		"x-tenant-id":  tenantID,
		"x-project-id": projectID,
	}
}

func mergeKBParentScopeValue(kind string, candidates []string) (string, *scope.Error) {
	normalized := ""
	for _, candidate := range candidates {
		value, err := scope.NormalizeScopeID(kind, candidate)
		if err != nil {
			return "", err
		}
		if value == "" {
			continue
		}
		if normalized != "" && normalized != value {
			return "", scope.ErrCrossScope(kind)
		}
		if normalized == "" {
			normalized = value
		}
	}
	if normalized == "" {
		return "", kbParentScopeRequiredError()
	}
	return normalized, nil
}

func kbParentScopeRequiredError() *scope.Error {
	return &scope.Error{
		Code:    scope.CodeScopeRequired,
		Message: "知识库目录操作缺少父作用域 tenant_id/project_id（header x-tenant-id/x-project-id、query 或 body）",
		Status:  http.StatusBadRequest,
	}
}

// ensureKBRequestScopeMatches 请求自带 scope（header/query）与目标 KB 不一致 → KB_CROSS_SCOPE（契约 §3.2）。
func ensureKBRequestScopeMatches(r *http.Request, item adminstore.KnowledgeBaseItem) *scope.Error {
	query := r.URL.Query()
	checks := []struct {
		kind     string
		value    string
		expected string
	}{
		{"tenant_id", firstNonEmpty(r.Header.Get("x-tenant-id"), query.Get("tenant_id")), item.TenantID},
		{"project_id", firstNonEmpty(r.Header.Get("x-project-id"), query.Get("project_id")), item.ProjectID},
		{"kb_id", firstNonEmpty(r.Header.Get("x-kb-id"), query.Get("kb_id")), item.ID},
	}
	for _, check := range checks {
		normalized, err := scope.NormalizeScopeID(check.kind, check.value)
		if err != nil {
			return err
		}
		if normalized == "" {
			continue
		}
		if normalized != check.expected {
			return scope.ErrCrossScope(check.kind)
		}
	}
	return nil
}

func kbScopeMap(item adminstore.KnowledgeBaseItem) map[string]string {
	return map[string]string{
		"x-tenant-id":  item.TenantID,
		"x-project-id": item.ProjectID,
		"x-kb-id":      item.ID,
	}
}

func writeScopeError(w http.ResponseWriter, scopeErr *scope.Error) {
	if scopeErr == nil {
		return
	}
	WriteJSON(w, scopeErr.Status, scopeErr.Message, map[string]string{"error_code": scopeErr.Code})
}

// writeKBAccessDenied 第二阶段鉴权拒绝（契约 §2.10：拒绝路径同样写审计）。
func writeKBAccessDenied(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	logStore adminLogStore,
	permission string,
	item adminstore.KnowledgeBaseItem,
) {
	if logStore != nil {
		if err := logStore.RecordBusinessAudit(r.Context(), adminstore.BusinessAuditRequest{
			OperatorID: optionalIntHeader(r, "x-auth-user-id"),
			TenantID:   &item.TenantID,
			ProjectID:  &item.ProjectID,
			KBID:       &item.ID,
			TraceID:    optionalStringHeader(r, traceHeader),
			Action:     "authz_denied",
			Resource:   "knowledge_base",
			ResourceID: &item.ID,
			Details: map[string]interface{}{
				"permission": permission,
				"error_code": scope.CodeAccessDenied,
			},
			Status: "denied",
		}); err != nil {
			logger.Warn("record knowledge base access denied audit failed", "error", err.Error())
		}
	}
	writeScopeError(w, scope.ErrAccessDenied(nil))
}

func recordKBAudit(
	r *http.Request,
	logger *slog.Logger,
	logStore adminLogStore,
	action string,
	item adminstore.KnowledgeBaseItem,
	details map[string]interface{},
) {
	if logStore == nil {
		return
	}
	if err := logStore.RecordBusinessAudit(r.Context(), adminstore.BusinessAuditRequest{
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TenantID:   &item.TenantID,
		ProjectID:  &item.ProjectID,
		KBID:       &item.ID,
		TraceID:    optionalStringHeader(r, traceHeader),
		Action:     action,
		Resource:   "knowledge_base",
		ResourceID: &item.ID,
		Details:    details,
		IPAddress:  optionalString(firstRemoteAddr(r)),
		UserAgent:  optionalString(r.UserAgent()),
		Status:     "success",
	}); err != nil {
		logger.Warn("record knowledge base audit failed", "action", action, "error", err.Error())
	}
}

func kbUpdatedFields(payload adminKBPatchPayload) []string {
	fields := []string{}
	if payload.Name != nil {
		fields = append(fields, "name")
	}
	if payload.Slug != nil {
		fields = append(fields, "slug")
	}
	if payload.Description != nil {
		fields = append(fields, "description")
	}
	if payload.Status != nil {
		fields = append(fields, "status")
	}
	if payload.ParserProfile != nil {
		fields = append(fields, "parser_profile")
	}
	if payload.RetrievalProfile != nil {
		fields = append(fields, "retrieval_profile")
	}
	return fields
}
