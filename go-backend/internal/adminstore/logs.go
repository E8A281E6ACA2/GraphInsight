package adminstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

var ErrLogNotFound = errors.New("admin log not found")

type LogItem struct {
	ID           int       `json:"id"`
	UserID       *int      `json:"user_id,omitempty"`
	OperatorID   *int      `json:"operator_id,omitempty"`
	TenantID     *string   `json:"tenant_id,omitempty"`
	TraceID      *string   `json:"trace_id,omitempty"`
	Username     *string   `json:"username,omitempty"`
	Action       string    `json:"action"`
	Resource     *string   `json:"resource,omitempty"`
	ResourceID   *string   `json:"resource_id,omitempty"`
	Details      *string   `json:"details,omitempty"`
	IPAddress    *string   `json:"ip_address,omitempty"`
	UserAgent    *string   `json:"user_agent,omitempty"`
	Status       string    `json:"status"`
	Severity     string    `json:"severity"`
	ErrorMessage *string   `json:"error_message,omitempty"`
	CreatedAt    time.Time `json:"created_at"`
}

type LogDetail struct {
	ID           int         `json:"id"`
	UserID       *int        `json:"user_id,omitempty"`
	OperatorID   *int        `json:"operator_id,omitempty"`
	TenantID     *string     `json:"tenant_id,omitempty"`
	TraceID      *string     `json:"trace_id,omitempty"`
	Username     *string     `json:"username,omitempty"`
	Action       string      `json:"action"`
	Resource     *string     `json:"resource,omitempty"`
	ResourceID   *string     `json:"resource_id,omitempty"`
	Details      interface{} `json:"details,omitempty"`
	IPAddress    *string     `json:"ip_address,omitempty"`
	UserAgent    *string     `json:"user_agent,omitempty"`
	Status       string      `json:"status"`
	Severity     string      `json:"severity"`
	ErrorMessage *string     `json:"error_message,omitempty"`
	CreatedAt    time.Time   `json:"created_at"`
}

type LogStats struct {
	TotalLogs     int            `json:"total_logs"`
	SuccessCount  int            `json:"success_count"`
	FailedCount   int            `json:"failed_count"`
	SuccessRate   float64        `json:"success_rate"`
	SeverityStats map[string]int `json:"severity_stats"`
	ActionStats   map[string]int `json:"action_stats"`
	UserStats     map[string]int `json:"user_stats"`
	HourlyStats   map[string]int `json:"hourly_stats"`
}

type LogListQuery struct {
	UserID    *int
	Action    string
	Resource  string
	Status    string
	TraceID   string
	StartDate *time.Time
	EndDate   *time.Time
	IPAddress string
	Page      int
	PageSize  int
}

type LogListResult struct {
	Items []LogItem
	Total int
}

type LogExportAuditRequest struct {
	Rows       int
	OperatorID *int
	TenantID   *string
	TraceID    *string
	IPAddress  *string
	UserAgent  *string
}

type LogCleanRequest struct {
	Days       int
	DryRun     bool
	OperatorID *int
	TenantID   *string
	TraceID    *string
	IPAddress  *string
	UserAgent  *string
}

type LogCleanResult struct {
	DeletedCount int    `json:"deleted_count"`
	Days         int    `json:"days"`
	DryRun       bool   `json:"dry_run"`
	CutoffAt     string `json:"cutoff_at"`
}

type BusinessAuditRequest struct {
	OperatorID   *int
	TenantID     *string
	ProjectID    *string
	KBID         *string
	TraceID      *string
	Action       string
	Resource     string
	ResourceID   *string
	Details      map[string]interface{}
	IPAddress    *string
	UserAgent    *string
	Status       string
	ErrorMessage *string
}

