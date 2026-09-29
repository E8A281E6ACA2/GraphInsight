package httpserver

// M4 问答/检索隔离（契约 §2.4/§3.3、手册 §7.4/§12/§14.2）：
// docqa / deep-research / nl2cypher 请求必须显式携带 kb 作用域（header/query/body
// 三来源严格一致），服务端计算 请求 ∩ 授权 的有效范围后把 scope 转发 Python。
// Python 侧独立校验 payload；本文件是 Go 层的强制点。

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/scope"
)

// qaScopeAuthorizer 由 adminstore.Client 实现：解析 subject 在
// admin_user_role_bindings 中的授权 KB 集合（kb 绑定 → 显式清单；
// global 绑定授予对应权限码 → allKBs 哨兵；tenant/project 绑定 →
// 通过 knowledge_bases 目录反查父作用域下的真实 KB 集合，M4-R1 FIX #1，
// 禁止放大为全部 KB）。
type qaScopeAuthorizer interface {
	AuthorizedKBIDs(ctx context.Context, subject string, permissions ...string) ([]string, bool, error)
}

// resolveQARequestScope 解析问答/检索请求的 kb 作用域：header/query 与 body
// （payload 解码出的 kb_id/kb_ids）三来源严格一致（契约 §3.2）。
// 不一致 → KB_CROSS_SCOPE；缺失 → KB_SCOPE_REQUIRED；非法 → SCOPE_INVALID。
func resolveQARequestScope(r *http.Request, bodyKBID string, bodyKBIDs []string) (*scope.SearchTarget, *scope.Error) {
	return scope.ResolveRequestWithBody(r, scope.BodySource("", "", bodyKBID, bodyKBIDs))
}

// authorizeQAEffectiveKBIDs 是问答/检索路由的第二阶段范围授权（契约 §2.4）：
// 计算 请求 KB ∩ 授权 KB。allKBs 哨兵短路为请求集合；交集为空 → 403 KB_ACCESS_DENIED
// 并写拒绝审计。返回 false 时响应（含审计）已写出。
//
// 语义说明（M4-R1 审计 P0 + 复审整改：KB 路由无条件 fail-closed）：
//   - subject 为空（未认证、无法归因主体）→ 401 拒绝（含审计），不再随第一阶段
//     soft 放行获得任意 KB 范围；
//   - 授权服务未接入 / 不支持 KB 作用域解析（含 adminStore 为 nil）→ 503 拒绝，
//     不再在 RBACEnforceBusinessAPI=false 下降级放行；
//   - 授权解析失败（AuthorizedKBIDs err）/ 交集为空（跨 KB）→ 无论 enforce/soft 一律拒绝。
func (g businessPermissionGuard) authorizeQAEffectiveKBIDs(
	w http.ResponseWriter,
	r *http.Request,
	logStore adminLogStore,
	permission string,
	target *scope.SearchTarget,
) ([]string, bool) {
	if target == nil || len(target.KBIDs) == 0 {
		writeScopeError(w, scope.ErrScopeRequired())
		return nil, false
	}
	subject := strings.TrimSpace(r.Header.Get("x-auth-user-name"))
	if subject == "" {
		g.logger.Warn("missing authenticated subject for kb scope authorization, fail closed", "permission", permission)
		writeQAAuthzDeniedAudit(r, g.logger, logStore, permission, target, false)
		w.Header().Set("WWW-Authenticate", "Bearer")
		WriteJSON(w, http.StatusUnauthorized, "缺少认证凭证", map[string]string{"error_code": "UNAUTHORIZED"})
		return nil, false
	}
	authorizer, ok := g.adminStore.(qaScopeAuthorizer)
	if !ok {
		g.logger.Error("admin store does not support kb scope authorization, fail closed", "permission", permission)
		writeQAAuthzRejectionAudit(r, g.logger, logStore, permission, target, "AUTHZ_UNAVAILABLE")
		WriteJSON(w, http.StatusServiceUnavailable, "授权服务不可用", map[string]string{"error_code": "AUTHZ_UNAVAILABLE"})
		return nil, false
	}
	authorized, allKBs, err := authorizer.AuthorizedKBIDs(r.Context(), subject, permission)
	if err != nil {
		// KB 边界 fail-closed：无法解析授权集合时不得降级放行跨 KB 范围。
		g.logger.Error("resolve authorized kb ids failed, fail closed", "permission", permission, "error", err.Error())
		writeQAAuthzRejectionAudit(r, g.logger, logStore, permission, target, "AUTHZ_UNAVAILABLE")
		WriteJSON(w, http.StatusServiceUnavailable, "授权服务不可用", map[string]string{"error_code": "AUTHZ_UNAVAILABLE"})
		return nil, false
	}
	if allKBs {
		return target.KBIDs, true
	}
	effective, scopeErr := target.EffectiveKBIDs(authorized)
	if scopeErr != nil {
		// 交集为空即跨 KB：无论 enforce/soft 一律 403，写拒绝审计。
		writeQAAuthzDeniedAudit(r, g.logger, logStore, permission, target, false)
		WriteJSON(w, http.StatusForbidden, "请求的知识库不在授权范围内", map[string]string{"error_code": scope.CodeAccessDenied})
		return nil, false
	}
	return effective, true
}

