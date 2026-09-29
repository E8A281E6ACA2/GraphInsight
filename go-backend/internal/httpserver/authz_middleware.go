package httpserver

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
)

type businessPermissionGuard struct {
	cfg        config.Config
	logger     *slog.Logger
	adminStore adminPermissionStore
}

type adminPermissionStore interface {
	CheckPermission(ctx context.Context, subject string, permission string, scope map[string]string) (authz.CheckResult, error)
}

// businessAuditWriter 由 adminstore.Client 实现；拒绝路径审计（契约 §2.10）走这里。
type businessAuditWriter interface {
	RecordBusinessAudit(ctx context.Context, req adminstore.BusinessAuditRequest) error
}

type adminNativeStore interface {
	adminPermissionStore
	adminConfigStore
	adminRbacBindingStore
	adminUserStore
	adminProfileStore
	adminProfileStatsStore
}

func newBusinessPermissionGuard(cfg config.Config, logger *slog.Logger, adminStoreOpt ...adminPermissionStore) businessPermissionGuard {
	var adminStore adminPermissionStore
	if len(adminStoreOpt) > 0 {
		adminStore = adminStoreOpt[0]
	}
	return businessPermissionGuard{
		cfg:        cfg,
		logger:     logger,
		adminStore: adminStore,
	}
}

func (g businessPermissionGuard) wrap(permission string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !g.allowRequest(w, r, permission) {
			return
		}
		next.ServeHTTP(w, r)
	}
}

// checkPermissionWithScope 是 kb-scoped 资源路由的第二阶段授权（契约 §14.2、§3.2）：
// handler 加载资源后，携带资源实际作用域 {tenant, project, kb} 重新求交，
// 防止仅有父作用域绑定的调用方通过 URL 触达未授权 KB。
//
// KB 作用域是安全边界，第二阶段授权独立 fail-closed（M4-R1 审计 P0）：
// 一旦能做出授权判定（存在已认证主体且授权服务可用），跨 KB 拒绝必须生效，
// 不受 RBACEnforceBusinessAPI=false 或 local_jwt_soft 的 soft allow 影响。
// 仅在“无法归因主体”（未认证，第一阶段已按 soft 放行）或“授权服务未接入”
// （纯本地无库开发）时按第一阶段的既有语义处理，不改变跨 KB 的判定口径。
func (g businessPermissionGuard) checkPermissionWithScope(r *http.Request, permission string, scopeMap map[string]string) bool {
	subject := strings.TrimSpace(r.Header.Get("x-auth-user-name"))
	if subject == "" {
		// 未认证：无法归因具体主体，与第一阶段 allowRequest 的无 token 语义保持一致。
		return true
	}
	if g.adminStore == nil {
		// 授权服务未接入：启用强制时 fail-closed；纯本地无库软模式保持第一阶段放行。
		if g.cfg.RBACEnforceBusinessAPI {
			g.logger.Error("admin store unavailable for scoped permission check", "permission", permission)
			return false
		}
		return true
	}
	result, err := g.adminStore.CheckPermission(r.Context(), subject, permission, scopeMap)
	if err != nil {
		// 无法确证授权：KB 边界 fail-closed，避免降级放行跨 KB 读取。
		g.logger.Error("scoped permission check failed, fail closed", "permission", permission, "error", err.Error())
		return false
	}
	if !result.Allowed {
		// 明确拒绝：无论 enforce/soft 一律生效（KB 独立 fail-closed）。
		g.logger.Warn("scoped permission denied", "permission", permission, "reason", result.Reason)
		return false
	}
	return true
}

func (g businessPermissionGuard) allowRequest(w http.ResponseWriter, r *http.Request, permission string) bool {
	return g.allowRequestWithScope(w, r, permission, resolveScopeHeaders(r))
}

