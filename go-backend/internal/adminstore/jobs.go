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

var ErrJobNotFound = errors.New("admin job not found")
var ErrJobValidation = errors.New("admin job validation failed")
var ErrJobInvalidTransition = errors.New("admin job invalid status transition")
var ErrJobMaxRetriesReached = errors.New("admin job max retries reached")

const (
	JobStatusPending   = "pending"
	JobStatusRunning   = "running"
	JobStatusSucceeded = "succeeded"
	JobStatusFailed    = "failed"
	JobStatusCancelled = "cancelled"
)

var supportedJobTypes = map[string]struct{}{
	"build_graph": {},
	"clear_kb":    {},
	"reindex":     {},
}

// jobColumns 是 JobItem 读取投影的唯一真相源：顺序必须与 scanJobItem 的 Scan 目标一一对应。
// 加列只改一边不会报错，位置扫描会静默错位，因此两处必须同批修改。
const jobColumns = `id,
	job_type,
	status,
	tenant_id,
	project_id,
	kb_id,
	payload,
	result,
	error_message,
	retry_count,
	max_retries,
	requested_by,
	trace_id,
	started_at,
	finished_at,
	created_at,
	updated_at,
	targets_hash`

type JobItem struct {
	ID           int                    `json:"id"`
	JobType      string                 `json:"job_type"`
	Status       string                 `json:"status"`
	TenantID     *string                `json:"tenant_id,omitempty"`
	ProjectID    *string                `json:"project_id,omitempty"`
	KBID         *string                `json:"kb_id,omitempty"`
	Payload      map[string]interface{} `json:"payload"`
	Result       map[string]interface{} `json:"result,omitempty"`
	ErrorMessage *string                `json:"error_message,omitempty"`
	RetryCount   int                    `json:"retry_count"`
	MaxRetries   int                    `json:"max_retries"`
	RequestedBy  *int                   `json:"requested_by,omitempty"`
	TraceID      *string                `json:"trace_id,omitempty"`
	StartedAt    *time.Time             `json:"started_at,omitempty"`
	FinishedAt   *time.Time             `json:"finished_at,omitempty"`
	CreatedAt    time.Time              `json:"created_at"`
	UpdatedAt    *time.Time             `json:"updated_at,omitempty"`
	TargetsHash  *string                `json:"targets_hash,omitempty"`
}

type JobLogItem struct {
	ID           int                    `json:"id"`
	Action       string                 `json:"action"`
	Status       string                 `json:"status"`
	ErrorMessage *string                `json:"error_message,omitempty"`
	TraceID      *string                `json:"trace_id,omitempty"`
	OperatorID   *int                   `json:"operator_id,omitempty"`
	CreatedAt    time.Time              `json:"created_at"`
	Details      map[string]interface{} `json:"details,omitempty"`
}

type JobListQuery struct {
	JobType   string
	Status    string
	TenantID  string
	ProjectID string
	KBID      string
	Page      int
	PageSize  int
}

type JobListResult struct {
	Items []JobItem
	Total int
}

type JobLogListResult struct {
	Items []JobLogItem
	Total int
}

type JobCreateRequest struct {
	JobType     string
	TenantID    *string
	ProjectID   *string
	KBID        *string
	Payload     map[string]interface{}
	MaxRetries  int
	RequestedBy *int
	TraceID     *string
	IPAddress   *string
	UserAgent   *string
}

type JobRetryRequest struct {
	JobID      int
	OperatorID *int
	TraceID    *string
	IPAddress  *string
	UserAgent  *string
}

type JobCancelRequest struct {
	JobID      int
	OperatorID *int
	TraceID    *string
	IPAddress  *string
	UserAgent  *string
}