// authorizeQAKBScopedRequest 是检索诊断/QA 类管理接口的第二阶段 KB 授权完整链
// （M4-R1 审计 P0-1，契约 §2.4/§3.2）：
//
//	解析 kb scope（header/query/body 严格一致）
//	-> 加载 KB 权威行（adminstore 为权威，不存在 → 404 KB_NOT_FOUND）
//	-> 校验请求声明的 tenant/project/kb 与 KB 行一致（不一致 → KB_CROSS_SCOPE）
//	-> 以 KB 行真实 {tenant, project, kb} 作用域执行权限求交（拒绝 → 403 KB_ACCESS_DENIED）
//
// 任一步失败即写出响应（含拒绝审计）并返回 false，调用方不得触达 Python。
// 返回的 effective KB 集合即服务端规范化后的转发作用域。
func (g businessPermissionGuard) authorizeQAKBScopedRequest(
	w http.ResponseWriter,
	r *http.Request,
	logStore adminLogStore,
	kbStore adminKBStore,
	permission string,
	bodyKBID string,
	bodyKBIDs []string,
) (*scope.SearchTarget, []string, bool) {
	target, scopeErr := resolveQARequestScope(r, bodyKBID, bodyKBIDs)
	if scopeErr != nil {
		writeQAScopeRejectionAudit(r, g.logger, logStore, permission, scopeErr)
		writeScopeError(w, scopeErr)
		return nil, nil, false
	}
	if kbStore == nil {
		g.logger.Error("knowledge base store unavailable for qa scope authorization", "permission", permission)
		writeQAAuthzRejectionAudit(r, g.logger, logStore, permission, target, "ADMIN_STORE_UNAVAILABLE")
		WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return nil, nil, false
	}
	for _, kbID := range target.KBIDs {
		item, err := kbStore.GetKnowledgeBase(r.Context(), kbID)
		if errors.Is(err, adminstore.ErrKBNotFound) {
			writeQAAuthzRejectionAudit(r, g.logger, logStore, permission, target, scope.CodeKBNotFound)
			writeScopeError(w, &scope.Error{Code: scope.CodeKBNotFound, Message: "知识库不存在", Status: http.StatusNotFound})
			return nil, nil, false
		}
		if err != nil {
			g.logger.Error("load knowledge base for qa scope authorization failed", "kb_id", kbID, "error", err.Error())
			writeQAAuthzRejectionAudit(r, g.logger, logStore, permission, target, "ADMIN_STORE_UNAVAILABLE")
			WriteJSON(w, http.StatusServiceUnavailable, "查询知识库失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return nil, nil, false
		}
		if mismatch := ensureKBRequestScopeMatches(r, item); mismatch != nil {
			writeQAScopeRejectionAudit(r, g.logger, logStore, permission, mismatch)
			writeScopeError(w, mismatch)
			return nil, nil, false
		}
		if !g.checkPermissionWithScope(r, permission, kbScopeMap(item)) {
			// KB 独立 fail-closed（M4-R1 审计 P0）：跨 KB 拒绝不受
			// RBACEnforceBusinessAPI=false 或 local_jwt_soft 的 soft allow 影响，
			// 拒绝路径不得触达 Python。
			writeQAAuthzDeniedAudit(r, g.logger, logStore, permission, target, false)
			writeScopeError(w, scope.ErrAccessDenied(target.KBIDs))
			return nil, nil, false
		}
	}
	return target, target.KBIDs, true
}

// writeQAAuthzDeniedAudit 记录 KB_ACCESS_DENIED 拒绝审计（契约 §2.10：拒绝路径同样写审计）。
func writeQAAuthzDeniedAudit(
	r *http.Request,
	logger *slog.Logger,
	logStore adminLogStore,
	permission string,
	target *scope.SearchTarget,
	softAllow bool,
) {
	writeQAAuthzRejectionAudit(r, logger, logStore, permission, target, scope.CodeAccessDenied, softAllow)
}

func writeQAAuthzRejectionAudit(
	r *http.Request,
	logger *slog.Logger,
	logStore adminLogStore,
	permission string,
	target *scope.SearchTarget,
	errorCode string,
	softAllow ...bool,
) {
	if logStore == nil {
		return
	}
	status := "denied"
	if len(softAllow) > 0 && softAllow[0] {
		status = "denied_soft_allow"
	}
	request := adminstore.BusinessAuditRequest{
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TenantID:   optionalString(target.TenantID),
		ProjectID:  optionalString(target.ProjectID),
		TraceID:    optionalStringHeader(r, traceHeader),
		Action:     "authz_denied",
		Resource:   "kb_scope",
		Details: map[string]interface{}{
			"permission":       permission,
			"error_code":       errorCode,
			"requested_kb_ids": target.KBIDs,
		},
		Status: status,
	}
	if err := logStore.RecordBusinessAudit(r.Context(), request); err != nil {
		logger.Warn("write qa scope denial audit failed", "permission", permission, "error", err.Error())
	}
}

// writeQAScopeRejectionAudit 记录作用域解析拒绝（KB_SCOPE_REQUIRED / KB_CROSS_SCOPE /
// SCOPE_INVALID）审计（契约 §2.10）。
func writeQAScopeRejectionAudit(
	r *http.Request,
	logger *slog.Logger,
	logStore adminLogStore,
	permission string,
	scopeErr *scope.Error,
) {
	if logStore == nil || scopeErr == nil {
		return
	}
	scopeHeaders := resolveScopeHeaders(r)
	request := adminstore.BusinessAuditRequest{
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TenantID:   scopeStringPtr(scopeHeaders["x-tenant-id"]),
		ProjectID:  scopeStringPtr(scopeHeaders["x-project-id"]),
		KBID:       scopeStringPtr(scopeHeaders["x-kb-id"]),
		TraceID:    optionalStringHeader(r, traceHeader),
		Action:     "authz_denied",
		Resource:   "kb_scope",
		Details: map[string]interface{}{
			"permission": permission,
			"error_code": scopeErr.Code,
		},
		Status: "denied",
	}
	if err := logStore.RecordBusinessAudit(r.Context(), request); err != nil {
		logger.Warn("write qa scope rejection audit failed", "permission", permission, "error", err.Error())
	}
}