// allowRequestWithScope 是 allowRequest 的显式作用域版本（契约 §3.2：
// "authorize 必须作用在请求实际目标作用域上"）。KB 目录 create/list 的父作用域
// 需要合并 body 来源后才能确定，handler 先解析作用域再调用本方法，保证
// CheckPermission 收到的是写入/查询真正指向的 tenant/project。
// scopeMap 使用 CheckPermission 的 scope 键（x-tenant-id/x-project-id/x-kb-id）。
func (g businessPermissionGuard) allowRequestWithScope(w http.ResponseWriter, r *http.Request, permission string, scopeMap map[string]string) bool {
	token, hasToken := extractBearerToken(r.Header.Get("Authorization"))
	if !hasToken {
		if g.cfg.RBACEnforceBusinessAPI {
			w.Header().Set("WWW-Authenticate", "Bearer")
			WriteJSON(w, http.StatusUnauthorized, "缺少认证凭证", map[string]interface{}{
				"error_code": "UNAUTHORIZED",
			})
			return false
		}
		return true
	}

	if isLocalJWTSoftMode(g.cfg.RBACAuthzMode) {
		return g.allowLocalJWTSoftRequest(w, r, token, permission)
	}
	if strings.EqualFold(strings.TrimSpace(g.cfg.RBACAuthzMode), "go_db") {
		return g.allowGoDBRequest(w, r, token, permission, scopeMap)
	}
	return g.allowGoDBRequest(w, r, token, permission, scopeMap)
}

func (g businessPermissionGuard) propagateAuthzResult(r *http.Request, permission string, result authz.CheckResult) {
	r.Header.Set("x-authz-permission", permission)
	r.Header.Set("x-authz-reason", result.Reason)
	if result.UserID > 0 {
		r.Header.Set("x-auth-user-id", strconv.Itoa(result.UserID))
	}
	if result.User != "" {
		r.Header.Set("x-auth-user-name", result.User)
	}
	if result.Email != "" {
		r.Header.Set("x-auth-user-email", result.Email)
	}
}

func isLocalJWTSoftMode(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "local_jwt_soft", "local_jwt":
		return true
	default:
		return false
	}
}

func (g businessPermissionGuard) allowGoDBRequest(w http.ResponseWriter, r *http.Request, token string, permission string, scopeMap map[string]string) bool {
	claims, err := newAdminJWTVerifier(g.cfg.AdminSecretKey).verify(token)
	if err != nil {
		w.Header().Set("WWW-Authenticate", "Bearer")
		errorCode := "INVALID_TOKEN"
		if errors.Is(err, errAdminJWTExpired) {
			errorCode = "TOKEN_EXPIRED"
		}
		WriteJSON(w, http.StatusUnauthorized, "Token 已过期或无效", map[string]interface{}{
			"error_code": errorCode,
		})
		return false
	}
	if g.adminStore == nil {
		if g.cfg.RBACEnforceBusinessAPI {
			g.logger.Error("admin store unavailable in go_db authz mode", "permission", permission)
			WriteJSON(w, http.StatusServiceUnavailable, "授权服务不可用", map[string]interface{}{
				"error_code": "AUTHZ_UNAVAILABLE",
			})
			return false
		}
		g.logger.Warn("admin store unavailable, soft allow", "permission", permission)
		g.propagateLocalJWTContext(r, claims, permission, "go_db_store_unavailable_soft_allow")
		return true
	}

	result, err := g.adminStore.CheckPermission(r.Context(), claims.Subject, permission, scopeMap)
	if err != nil {
		if errors.Is(err, authz.ErrUnauthorized) {
			w.Header().Set("WWW-Authenticate", "Bearer")
			WriteJSON(w, http.StatusUnauthorized, "Token 已过期或无效", map[string]interface{}{
				"error_code": "INVALID_TOKEN",
			})
			return false
		}
		if g.cfg.RBACEnforceBusinessAPI {
			g.logger.Error("go_db authz check failed", "permission", permission, "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "授权服务不可用", map[string]interface{}{
				"error_code": "AUTHZ_UNAVAILABLE",
			})
			return false
		}
		g.logger.Warn("go_db authz check failed, soft allow", "permission", permission, "error", err.Error())
		g.propagateLocalJWTContext(r, claims, permission, "go_db_error_soft_allow")
		return true
	}
	if !result.Allowed {
		if g.cfg.RBACEnforceBusinessAPI {
			g.auditDeniedWithScope(r, permission, result, false, scopeMap)
			WriteJSON(w, http.StatusForbidden, "权限不足", map[string]interface{}{
				"error_code": "PERMISSION_DENIED",
				"reason":     result.Reason,
			})
			return false
		}
		g.logger.Warn("go_db authz denied, soft allow", "permission", permission, "reason", result.Reason)
		g.auditDeniedWithScope(r, permission, result, true, scopeMap)
	}

	g.propagateAuthzResult(r, permission, result)
	return true
}

