package httpserver

// 业务面 KB 目录（M4-R1 步骤 3，前端调用方迁移）：
//
//	GET /api/knowledge-bases — 返回"当前用户可访问的 active 知识库"，供 workspace
//	KB 选择器填充可选集合。
//
// 门控设计（与 kb-scoped 资源路由的两阶段鉴权不同）：目录本身没有单一 kb 作用域，
// 不能走 guard.wrap（空 scope 下 tenant/project 绑定会在第一阶段 CheckPermission 被
// scope_mismatch 拒绝，项目级授权用户永远进不来）。语义上"有任一作用域的 graph:read
// 绑定即可看到该作用域下的 KB"，因此门控收敛为：
//  1. 必须携带合法 JWT（401 INVALID_TOKEN / TOKEN_EXPIRED 与 guard 口径一致）；
//  2. 授权集合由 AuthorizedKBIDs(subject, "graph:read") 解析（global→全量哨兵、
//     tenant/project→目录反查、kb→显式集合、无任何 graph:read 绑定→空集合）；
//  3. 按已解析集合取 active KB 行；集合为空则 items 为空——不放大授权。
// 本端点绝不返回未授权 KB，也不接受调用方传入的 tenant/project/kb 过滤参数。

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/authz"
)

const workspaceKnowledgeBasesRoute = "/api/knowledge-bases"

// workspaceKBStore 是 KB 业务目录所需的最小存储接口；由 adminstore.Client 实现。
// 独立定义避免扩大 adminKBStore（控制面 CRUD）既有测试 fake 的契约面。
type workspaceKBStore interface {
	AuthorizedKBIDs(ctx context.Context, subject string, permissions ...string) ([]string, bool, error)
	ListAuthorizedKnowledgeBases(ctx context.Context, kbIDs []string, allKBs bool) ([]adminstore.KnowledgeBaseItem, error)
}

func asWorkspaceKBStore(store interface{}) workspaceKBStore {
	typed, _ := store.(workspaceKBStore)
	return typed
}

// 编译期保证 adminstore.Client 满足业务目录存储接口。
var _ workspaceKBStore = (*adminstore.Client)(nil)

type workspaceKnowledgeBaseItem struct {
	KBID      string `json:"kb_id"`
	Name      string `json:"name"`
	TenantID  string `json:"tenant_id"`
	ProjectID string `json:"project_id"`
	Status    string `json:"status"`
}

type workspaceKnowledgeBasesData struct {
	Items []workspaceKnowledgeBaseItem `json:"items"`
}

// buildWorkspaceKnowledgeBasesHandler GET /api/knowledge-bases（业务面，graph:read 集合）。
// graphReadPermission 与 guard 第一阶段使用的权限码一致，便于审计与后续口径调整。
func buildWorkspaceKnowledgeBasesHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore workspaceKBStore,
) http.HandlerFunc {
	const graphReadPermission = "graph:read"
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		token, hasToken := extractBearerToken(r.Header.Get("Authorization"))
		if !hasToken {
			w.Header().Set("WWW-Authenticate", "Bearer")
			WriteJSON(w, http.StatusUnauthorized, "缺少认证凭证", map[string]interface{}{
				"error_code": "UNAUTHORIZED",
			})
			return
		}
		claims, err := newAdminJWTVerifier(guard.cfg.AdminSecretKey).verify(token)
		if err != nil {
			w.Header().Set("WWW-Authenticate", "Bearer")
			errorCode := "INVALID_TOKEN"
			if errors.Is(err, errAdminJWTExpired) {
				errorCode = "TOKEN_EXPIRED"
			}
			WriteJSON(w, http.StatusUnauthorized, "Token 已过期或无效", map[string]interface{}{
				"error_code": errorCode,
			})
			return
		}
		if kbStore == nil {
			logger.Error("workspace kb catalog store unavailable", "route", workspaceKnowledgeBasesRoute)
			WriteJSON(w, http.StatusServiceUnavailable, "知识库目录服务不可用", map[string]interface{}{
				"error_code": "KB_CATALOG_UNAVAILABLE",
			})
			return
		}
		kbIDs, allKBs, err := kbStore.AuthorizedKBIDs(r.Context(), claims.Subject, graphReadPermission)
		if err != nil {
			if errors.Is(err, authz.ErrUnauthorized) {
				WriteJSON(w, http.StatusUnauthorized, "Token 已过期或无效", map[string]interface{}{
					"error_code": "INVALID_TOKEN",
				})
				return
			}
			logger.Error("workspace kb catalog authorization failed",
				"route", workspaceKnowledgeBasesRoute,
				"permission", graphReadPermission,
				"error", err.Error(),
			)
			WriteJSON(w, http.StatusServiceUnavailable, "授权服务不可用", map[string]interface{}{
				"error_code": "AUTHZ_UNAVAILABLE",
			})
			return
		}
		items, err := kbStore.ListAuthorizedKnowledgeBases(r.Context(), kbIDs, allKBs)
		if err != nil {
			logger.Error("workspace kb catalog list failed",
				"route", workspaceKnowledgeBasesRoute,
				"all_kbs", allKBs,
				"error", err.Error(),
			)
			WriteJSON(w, http.StatusServiceUnavailable, "知识库目录不可用", map[string]interface{}{
				"error_code": "KB_CATALOG_UNAVAILABLE",
			})
			return
		}
		data := workspaceKnowledgeBasesData{Items: make([]workspaceKnowledgeBaseItem, 0, len(items))}
		for _, item := range items {
			data.Items = append(data.Items, workspaceKnowledgeBaseItem{
				KBID:      item.ID,
				Name:      item.Name,
				TenantID:  item.TenantID,
				ProjectID: item.ProjectID,
				Status:    item.Status,
			})
		}
		WriteJSON(w, http.StatusOK, "ok", data)
	})
}