func (c *Client) CreateJob(ctx context.Context, req JobCreateRequest) (JobItem, error) {
	if c == nil || c.db == nil {
		return JobItem{}, errors.New("admin store is not initialized")
	}
	req.JobType = strings.TrimSpace(req.JobType)
	if err := validateJobCreateRequest(req); err != nil {
		return JobItem{}, err
	}
	payloadText, err := encodeJobObject(req.Payload)
	if err != nil {
		return JobItem{}, err
	}

	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return JobItem{}, fmt.Errorf("begin create admin job transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	row := tx.QueryRowContext(ctx, `
		INSERT INTO admin_jobs (
			job_type,
			status,
			tenant_id,
			project_id,
			kb_id,
			payload,
			retry_count,
			max_retries,
			requested_by,
			trace_id
		)
		VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8, $9)
		RETURNING
			`+jobColumns+`
	`, req.JobType, JobStatusPending, req.TenantID, req.ProjectID, req.KBID, payloadText, req.MaxRetries, req.RequestedBy, req.TraceID)
	item, err := scanJobItem(row)
	if err != nil {
		return JobItem{}, fmt.Errorf("insert admin job failed: %w", err)
	}
	if err := insertJobAuditLog(ctx, tx, "job_created", item, req.RequestedBy, req.TraceID, req.IPAddress, req.UserAgent, map[string]interface{}{
		"job_type":    item.JobType,
		"status":      item.Status,
		"max_retries": item.MaxRetries,
	}); err != nil {
		return JobItem{}, err
	}
	if err := tx.Commit(); err != nil {
		return JobItem{}, fmt.Errorf("commit create admin job transaction failed: %w", err)
	}
	return item, nil
}

func (c *Client) RetryJob(ctx context.Context, req JobRetryRequest) (JobItem, error) {
	if c == nil || c.db == nil {
		return JobItem{}, errors.New("admin store is not initialized")
	}
	if req.JobID <= 0 {
		return JobItem{}, ErrJobNotFound
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return JobItem{}, fmt.Errorf("begin retry admin job transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	current, err := getJobForUpdate(ctx, tx, req.JobID)
	if errors.Is(err, sql.ErrNoRows) {
		return JobItem{}, ErrJobNotFound
	}
	if err != nil {
		return JobItem{}, err
	}
	if current.Status != JobStatusFailed && current.Status != JobStatusCancelled {
		return JobItem{}, ErrJobInvalidTransition
	}
	if current.RetryCount >= current.MaxRetries {
		return JobItem{}, ErrJobMaxRetriesReached
	}

	row := tx.QueryRowContext(ctx, `
		UPDATE admin_jobs
		SET
			retry_count = COALESCE(retry_count, 0) + 1,
			status = $2,
			error_message = NULL,
			result = NULL,
			started_at = NULL,
			finished_at = NULL,
			requested_by = COALESCE($3, requested_by),
			trace_id = COALESCE($4, trace_id),
			updated_at = NOW()
		WHERE id = $1
		RETURNING
			`+jobColumns+`
	`, req.JobID, JobStatusPending, req.OperatorID, req.TraceID)
	item, err := scanJobItem(row)
	if err != nil {
		return JobItem{}, fmt.Errorf("update admin job retry failed: %w", err)
	}
	if err := insertJobAuditLog(ctx, tx, "job_retry_submitted", item, req.OperatorID, req.TraceID, req.IPAddress, req.UserAgent, map[string]interface{}{
		"job_type":    item.JobType,
		"retry_count": item.RetryCount,
		"max_retries": item.MaxRetries,
		"operator_id": req.OperatorID,
	}); err != nil {
		return JobItem{}, err
	}
	if err := tx.Commit(); err != nil {
		return JobItem{}, fmt.Errorf("commit retry admin job transaction failed: %w", err)
	}
	return item, nil
}

func (c *Client) CancelJob(ctx context.Context, req JobCancelRequest) (JobItem, error) {
	if c == nil || c.db == nil {
		return JobItem{}, errors.New("admin store is not initialized")
	}
	if req.JobID <= 0 {
		return JobItem{}, ErrJobNotFound
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return JobItem{}, fmt.Errorf("begin cancel admin job transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	current, err := getJobForUpdate(ctx, tx, req.JobID)
	if errors.Is(err, sql.ErrNoRows) {
		return JobItem{}, ErrJobNotFound
	}
	if err != nil {
		return JobItem{}, err
	}
	if current.Status != JobStatusPending && current.Status != JobStatusRunning {
		return JobItem{}, ErrJobInvalidTransition
	}

	row := tx.QueryRowContext(ctx, `
		UPDATE admin_jobs
		SET
			status = $2,
			finished_at = NOW(),
			trace_id = COALESCE($3, trace_id),
			updated_at = NOW()
		WHERE id = $1
		RETURNING
			`+jobColumns+`
	`, req.JobID, JobStatusCancelled, req.TraceID)
	item, err := scanJobItem(row)
	if err != nil {
		return JobItem{}, fmt.Errorf("update admin job cancel failed: %w", err)
	}
	if err := insertJobAuditLog(ctx, tx, "job_cancelled", item, req.OperatorID, req.TraceID, req.IPAddress, req.UserAgent, map[string]interface{}{
		"job_type": item.JobType,
		"status":   item.Status,
	}); err != nil {
		return JobItem{}, err
	}
	if err := tx.Commit(); err != nil {
		return JobItem{}, fmt.Errorf("commit cancel admin job transaction failed: %w", err)
	}
	return item, nil
}

func (c *Client) ListJobs(ctx context.Context, query JobListQuery) (JobListResult, error) {
	if c == nil || c.db == nil {
		return JobListResult{}, errors.New("admin store is not initialized")
	}
	page, pageSize := normalizeJobPagination(query.Page, query.PageSize)
	query.Page = page
	query.PageSize = pageSize
	where, args := buildJobWhere(query)

	var total int
	if err := c.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM admin_jobs j"+where, args...).Scan(&total); err != nil {
		return JobListResult{}, fmt.Errorf("count admin jobs failed: %w", err)
	}

	listArgs := append([]interface{}{}, args...)
	limitIndex := len(listArgs) + 1
	offsetIndex := len(listArgs) + 2
	listArgs = append(listArgs, pageSize, (page-1)*pageSize)
	rows, err := c.db.QueryContext(ctx, fmt.Sprintf(`
		SELECT
			`+jobColumns+`
		FROM admin_jobs j
		%s
		ORDER BY created_at DESC
		LIMIT $%d OFFSET $%d
	`, where, limitIndex, offsetIndex), listArgs...)
	if err != nil {
		return JobListResult{}, fmt.Errorf("query admin jobs failed: %w", err)
	}
	defer rows.Close()

	items := []JobItem{}
	for rows.Next() {
		item, err := scanJobItem(rows)
		if err != nil {
			return JobListResult{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return JobListResult{}, fmt.Errorf("iterate admin jobs failed: %w", err)
	}
	return JobListResult{Items: items, Total: total}, nil
}

func (c *Client) GetJob(ctx context.Context, jobID int) (JobItem, error) {
	if c == nil || c.db == nil {
		return JobItem{}, errors.New("admin store is not initialized")
	}
	if jobID <= 0 {
		return JobItem{}, ErrJobNotFound
	}
	row := c.db.QueryRowContext(ctx, `
		SELECT
			`+jobColumns+`
		FROM admin_jobs
		WHERE id = $1
		LIMIT 1
	`, jobID)
	item, err := scanJobItem(row)
	if errors.Is(err, sql.ErrNoRows) {
		return JobItem{}, ErrJobNotFound
	}
	if err != nil {
		return JobItem{}, err
	}
	return item, nil
}

func (c *Client) ListJobLogs(ctx context.Context, jobID int, page int, pageSize int) (JobLogListResult, error) {
	if c == nil || c.db == nil {
		return JobLogListResult{}, errors.New("admin store is not initialized")
	}
	if jobID <= 0 {
		return JobLogListResult{}, ErrJobNotFound
	}
	var exists int
	if err := c.db.QueryRowContext(ctx, "SELECT id FROM admin_jobs WHERE id = $1 LIMIT 1", jobID).Scan(&exists); errors.Is(err, sql.ErrNoRows) {
		return JobLogListResult{}, ErrJobNotFound
	} else if err != nil {
		return JobLogListResult{}, fmt.Errorf("check admin job exists failed: %w", err)
	}
	page, pageSize = normalizeJobLogPagination(page, pageSize)

	var total int
	if err := c.db.QueryRowContext(ctx, `
		SELECT COUNT(*)
		FROM admin_logs
		WHERE resource = 'job' AND resource_id = $1
	`, fmt.Sprintf("%d", jobID)).Scan(&total); err != nil {
		return JobLogListResult{}, fmt.Errorf("count admin job logs failed: %w", err)
	}

	rows, err := c.db.QueryContext(ctx, `
		SELECT
			id,
			action,
			COALESCE(status, 'success'),
			error_message,
			trace_id,
			operator_id,
			created_at,
			details
		FROM admin_logs
		WHERE resource = 'job' AND resource_id = $1
		ORDER BY created_at DESC
		LIMIT $2 OFFSET $3
	`, fmt.Sprintf("%d", jobID), pageSize, (page-1)*pageSize)
	if err != nil {
		return JobLogListResult{}, fmt.Errorf("query admin job logs failed: %w", err)
	}
	defer rows.Close()

	items := []JobLogItem{}
	for rows.Next() {
		item, err := scanJobLogItem(rows)
		if err != nil {
			return JobLogListResult{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return JobLogListResult{}, fmt.Errorf("iterate admin job logs failed: %w", err)
	}
	return JobLogListResult{Items: items, Total: total}, nil
}

type jobRowScanner interface {
	Scan(dest ...interface{}) error
}

func scanJobItem(scanner jobRowScanner) (JobItem, error) {
	var item JobItem
	var tenantID sql.NullString
	var projectID sql.NullString
	var kbID sql.NullString
	var payload sql.NullString
	var result sql.NullString
	var errorMessage sql.NullString
	var requestedBy sql.NullInt64
	var traceID sql.NullString
	var startedAt sql.NullTime
	var finishedAt sql.NullTime
	var updatedAt sql.NullTime
	var targetsHash sql.NullString
	if err := scanner.Scan(
		&item.ID,
		&item.JobType,
		&item.Status,
		&tenantID,
		&projectID,
		&kbID,
		&payload,
		&result,
		&errorMessage,
		&item.RetryCount,
		&item.MaxRetries,
		&requestedBy,
		&traceID,
		&startedAt,
		&finishedAt,
		&item.CreatedAt,
		&updatedAt,
		&targetsHash,
	); err != nil {
		return JobItem{}, err
	}
	item.TenantID = stringPtrFromNull(tenantID)
	item.ProjectID = stringPtrFromNull(projectID)
	item.KBID = stringPtrFromNull(kbID)
	item.Payload = parseObjectJSONOrEmpty(stringPtrFromNull(payload))
	item.Result = parseObjectJSONOrNil(stringPtrFromNull(result))
	item.ErrorMessage = stringPtrFromNull(errorMessage)
	item.RequestedBy = intPtrFromNull(requestedBy)
	item.TraceID = stringPtrFromNull(traceID)
	item.TargetsHash = stringPtrFromNull(targetsHash)
	if startedAt.Valid {
		value := startedAt.Time
		item.StartedAt = &value
	}
	if finishedAt.Valid {
		value := finishedAt.Time
		item.FinishedAt = &value
	}
	if updatedAt.Valid {
		value := updatedAt.Time
		item.UpdatedAt = &value
	}
	return item, nil
}

func scanJobLogItem(scanner jobRowScanner) (JobLogItem, error) {
	var item JobLogItem
	var errorMessage sql.NullString
	var traceID sql.NullString
	var operatorID sql.NullInt64
	var details sql.NullString
	if err := scanner.Scan(
		&item.ID,
		&item.Action,
		&item.Status,
		&errorMessage,
		&traceID,
		&operatorID,
		&item.CreatedAt,
		&details,
	); err != nil {
		return JobLogItem{}, err
	}
	item.ErrorMessage = stringPtrFromNull(errorMessage)
	item.TraceID = stringPtrFromNull(traceID)
	item.OperatorID = intPtrFromNull(operatorID)
	item.Details = parseObjectJSONOrNil(stringPtrFromNull(details))
	return item, nil
}

func buildJobWhere(query JobListQuery) (string, []interface{}) {
	clauses := []string{}
	args := []interface{}{}
	if jobType := strings.TrimSpace(query.JobType); jobType != "" {
		args = append(args, jobType)
		clauses = append(clauses, fmt.Sprintf("j.job_type = $%d", len(args)))
	}
	if status := strings.TrimSpace(query.Status); status != "" {
		args = append(args, status)
		clauses = append(clauses, fmt.Sprintf("j.status = $%d", len(args)))
	}
	if tenantID := strings.TrimSpace(query.TenantID); tenantID != "" {
		args = append(args, tenantID)
		clauses = append(clauses, fmt.Sprintf("j.tenant_id = $%d", len(args)))
	}
	if projectID := strings.TrimSpace(query.ProjectID); projectID != "" {
		args = append(args, projectID)
		clauses = append(clauses, fmt.Sprintf("j.project_id = $%d", len(args)))
	}
	if kbID := strings.TrimSpace(query.KBID); kbID != "" {
		args = append(args, kbID)
		clauses = append(clauses, fmt.Sprintf("j.kb_id = $%d", len(args)))
	}
	if len(clauses) == 0 {
		return "", args
	}
	return " WHERE " + strings.Join(clauses, " AND "), args
}

func normalizeJobPagination(page int, pageSize int) (int, int) {
	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = 20
	}
	if pageSize > 200 {
		pageSize = 200
	}
	return page, pageSize
}

func normalizeJobLogPagination(page int, pageSize int) (int, int) {
	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = 50
	}
	if pageSize > 200 {
		pageSize = 200
	}
	return page, pageSize
}

func parseObjectJSONOrEmpty(value *string) map[string]interface{} {
	parsed := parseObjectJSONOrNil(value)
	if parsed == nil {
		return map[string]interface{}{}
	}
	return parsed
}

func parseObjectJSONOrNil(value *string) map[string]interface{} {
	if value == nil || strings.TrimSpace(*value) == "" {
		return nil
	}
	parsed := parseNullableJSON(value)
	if asMap, ok := parsed.(map[string]interface{}); ok {
		return asMap
	}
	if parsed != nil {
		return map[string]interface{}{"raw": parsed}
	}
	return map[string]interface{}{"raw": *value}
}

func validateJobCreateRequest(req JobCreateRequest) error {
	if _, ok := supportedJobTypes[req.JobType]; !ok {
		return ErrJobValidation
	}
	if req.MaxRetries < 0 || req.MaxRetries > 20 {
		return ErrJobValidation
	}
	for _, value := range []*string{req.TenantID, req.ProjectID, req.KBID} {
		if value != nil && len(strings.TrimSpace(*value)) > 100 {
			return ErrJobValidation
		}
	}
	return nil
}

func encodeJobObject(value map[string]interface{}) (string, error) {
	if value == nil {
		value = map[string]interface{}{}
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		return "", fmt.Errorf("encode admin job payload failed: %w", err)
	}
	return string(encoded), nil
}

func getJobForUpdate(ctx context.Context, tx *sql.Tx, jobID int) (JobItem, error) {
	row := tx.QueryRowContext(ctx, `
		SELECT
			`+jobColumns+`
		FROM admin_jobs
		WHERE id = $1
		LIMIT 1
		FOR UPDATE
	`, jobID)
	item, err := scanJobItem(row)
	if err != nil {
		return JobItem{}, err
	}
	return item, nil
}

func insertJobAuditLog(ctx context.Context, tx *sql.Tx, action string, job JobItem, operatorID *int, traceID *string, ipAddress *string, userAgent *string, details map[string]interface{}) error {
	encodedDetails, err := json.Marshal(details)
	if err != nil {
		return fmt.Errorf("encode job audit details failed: %w", err)
	}
	userID := operatorID
	if userID == nil {
		userID = job.RequestedBy
	}
	tenantID := job.TenantID
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO admin_logs (
			user_id,
			operator_id,
			tenant_id,
			trace_id,
			action,
			resource,
			resource_id,
			details,
			ip_address,
			user_agent,
			status
		)
		VALUES ($1, $1, $2, $3, $4, 'job', $5, $6, $7, $8, 'success')
	`, userID, tenantID, traceIDOrJobTrace(traceID, job.TraceID), action, fmt.Sprintf("%d", job.ID), string(encodedDetails), ipAddress, userAgent); err != nil {
		return fmt.Errorf("insert job audit log failed: %w", err)
	}
	return nil
}

func traceIDOrJobTrace(primary *string, fallback *string) *string {
	if primary != nil && strings.TrimSpace(*primary) != "" {
		return primary
	}
	return fallback
}