func (g businessPermissionGuard) allowLocalJWTSoftRequest(w http.ResponseWriter, r *http.Request, token string, permission string) bool {
	claims, err := newAdminJWTVerifier(g.cfg.AdminSecretKey).verify(token)
	if err != nil {
		w.Header().Set("WWW-Authenticate", "Bearer")
		message := "Token 已过期或无效"
		errorCode := "INVALID_TOKEN"
		if errors.Is(err, errAdminJWTExpired) {
			errorCode = "TOKEN_EXPIRED"
		}
		WriteJSON(w, http.StatusUnauthorized, message, map[string]interface{}{
			"error_code": errorCode,
		})
		return false
	}

	g.propagateLocalJWTContext(r, claims, permission, "local_jwt_soft_allow")
	return true
}

func (g businessPermissionGuard) propagateLocalJWTContext(r *http.Request, claims adminJWTClaims, permission string, reason string) {
	r.Header.Set("x-authz-permission", permission)
	r.Header.Set("x-authz-reason", reason)
	r.Header.Set("x-auth-user-name", claims.Subject)
	if strings.Contains(claims.Subject, "@") {
		r.Header.Set("x-auth-user-email", claims.Subject)
	}
}

func extractBearerToken(value string) (string, bool) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", false
	}
	parts := strings.SplitN(trimmed, " ", 2)
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") {
		return "", false
	}
	token := strings.TrimSpace(parts[1])
	if token == "" {
		return "", false
	}
	return token, true
}

func resolveScopeHeaders(r *http.Request) map[string]string {
	tenantID := strings.TrimSpace(r.Header.Get("x-tenant-id"))
	if tenantID == "" {
		tenantID = strings.TrimSpace(r.URL.Query().Get("tenant_id"))
	}
	projectID := strings.TrimSpace(r.Header.Get("x-project-id"))
	if projectID == "" {
		projectID = strings.TrimSpace(r.URL.Query().Get("project_id"))
	}
	kbID := strings.TrimSpace(r.Header.Get("x-kb-id"))
	if kbID == "" {
		kbID = strings.TrimSpace(r.URL.Query().Get("kb_id"))
	}
	return map[string]string{
		"x-tenant-id":  tenantID,
		"x-project-id": projectID,
		"x-kb-id":      kbID,
	}
}

func scopeStringPtr(value string) *string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return nil
	}
	return &trimmed
}

// auditDenied 在权限拒绝（含 soft allow）路径写审计（契约 §2.10：拒绝路径同样写审计）。
// 审计失败只记日志，不改变原有拒绝响应。
func (g businessPermissionGuard) auditDenied(r *http.Request, permission string, result authz.CheckResult, softAllow bool) {
	g.auditDeniedWithScope(r, permission, result, softAllow, resolveScopeHeaders(r))
}

// auditDeniedWithScope 允许调用方显式指定审计作用域（KB 目录 create/list 的
// 父作用域来自 body 合并结果，不能只看 header/query）。
func (g businessPermissionGuard) auditDeniedWithScope(r *http.Request, permission string, result authz.CheckResult, softAllow bool, scopeHeaders map[string]string) {
	writer, ok := g.adminStore.(businessAuditWriter)
	if !ok {
		return
	}
	if scopeHeaders == nil {
		scopeHeaders = resolveScopeHeaders(r)
	}
	var operatorID *int
	if result.UserID > 0 {
		id := result.UserID
		operatorID = &id
	}
	status := "denied"
	if softAllow {
		status = "denied_soft_allow"
	}
	err := writer.RecordBusinessAudit(r.Context(), adminstore.BusinessAuditRequest{
		OperatorID: operatorID,
		TenantID:   scopeStringPtr(scopeHeaders["x-tenant-id"]),
		ProjectID:  scopeStringPtr(scopeHeaders["x-project-id"]),
		KBID:       scopeStringPtr(scopeHeaders["x-kb-id"]),
		TraceID:    scopeStringPtr(r.Header.Get("X-Trace-Id")),
		Action:     "authz_denied",
		Resource:   "kb_scope",
		ResourceID: scopeStringPtr(scopeHeaders["x-kb-id"]),
		Details: map[string]interface{}{
			"permission": permission,
			"reason":     result.Reason,
		},
		Status: status,
	})
	if err != nil {
		g.logger.Warn("write authz denial audit failed", "permission", permission, "error", err.Error())
	}
}
