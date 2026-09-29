package httpserver

import (
	"bytes"
	"context"
	"encoding/csv"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"graphinsight/go-backend/internal/adminstore"
)

type adminLogStore interface {
	ListLogs(ctx context.Context, query adminstore.LogListQuery) (adminstore.LogListResult, error)
	GetLogByID(ctx context.Context, logID int) (adminstore.LogDetail, error)
	GetLogStats(ctx context.Context, startDate *time.Time, endDate *time.Time) (adminstore.LogStats, error)
	ListRecentLogs(ctx context.Context, limit int, action string) ([]adminstore.LogItem, error)
	CleanOldLogs(ctx context.Context, req adminstore.LogCleanRequest) (adminstore.LogCleanResult, error)
	RecordLogExportAudit(ctx context.Context, req adminstore.LogExportAuditRequest) error
	RecordBusinessAudit(ctx context.Context, req adminstore.BusinessAuditRequest) error
}

func asAdminLogStore(store interface{}) adminLogStore {
	typed, _ := store.(adminLogStore)
	return typed
}

func buildAdminLogsReadNativeHandler(logger *slog.Logger, guard businessPermissionGuard, logStore adminLogStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("logs:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if logStore == nil {
			logger.Error("admin log store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "日志数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		switch {
		case r.URL.Path == "/api/v1/admin/logs":
			if optionalBoolQueryDefault(r, "export", false) {
				writeAdminLogsCSVExport(w, r, logger, logStore)
				return
			}
			page := boundedIntQuery(r, "page", 1, 1, 1_000_000)
			pageSize := boundedIntQuery(r, "page_size", 20, 1, 100)
			userID, ok := optionalPositiveIntQuery(w, r, "user_id")
			if !ok {
				return
			}
			startDate, ok := optionalTimeQuery(w, r, "start_date")
			if !ok {
				return
			}
			endDate, ok := optionalTimeQuery(w, r, "end_date")
			if !ok {
				return
			}
			result, err := logStore.ListLogs(r.Context(), adminstore.LogListQuery{
				UserID:    userID,
				Action:    strings.TrimSpace(r.URL.Query().Get("action")),
				Resource:  strings.TrimSpace(r.URL.Query().Get("resource")),
				Status:    strings.TrimSpace(r.URL.Query().Get("status")),
				TraceID:   strings.TrimSpace(r.URL.Query().Get("trace_id")),
				StartDate: startDate,
				EndDate:   endDate,
				IPAddress: strings.TrimSpace(r.URL.Query().Get("ip_address")),
				Page:      page,
				PageSize:  pageSize,
			})
			if err != nil {
				logger.Error("list admin logs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取日志列表失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			totalPages := 0
			if pageSize > 0 && result.Total > 0 {
				totalPages = (result.Total + pageSize - 1) / pageSize
			}
			WriteJSON(w, http.StatusOK, "获取成功", adminPaginatedData{
				Items:      result.Items,
				Total:      result.Total,
				Page:       page,
				PageSize:   pageSize,
				TotalPages: totalPages,
			})
		case r.URL.Path == "/api/v1/admin/logs/stats/summary":
			startDate, ok := optionalTimeQuery(w, r, "start_date")
			if !ok {
				return
			}
			endDate, ok := optionalTimeQuery(w, r, "end_date")
			if !ok {
				return
			}
			stats, err := logStore.GetLogStats(r.Context(), startDate, endDate)
			if err != nil {
				logger.Error("get admin log stats failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取日志统计失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", stats)
		case r.URL.Path == "/api/v1/admin/logs/recent/list":
			logs, err := logStore.ListRecentLogs(
				r.Context(),
				boundedIntQuery(r, "limit", 10, 1, 100),
				strings.TrimSpace(r.URL.Query().Get("action")),
			)
			if err != nil {
				logger.Error("list recent admin logs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取最近日志失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", logs)
		case strings.HasPrefix(r.URL.Path, "/api/v1/admin/logs/"):
			logID, ok := parseAdminLogDetailPath(r.URL.Path)
			if !ok || logID <= 0 {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			logDetail, err := logStore.GetLogByID(r.Context(), logID)
			if errors.Is(err, adminstore.ErrLogNotFound) {
				WriteJSON(w, http.StatusNotFound, "日志不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if err != nil {
				logger.Error("get admin log detail failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取日志详情失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", logDetail)
		default:
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	}))
}

const maxAdminLogsCSVExportRows = 100000

func writeAdminLogsCSVExport(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	logStore adminLogStore,
) {
	userID, ok := optionalPositiveIntQuery(w, r, "user_id")
	if !ok {
		return
	}
	startDate, ok := optionalTimeQuery(w, r, "start_date")
	if !ok {
		return
	}
	endDate, ok := optionalTimeQuery(w, r, "end_date")
	if !ok {
		return
	}
	result, err := logStore.ListLogs(r.Context(), adminstore.LogListQuery{
		UserID:    userID,
		Action:    strings.TrimSpace(r.URL.Query().Get("action")),
		Resource:  strings.TrimSpace(r.URL.Query().Get("resource")),
		Status:    strings.TrimSpace(r.URL.Query().Get("status")),
		TraceID:   strings.TrimSpace(r.URL.Query().Get("trace_id")),
		StartDate: startDate,
		EndDate:   endDate,
		IPAddress: strings.TrimSpace(r.URL.Query().Get("ip_address")),
		Page:      1,
		PageSize:  maxAdminLogsCSVExportRows,
	})
	if err != nil {
		logger.Error("export admin logs csv failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "导出日志失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return
	}

	var buffer bytes.Buffer
	buffer.WriteString("\uFEFF")
	writer := csv.NewWriter(&buffer)
	if err := writer.Write([]string{
		"id", "user_id", "operator_id", "tenant_id", "trace_id", "username",
		"action", "resource", "resource_id", "status", "severity",
		"error_message", "ip_address", "user_agent", "details", "created_at",
	}); err != nil {
		logger.Error("encode logs csv header failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "导出日志失败", map[string]string{"error_code": "CSV_ENCODE_FAILED"})
		return
	}
	for _, item := range result.Items {
		if err := writer.Write([]string{
			strconv.Itoa(item.ID),
			optionalIntString(item.UserID),
			optionalIntString(item.OperatorID),
			optionalStringValue(item.TenantID),
			optionalStringValue(item.TraceID),
			optionalStringValue(item.Username),
			item.Action,
			optionalStringValue(item.Resource),
			optionalStringValue(item.ResourceID),
			item.Status,
			item.Severity,
			optionalStringValue(item.ErrorMessage),
			optionalStringValue(item.IPAddress),
			optionalStringValue(item.UserAgent),
			optionalStringValue(item.Details),
			item.CreatedAt.UTC().Format(time.RFC3339),
		}); err != nil {
			logger.Error("encode logs csv row failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "导出日志失败", map[string]string{"error_code": "CSV_ENCODE_FAILED"})
			return
		}
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		logger.Error("flush logs csv failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "导出日志失败", map[string]string{"error_code": "CSV_ENCODE_FAILED"})
		return
	}

	if err := logStore.RecordLogExportAudit(r.Context(), adminstore.LogExportAuditRequest{
		Rows:       len(result.Items),
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
		TraceID:    optionalStringHeader(r, traceHeader),
		IPAddress:  optionalString(firstRemoteAddr(r)),
		UserAgent:  optionalString(r.UserAgent()),
	}); err != nil {
		logger.Warn("write log export audit failed", "error", err.Error())
	}

	filename := fmt.Sprintf("admin_logs_%s.csv", time.Now().UTC().Format("20060102_150405"))
	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", filename))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(buffer.Bytes())
}

func buildAdminLogsCleanNativeHandler(logger *slog.Logger, guard businessPermissionGuard, logStore adminLogStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("logs:clean", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if r.URL.Path != "/api/v1/admin/logs/clean" {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		if logStore == nil {
			logger.Error("admin log store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "日志数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		result, err := logStore.CleanOldLogs(r.Context(), adminstore.LogCleanRequest{
			Days:       boundedIntQuery(r, "days", 90, 1, 365),
			DryRun:     optionalBoolQueryDefault(r, "dry_run", false),
			OperatorID: optionalIntHeader(r, "x-auth-user-id"),
			TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
			TraceID:    optionalStringHeader(r, traceHeader),
			IPAddress:  optionalString(firstRemoteAddr(r)),
			UserAgent:  optionalString(r.UserAgent()),
		})
		if err != nil {
			logger.Error("clean old admin logs failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "清理日志失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "清理完成", result)
	}))
}

func parseAdminLogDetailPath(path string) (logID int, ok bool) {
	rest := strings.TrimPrefix(path, "/api/v1/admin/logs/")
	if rest == "" || strings.Contains(rest, "/") {
		return 0, false
	}
	id, err := strconv.Atoi(rest)
	if err != nil {
		return 0, false
	}
	return id, true
}

func optionalIntString(value *int) string {
	if value == nil {
		return ""
	}
	return strconv.Itoa(*value)
}

func optionalStringValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func optionalTimeQuery(w http.ResponseWriter, r *http.Request, key string) (*time.Time, bool) {
	raw := strings.TrimSpace(r.URL.Query().Get(key))
	if raw == "" {
		return nil, true
	}
	for _, layout := range []string{time.RFC3339, "2006-01-02T15:04:05", "2006-01-02"} {
		value, err := time.Parse(layout, raw)
		if err == nil {
			return &value, true
		}
	}
	WriteJSON(w, http.StatusBadRequest, "参数错误", map[string]string{"error_code": "INVALID_QUERY"})
	return nil, false
}