func (c *Client) ListLogs(ctx context.Context, query LogListQuery) (LogListResult, error) {
	if c == nil || c.db == nil {
		return LogListResult{}, errors.New("admin store is not initialized")
	}
	page, pageSize := normalizeLogPagination(query.Page, query.PageSize)
	query.Page = page
	query.PageSize = pageSize

	where, args := buildLogWhere(query)
	var total int
	if err := c.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM admin_logs l"+where, args...).Scan(&total); err != nil {
		return LogListResult{}, fmt.Errorf("count admin logs failed: %w", err)
	}

	listArgs := append([]interface{}{}, args...)
	limitIndex := len(listArgs) + 1
	offsetIndex := len(listArgs) + 2
	listArgs = append(listArgs, pageSize, (page-1)*pageSize)

	rows, err := c.db.QueryContext(ctx, fmt.Sprintf(`
		SELECT
			l.id,
			l.user_id,
			l.operator_id,
			l.tenant_id,
			l.trace_id,
			u.username,
			l.action,
			l.resource,
			l.resource_id,
			l.details,
			l.ip_address,
			l.user_agent,
			COALESCE(l.status, 'success'),
			l.error_message,
			l.created_at
		FROM admin_logs l
		LEFT JOIN admin_users u ON l.user_id = u.id
		%s
		ORDER BY l.created_at DESC
		LIMIT $%d OFFSET $%d
	`, where, limitIndex, offsetIndex), listArgs...)
	if err != nil {
		return LogListResult{}, fmt.Errorf("query admin logs failed: %w", err)
	}
	defer rows.Close()

	items := []LogItem{}
	for rows.Next() {
		item, err := scanLogItem(rows)
		if err != nil {
			return LogListResult{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return LogListResult{}, fmt.Errorf("iterate admin logs failed: %w", err)
	}
	return LogListResult{Items: items, Total: total}, nil
}

func (c *Client) RecordLogExportAudit(ctx context.Context, req LogExportAuditRequest) error {
	if c == nil || c.db == nil {
		return errors.New("admin store is not initialized")
	}
	detailsPayload := map[string]interface{}{
		"rows": req.Rows,
	}
	if req.TraceID != nil && strings.TrimSpace(*req.TraceID) != "" {
		detailsPayload["trace_id"] = strings.TrimSpace(*req.TraceID)
	}
	detailsJSON, err := json.Marshal(detailsPayload)
	if err != nil {
		return fmt.Errorf("marshal log export audit details failed: %w", err)
	}
	status := "success"
	resource := "audit_log"
	action := "log_export_csv"
	_, err = c.db.ExecContext(ctx, `
		INSERT INTO admin_logs (
			user_id,
			operator_id,
			tenant_id,
			trace_id,
			action,
			resource,
			details,
			ip_address,
			user_agent,
			status,
			created_at
		) VALUES (
			$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11
		)
	`,
		req.OperatorID,
		req.OperatorID,
		req.TenantID,
		req.TraceID,
		action,
		resource,
		string(detailsJSON),
		req.IPAddress,
		req.UserAgent,
		status,
		time.Now().UTC(),
	)
	if err != nil {
		return fmt.Errorf("insert log export audit failed: %w", err)
	}
	return nil
}

func (c *Client) GetLogByID(ctx context.Context, logID int) (LogDetail, error) {
	if c == nil || c.db == nil {
		return LogDetail{}, errors.New("admin store is not initialized")
	}
	if logID <= 0 {
		return LogDetail{}, ErrLogNotFound
	}
	row := c.db.QueryRowContext(ctx, `
		SELECT
			l.id,
			l.user_id,
			l.operator_id,
			l.tenant_id,
			l.trace_id,
			u.username,
			l.action,
			l.resource,
			l.resource_id,
			l.details,
			l.ip_address,
			l.user_agent,
			COALESCE(l.status, 'success'),
			l.error_message,
			l.created_at
		FROM admin_logs l
		LEFT JOIN admin_users u ON l.user_id = u.id
		WHERE l.id = $1
		LIMIT 1
	`, logID)
	item, err := scanLogItem(row)
	if errors.Is(err, sql.ErrNoRows) {
		return LogDetail{}, ErrLogNotFound
	}
	if err != nil {
		return LogDetail{}, err
	}
	return LogDetail{
		ID:           item.ID,
		UserID:       item.UserID,
		OperatorID:   item.OperatorID,
		TenantID:     item.TenantID,
		TraceID:      item.TraceID,
		Username:     item.Username,
		Action:       item.Action,
		Resource:     item.Resource,
		ResourceID:   item.ResourceID,
		Details:      parseLogDetails(item.Details),
		IPAddress:    item.IPAddress,
		UserAgent:    item.UserAgent,
		Status:       item.Status,
		Severity:     item.Severity,
		ErrorMessage: item.ErrorMessage,
		CreatedAt:    item.CreatedAt,
	}, nil
}

func (c *Client) GetLogStats(ctx context.Context, startDate *time.Time, endDate *time.Time) (LogStats, error) {
	if c == nil || c.db == nil {
		return LogStats{}, errors.New("admin store is not initialized")
	}
	now := time.Now().UTC()
	if endDate == nil {
		endDate = &now
	}
	if startDate == nil {
		start := endDate.AddDate(0, 0, -7)
		startDate = &start
	}

	stats := emptyLogStats()
	rows, err := c.db.QueryContext(ctx, `
		SELECT
			COALESCE(l.status, 'success'),
			l.action,
			l.error_message,
			u.username,
			EXTRACT(HOUR FROM l.created_at)::int
		FROM admin_logs l
		LEFT JOIN admin_users u ON l.user_id = u.id
		WHERE l.created_at >= $1 AND l.created_at <= $2
	`, *startDate, *endDate)
	if err != nil {
		return LogStats{}, fmt.Errorf("query admin log stats failed: %w", err)
	}
	defer rows.Close()

	for rows.Next() {
		var status string
		var action string
		var errorMessage sql.NullString
		var username sql.NullString
		var hour int
		if err := rows.Scan(&status, &action, &errorMessage, &username, &hour); err != nil {
			return LogStats{}, fmt.Errorf("scan admin log stats failed: %w", err)
		}
		stats.TotalLogs++
		switch status {
		case "success":
			stats.SuccessCount++
		case "failed":
			stats.FailedCount++
		}
		stats.SeverityStats[classifyLogSeverity(status, action, stringPtrFromNull(errorMessage))]++
		if action != "" {
			stats.ActionStats[action]++
		}
		if username.Valid && username.String != "" {
			stats.UserStats[username.String]++
		}
		if hour >= 0 && hour <= 23 {
			stats.HourlyStats[fmt.Sprintf("%02d", hour)]++
		}
	}
	if err := rows.Err(); err != nil {
		return LogStats{}, fmt.Errorf("iterate admin log stats failed: %w", err)
	}
	if stats.TotalLogs > 0 {
		stats.SuccessRate = float64(stats.SuccessCount) / float64(stats.TotalLogs)
	}
	return stats, nil
}

func (c *Client) ListRecentLogs(ctx context.Context, limit int, action string) ([]LogItem, error) {
	if limit < 1 {
		limit = 10
	}
	if limit > 100 {
		limit = 100
	}
	result, err := c.ListLogs(ctx, LogListQuery{
		Action:   action,
		Page:     1,
		PageSize: limit,
	})
	if err != nil {
		return nil, err
	}
	return result.Items, nil
}

func (c *Client) CleanOldLogs(ctx context.Context, req LogCleanRequest) (LogCleanResult, error) {
	if c == nil || c.db == nil {
		return LogCleanResult{}, errors.New("admin store is not initialized")
	}
	days := req.Days
	if days < 1 {
		days = 90
	}
	if days > 365 {
		days = 365
	}
	cutoff := time.Now().UTC().Add(-time.Duration(days) * 24 * time.Hour)
	result := LogCleanResult{
		Days:     days,
		DryRun:   req.DryRun,
		CutoffAt: cutoff.Format(time.RFC3339),
	}

	if req.DryRun {
		if err := c.db.QueryRowContext(ctx, `
			SELECT COUNT(*)
			FROM admin_logs
			WHERE created_at < $1
		`, cutoff).Scan(&result.DeletedCount); err != nil {
			return LogCleanResult{}, fmt.Errorf("count old admin logs failed: %w", err)
		}
		return result, nil
	}

	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return LogCleanResult{}, fmt.Errorf("begin log clean transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	deleteSQL := `
		DELETE FROM admin_logs
		WHERE created_at < $1
	`
	args := []interface{}{cutoff}
	if req.TraceID != nil && strings.TrimSpace(*req.TraceID) != "" {
		deleteSQL += " AND (trace_id IS NULL OR trace_id <> $2)"
		args = append(args, strings.TrimSpace(*req.TraceID))
	}
	deleteResult, err := tx.ExecContext(ctx, deleteSQL, args...)
	if err != nil {
		return LogCleanResult{}, fmt.Errorf("delete old admin logs failed: %w", err)
	}
	affected, err := deleteResult.RowsAffected()
	if err != nil {
		return LogCleanResult{}, fmt.Errorf("read deleted admin log count failed: %w", err)
	}
	result.DeletedCount = int(affected)

	details, err := json.Marshal(map[string]interface{}{
		"days":          days,
		"deleted_count": result.DeletedCount,
		"cutoff_at":     result.CutoffAt,
	})
	if err != nil {
		return LogCleanResult{}, fmt.Errorf("encode log clean details failed: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO admin_logs (
			user_id,
			operator_id,
			tenant_id,
			trace_id,
			action,
			resource,
			details,
			ip_address,
			user_agent,
			status
		)
		VALUES ($1, $1, $2, $3, 'cleanup', 'logs', $4, $5, $6, 'success')
	`, req.OperatorID, req.TenantID, req.TraceID, string(details), req.IPAddress, req.UserAgent); err != nil {
		return LogCleanResult{}, fmt.Errorf("insert log clean audit failed: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return LogCleanResult{}, fmt.Errorf("commit log clean transaction failed: %w", err)
	}
	return result, nil
}

func (c *Client) RecordBusinessAudit(ctx context.Context, req BusinessAuditRequest) error {
	if c == nil || c.db == nil {
		return errors.New("admin store is not initialized")
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin business audit transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	encodedDetails, err := json.Marshal(req.Details)
	if err != nil {
		return fmt.Errorf("encode business audit details failed: %w", err)
	}

	status := strings.TrimSpace(req.Status)
	if status == "" {
		status = "success"
	}
	resource := strings.TrimSpace(req.Resource)
	if resource == "" {
		resource = "business"
	}

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO admin_logs (
			user_id,
			operator_id,
			tenant_id,
			project_id,
			kb_id,
			trace_id,
			action,
			resource,
			resource_id,
			details,
			ip_address,
			user_agent,
			status,
			error_message
		)
		VALUES ($1, $1, $2, $12, $13, $3, $4, $5, $6, $7, $8, $9, $10, $11)
	`, req.OperatorID, req.TenantID, req.TraceID, req.Action, resource, req.ResourceID, string(encodedDetails), req.IPAddress, req.UserAgent, status, req.ErrorMessage, req.ProjectID, req.KBID); err != nil {
		return fmt.Errorf("insert business audit log failed: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit business audit transaction failed: %w", err)
	}
	return nil
}

type logRowScanner interface {
	Scan(dest ...interface{}) error
}

func scanLogItem(scanner logRowScanner) (LogItem, error) {
	var item LogItem
	var userID sql.NullInt64
	var operatorID sql.NullInt64
	var tenantID sql.NullString
	var traceID sql.NullString
	var username sql.NullString
	var resource sql.NullString
	var resourceID sql.NullString
	var details sql.NullString
	var ipAddress sql.NullString
	var userAgent sql.NullString
	var errorMessage sql.NullString
	if err := scanner.Scan(
		&item.ID,
		&userID,
		&operatorID,
		&tenantID,
		&traceID,
		&username,
		&item.Action,
		&resource,
		&resourceID,
		&details,
		&ipAddress,
		&userAgent,
		&item.Status,
		&errorMessage,
		&item.CreatedAt,
	); err != nil {
		return LogItem{}, err
	}
	item.UserID = intPtrFromNull(userID)
	item.OperatorID = intPtrFromNull(operatorID)
	item.TenantID = stringPtrFromNull(tenantID)
	item.TraceID = stringPtrFromNull(traceID)
	item.Username = stringPtrFromNull(username)
	item.Resource = stringPtrFromNull(resource)
	item.ResourceID = stringPtrFromNull(resourceID)
	item.Details = stringPtrFromNull(details)
	item.IPAddress = stringPtrFromNull(ipAddress)
	item.UserAgent = stringPtrFromNull(userAgent)
	item.ErrorMessage = stringPtrFromNull(errorMessage)
	item.Severity = classifyLogSeverity(item.Status, item.Action, item.ErrorMessage)
	return item, nil
}

func buildLogWhere(query LogListQuery) (string, []interface{}) {
	clauses := []string{}
	args := []interface{}{}
	if query.UserID != nil {
		args = append(args, *query.UserID)
		clauses = append(clauses, fmt.Sprintf("l.user_id = $%d", len(args)))
	}
	if action := strings.TrimSpace(query.Action); action != "" {
		args = append(args, action)
		clauses = append(clauses, fmt.Sprintf("l.action = $%d", len(args)))
	}
	if resource := strings.TrimSpace(query.Resource); resource != "" {
		args = append(args, resource)
		clauses = append(clauses, fmt.Sprintf("l.resource = $%d", len(args)))
	}
	if status := strings.TrimSpace(query.Status); status != "" {
		args = append(args, status)
		clauses = append(clauses, fmt.Sprintf("l.status = $%d", len(args)))
	}
	if traceID := strings.TrimSpace(query.TraceID); traceID != "" {
		args = append(args, traceID)
		clauses = append(clauses, fmt.Sprintf("l.trace_id = $%d", len(args)))
	}
	if query.StartDate != nil {
		args = append(args, *query.StartDate)
		clauses = append(clauses, fmt.Sprintf("l.created_at >= $%d", len(args)))
	}
	if query.EndDate != nil {
		args = append(args, *query.EndDate)
		clauses = append(clauses, fmt.Sprintf("l.created_at <= $%d", len(args)))
	}
	if ipAddress := strings.TrimSpace(query.IPAddress); ipAddress != "" {
		args = append(args, ipAddress)
		clauses = append(clauses, fmt.Sprintf("l.ip_address = $%d", len(args)))
	}
	if len(clauses) == 0 {
		return "", args
	}
	return " WHERE " + strings.Join(clauses, " AND "), args
}

func normalizeLogPagination(page int, pageSize int) (int, int) {
	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = 20
	}
	if pageSize > 100 {
		pageSize = 100
	}
	return page, pageSize
}

func classifyLogSeverity(status string, action string, errorMessage *string) string {
	normalizedStatus := strings.ToLower(strings.TrimSpace(status))
	normalizedAction := strings.ToLower(strings.TrimSpace(action))
	normalizedError := ""
	if errorMessage != nil {
		normalizedError = strings.ToLower(strings.TrimSpace(*errorMessage))
	}
	if normalizedStatus == "failed" || normalizedError != "" {
		return "error"
	}
	for _, keyword := range []string{"retry", "cancel", "cleanup", "clean", "delete"} {
		if strings.Contains(normalizedAction, keyword) {
			return "warn"
		}
	}
	return "info"
}

func parseLogDetails(details *string) interface{} {
	if details == nil || strings.TrimSpace(*details) == "" {
		return nil
	}
	parsed := parseNullableJSON(details)
	if parsed != nil {
		return parsed
	}
	return map[string]string{"raw": *details}
}

func parseNullableJSON(value *string) interface{} {
	if value == nil || strings.TrimSpace(*value) == "" {
		return nil
	}
	var decoded interface{}
	if err := json.Unmarshal([]byte(*value), &decoded); err == nil {
		return decoded
	}
	return nil
}

func emptyLogStats() LogStats {
	hourlyStats := map[string]int{}
	for hour := 0; hour < 24; hour++ {
		hourlyStats[fmt.Sprintf("%02d", hour)] = 0
	}
	return LogStats{
		SeverityStats: map[string]int{
			"info":  0,
			"warn":  0,
			"error": 0,
		},
		ActionStats: map[string]int{},
		UserStats:   map[string]int{},
		HourlyStats: hourlyStats,
	}
}

func intPtrFromNull(value sql.NullInt64) *int {
	if !value.Valid {
		return nil
	}
	normalized := int(value.Int64)
	return &normalized
}
