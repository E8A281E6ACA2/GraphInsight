package httpserver

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/scope"
)

type adminQATraceStore interface {
	ListQATraces(ctx context.Context, query adminstore.QATraceListQuery) (adminstore.QATraceListResult, error)
	GetQATrace(ctx context.Context, traceIDOrPK string, kbID string) (adminstore.QATraceDetail, error)
	GetQACostSummary(ctx context.Context, query adminstore.QACostSummaryQuery) (adminstore.QACostSummary, error)
}

func asAdminQATraceStore(store interface{}) adminQATraceStore {
	typed, _ := store.(adminQATraceStore)
	return typed
}

func buildAdminQATracesNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	traceStore adminQATraceStore,
	kbStore adminKBStore,
	logStore adminLogStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if traceStore == nil {
			logger.Error("admin qa trace store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "QA trace 数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		// M4-R1 审计 P0-2：QA trace 含问题/答案摘要/检索快照/引用，不能跨 KB 全局可读。
		// 第二阶段 KB 授权：解析 kb scope -> 加载 KB 权威行 -> 校验归属 -> 权限求交。
		// list/detail/cost-summary 均限定单一 KB 作用域。
		_, effectiveKBIDs, ok := guard.authorizeQAKBScopedRequest(w, r, logStore, kbStore, "monitor:read", "", nil)
		if !ok {
			return
		}
		if len(effectiveKBIDs) != 1 {
			writeScopeError(w, scope.ErrCrossScope("kb_id"))
			return
		}
		kbID := effectiveKBIDs[0]

		switch {
		case r.URL.Path == "/api/v1/admin/qa-traces":
			page := boundedIntQuery(r, "page", 1, 1, 1_000_000)
			pageSize := boundedIntQuery(r, "page_size", 20, 1, 200)
			operatorID, ok := optionalPositiveIntQuery(w, r, "operator_id")
			if !ok {
				return
			}
			result, err := traceStore.ListQATraces(r.Context(), adminstore.QATraceListQuery{
				QAType:     strings.TrimSpace(r.URL.Query().Get("qa_type")),
				Status:     strings.TrimSpace(r.URL.Query().Get("status")),
				TraceID:    strings.TrimSpace(r.URL.Query().Get("trace_id")),
				OperatorID: operatorID,
				Keyword:    strings.TrimSpace(r.URL.Query().Get("keyword")),
				KBID:       kbID,
				Page:       page,
				PageSize:   pageSize,
			})
			if err != nil {
				logger.Error("list qa traces failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "List QA traces failed", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			totalPages := 0
			if pageSize > 0 && result.Total > 0 {
				totalPages = (result.Total + pageSize - 1) / pageSize
			}
			WriteJSON(w, http.StatusOK, "ok", adminPaginatedData{
				Items:      result.Items,
				Total:      result.Total,
				Page:       page,
				PageSize:   pageSize,
				TotalPages: totalPages,
			})
		case r.URL.Path == "/api/v1/admin/qa-traces/cost-summary":
			windowHours := boundedIntQuery(r, "window_hours", 24, 1, 24*90)
			summary, err := traceStore.GetQACostSummary(r.Context(), adminstore.QACostSummaryQuery{
				QAType:      strings.TrimSpace(r.URL.Query().Get("qa_type")),
				Status:      strings.TrimSpace(r.URL.Query().Get("status")),
				KBID:        kbID,
				WindowHours: windowHours,
			})
			if err != nil {
				logger.Error("get qa cost summary failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "Get QA model cost summary failed", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "ok", summary)
		case strings.HasPrefix(r.URL.Path, "/api/v1/admin/qa-traces/"):
			traceIDOrPK := strings.TrimPrefix(r.URL.Path, "/api/v1/admin/qa-traces/")
			if strings.TrimSpace(traceIDOrPK) == "" {
				WriteJSON(w, http.StatusNotFound, "QA trace not found", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			trace, err := traceStore.GetQATrace(r.Context(), traceIDOrPK, kbID)
			if errors.Is(err, adminstore.ErrQATraceNotFound) {
				WriteJSON(w, http.StatusNotFound, "QA trace not found", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if err != nil {
				logger.Error("get qa trace detail failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "Get QA trace detail failed", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "ok", trace)
		default:
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	}))
}
