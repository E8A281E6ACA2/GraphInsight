package adminstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

var ErrQATraceNotFound = errors.New("qa trace not found")

type QATraceItem struct {
	ID               int         `json:"id"`
	TraceID          *string     `json:"trace_id,omitempty"`
	QAType           string      `json:"qa_type"`
	Status           string      `json:"status"`
	Question         string      `json:"question"`
	OperatorID       *int        `json:"operator_id,omitempty"`
	TenantID         *string     `json:"tenant_id,omitempty"`
	ProjectID        *string     `json:"project_id,omitempty"`
	KBID             *string     `json:"kb_id,omitempty"`
	Model            *string     `json:"model,omitempty"`
	ReasoningProfile *string     `json:"reasoning_profile,omitempty"`
	TopK             *int        `json:"top_k,omitempty"`
	LatencyMS        *int        `json:"latency_ms,omitempty"`
	RetrievalCount   int         `json:"retrieval_count"`
	CitationCount    int         `json:"citation_count"`
	AnswerPreview    *string     `json:"answer_preview,omitempty"`
	ErrorMessage     *string     `json:"error_message,omitempty"`
	CreatedAt        time.Time   `json:"created_at"`
	Extra            interface{} `json:"-"`
}

type QATraceDetail struct {
	ID                 int         `json:"id"`
	TraceID            *string     `json:"trace_id,omitempty"`
	QAType             string      `json:"qa_type"`
	Status             string      `json:"status"`
	Question           string      `json:"question"`
	OperatorID         *int        `json:"operator_id,omitempty"`
	TenantID           *string     `json:"tenant_id,omitempty"`
	ProjectID          *string     `json:"project_id,omitempty"`
	KBID               *string     `json:"kb_id,omitempty"`
	Model              *string     `json:"model,omitempty"`
	TopK               *int        `json:"top_k,omitempty"`
	LatencyMS          *int        `json:"latency_ms,omitempty"`
	RetrievalCount     int         `json:"retrieval_count"`
	CitationCount      int         `json:"citation_count"`
	AnswerPreview      *string     `json:"answer_preview,omitempty"`
	ErrorMessage       *string     `json:"error_message,omitempty"`
	CreatedAt          time.Time   `json:"created_at"`
	RetrievalSnapshot  interface{} `json:"retrieval_snapshot,omitempty"`
	GenerationSnapshot interface{} `json:"generation_snapshot,omitempty"`
	ResponseSnapshot   interface{} `json:"response_snapshot,omitempty"`
}

type QATraceListQuery struct {
	QAType     string
	Status     string
	TraceID    string
	OperatorID *int
	Keyword    string
	KBID       string
	Page       int
	PageSize   int
}

type QATraceListResult struct {
	Items []QATraceItem
	Total int
}

type QACostModelBreakdown struct {
	Model            string  `json:"model"`
	QAType           string  `json:"qa_type"`
	Calls            int     `json:"calls"`
	PromptTokens     int     `json:"prompt_tokens"`
	CompletionTokens int     `json:"completion_tokens"`
	TotalTokens      int     `json:"total_tokens"`
	EstimatedCost    float64 `json:"estimated_cost"`
	AvgLatencyMS     float64 `json:"avg_latency_ms"`
	SuccessRate      float64 `json:"success_rate"`
}

type QACostSummary struct {
	WindowHours      int                    `json:"window_hours"`
	TotalCalls       int                    `json:"total_calls"`
	SuccessCalls     int                    `json:"success_calls"`
	FailedCalls      int                    `json:"failed_calls"`
	SuccessRate      float64                `json:"success_rate"`
	PromptTokens     int                    `json:"prompt_tokens"`
	CompletionTokens int                    `json:"completion_tokens"`
	TotalTokens      int                    `json:"total_tokens"`
	EstimatedCost    float64                `json:"estimated_cost"`
	Currency         string                 `json:"currency"`
	PricingSource    string                 `json:"pricing_source"`
	Models           []QACostModelBreakdown `json:"models"`
}

type QACostSummaryQuery struct {
	QAType      string
	Status      string
	KBID        string
	WindowHours int
}

func (c *Client) ListQATraces(ctx context.Context, query QATraceListQuery) (QATraceListResult, error) {
	if c == nil || c.db == nil {
		return QATraceListResult{}, errors.New("admin store is not initialized")
	}
	page, pageSize := normalizeQATracePagination(query.Page, query.PageSize)
	query.Page = page
	query.PageSize = pageSize
	where, args := buildQATraceWhere(query)

	var total int
	if err := c.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM admin_qa_traces q"+where, args...).Scan(&total); err != nil {
		return QATraceListResult{}, fmt.Errorf("count qa traces failed: %w", err)
	}

	listArgs := append([]interface{}{}, args...)
	limitIndex := len(listArgs) + 1
	offsetIndex := len(listArgs) + 2
	listArgs = append(listArgs, pageSize, (page-1)*pageSize)
	rows, err := c.db.QueryContext(ctx, fmt.Sprintf(`
		SELECT
			id,
			trace_id,
			qa_type,
			status,
			question,
			operator_id,
			tenant_id,
			project_id,
			kb_id,
			model,
			generation_snapshot,
			top_k,
			latency_ms,
			retrieval_count,
			citation_count,
			answer_preview,
			error_message,
			created_at
		FROM admin_qa_traces q
		%s
		ORDER BY created_at DESC
		LIMIT $%d OFFSET $%d
	`, where, limitIndex, offsetIndex), listArgs...)
	if err != nil {
		return QATraceListResult{}, fmt.Errorf("query qa traces failed: %w", err)
	}
	defer rows.Close()

	items := []QATraceItem{}
	for rows.Next() {
		item, err := scanQATraceItem(rows)
		if err != nil {
			return QATraceListResult{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return QATraceListResult{}, fmt.Errorf("iterate qa traces failed: %w", err)
	}
	return QATraceListResult{Items: items, Total: total}, nil
}

func (c *Client) GetQATrace(ctx context.Context, traceIDOrPK string, kbID string) (QATraceDetail, error) {
	if c == nil || c.db == nil {
		return QATraceDetail{}, errors.New("admin store is not initialized")
	}
	traceIDOrPK = strings.TrimSpace(traceIDOrPK)
	if traceIDOrPK == "" {
		return QATraceDetail{}, ErrQATraceNotFound
	}

	clauses := []string{}
	args := []interface{}{}
	if id, err := strconv.Atoi(traceIDOrPK); err == nil {
		args = append(args, id)
		clauses = append(clauses, fmt.Sprintf("id = $%d", len(args)))
	} else {
		args = append(args, traceIDOrPK)
		clauses = append(clauses, fmt.Sprintf("trace_id = $%d", len(args)))
	}
	// KB 作用域强制（M4-R1 审计 P0-2）：非空 kb_id 时只允许读取该 KB 下的 trace，
	// 跨 KB 读取自然落空（ErrQATraceNotFound -> 404），避免全局可读。
	if scopedKB := strings.TrimSpace(kbID); scopedKB != "" {
		args = append(args, scopedKB)
		clauses = append(clauses, fmt.Sprintf("kb_id = $%d", len(args)))
	}
	where := strings.Join(clauses, " AND ")

	row := c.db.QueryRowContext(ctx, fmt.Sprintf(`
		SELECT
			id,
			trace_id,
			qa_type,
			status,
			question,
			operator_id,
			tenant_id,
			project_id,
			kb_id,
			model,
			top_k,
			latency_ms,
			retrieval_count,
			citation_count,
			answer_preview,
			error_message,
			created_at,
			retrieval_snapshot,
			generation_snapshot,
			response_snapshot
		FROM admin_qa_traces
		WHERE %s
		LIMIT 1
	`, where), args...)
	detail, err := scanQATraceDetail(row)
	if errors.Is(err, sql.ErrNoRows) {
		return QATraceDetail{}, ErrQATraceNotFound
	}
	if err != nil {
		return QATraceDetail{}, err
	}
	return detail, nil
}

func (c *Client) GetQACostSummary(ctx context.Context, query QACostSummaryQuery) (QACostSummary, error) {
	if c == nil || c.db == nil {
		return QACostSummary{}, errors.New("admin store is not initialized")
	}
	windowHours := query.WindowHours
	if windowHours < 1 {
		windowHours = 24
	}
	if windowHours > 24*90 {
		windowHours = 24 * 90
	}
	since := time.Now().UTC().Add(-time.Duration(windowHours) * time.Hour)
	filters := QATraceListQuery{
		QAType: query.QAType,
		Status: query.Status,
		KBID:   query.KBID,
	}
	where, args := buildQATraceWhere(filters)
	args = append(args, since)
	if where == "" {
		where = fmt.Sprintf(" WHERE q.created_at >= $%d", len(args))
	} else {
		where += fmt.Sprintf(" AND q.created_at >= $%d", len(args))
	}

	rows, err := c.db.QueryContext(ctx, `
		SELECT
			q.qa_type,
			q.status,
			q.model,
			q.latency_ms,
			q.generation_snapshot
		FROM admin_qa_traces q
	`+where, args...)
	if err != nil {
		return QACostSummary{}, fmt.Errorf("query qa cost summary failed: %w", err)
	}
	defer rows.Close()

	summary := QACostSummary{
		WindowHours:   windowHours,
		Currency:      "USD",
		PricingSource: "not_configured",
		Models:        []QACostModelBreakdown{},
	}
	pricing := loadQACostPricing()
	summary.Currency = pricing.Currency
	summary.PricingSource = pricing.Source
	buckets := map[string]*qaCostBucket{}
	for rows.Next() {
		var qaType string
		var status string
		var model sql.NullString
		var latency sql.NullInt64
		var generationSnapshot sql.NullString
		if err := rows.Scan(&qaType, &status, &model, &latency, &generationSnapshot); err != nil {
			return QACostSummary{}, fmt.Errorf("scan qa cost summary failed: %w", err)
		}
		modelName := "unknown"
		if model.Valid && strings.TrimSpace(model.String) != "" {
			modelName = model.String
		}
		usage := extractQATokenUsage(stringPtrFromNull(generationSnapshot))
		summary.TotalCalls++
		if status == "success" {
			summary.SuccessCalls++
		}
		summary.PromptTokens += usage.PromptTokens
		summary.CompletionTokens += usage.CompletionTokens
		summary.TotalTokens += usage.TotalTokens
		cost := estimateQACost(pricing.Models, modelName, usage.PromptTokens, usage.CompletionTokens)
		summary.EstimatedCost += cost

		key := modelName + "\x00" + qaType
		bucket := buckets[key]
		if bucket == nil {
			bucket = &qaCostBucket{Model: modelName, QAType: qaType}
			buckets[key] = bucket
		}
		bucket.Calls++
		if status == "success" {
			bucket.SuccessCalls++
		}
		bucket.PromptTokens += usage.PromptTokens
		bucket.CompletionTokens += usage.CompletionTokens
		bucket.TotalTokens += usage.TotalTokens
		bucket.EstimatedCost += cost
		if latency.Valid {
			bucket.LatencyMS = append(bucket.LatencyMS, maxInt(0, int(latency.Int64)))
		}
	}
	if err := rows.Err(); err != nil {
		return QACostSummary{}, fmt.Errorf("iterate qa cost summary failed: %w", err)
	}
	summary.FailedCalls = summary.TotalCalls - summary.SuccessCalls
	if summary.TotalCalls > 0 {
		summary.SuccessRate = roundFloat(float64(summary.SuccessCalls)/float64(summary.TotalCalls), 6)
	}
	for _, bucket := range buckets {
		model := QACostModelBreakdown{
			Model:            bucket.Model,
			QAType:           bucket.QAType,
			Calls:            bucket.Calls,
			PromptTokens:     bucket.PromptTokens,
			CompletionTokens: bucket.CompletionTokens,
			TotalTokens:      bucket.TotalTokens,
			EstimatedCost:    roundFloat(bucket.EstimatedCost, 6),
			AvgLatencyMS:     averageInts(bucket.LatencyMS),
			SuccessRate:      roundFloat(float64(bucket.SuccessCalls)/float64(maxInt(1, bucket.Calls)), 6),
		}
		summary.Models = append(summary.Models, model)
	}
	summary.EstimatedCost = roundFloat(summary.EstimatedCost, 6)
	sort.Slice(summary.Models, func(i, j int) bool {
		left := summary.Models[i]
		right := summary.Models[j]
		if left.EstimatedCost != right.EstimatedCost {
			return left.EstimatedCost > right.EstimatedCost
		}
		if left.TotalTokens != right.TotalTokens {
			return left.TotalTokens > right.TotalTokens
		}
		return left.Calls > right.Calls
	})
	return summary, nil
}

type qaCostPricing struct {
	Currency string
	Source   string
	Models   map[string]map[string]float64
}

type qaCostBucket struct {
	Model            string
	QAType           string
	Calls            int
	SuccessCalls     int
	PromptTokens     int
	CompletionTokens int
	TotalTokens      int
	EstimatedCost    float64
	LatencyMS        []int
}

func loadQACostPricing() qaCostPricing {
	raw := strings.TrimSpace(os.Getenv("AI_COST_MODEL_PRICING_JSON"))
	currency := strings.TrimSpace(os.Getenv("AI_COST_CURRENCY"))
	if currency == "" {
		currency = "USD"
	}
	if raw == "" {
		return qaCostPricing{
			Currency: currency,
			Source:   "not_configured",
			Models:   map[string]map[string]float64{},
		}
	}

	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
		return qaCostPricing{
			Currency: currency,
			Source:   "invalid_config",
			Models:   map[string]map[string]float64{},
		}
	}

	modelsRaw := parsed
	if nested, ok := parsed["models"].(map[string]interface{}); ok {
		modelsRaw = nested
	}
	models := make(map[string]map[string]float64, len(modelsRaw))
	for model, rawPrice := range modelsRaw {
		priceMap, ok := rawPrice.(map[string]interface{})
		if !ok {
			continue
		}
		models[model] = map[string]float64{
			"prompt_per_1k":     safeFloat(priceMap["prompt_per_1k"]),
			"completion_per_1k": safeFloat(priceMap["completion_per_1k"]),
			"input_per_1k":      safeFloat(priceMap["input_per_1k"]),
			"output_per_1k":     safeFloat(priceMap["output_per_1k"]),
		}
	}
	if configuredCurrency := strings.TrimSpace(asString(parsed["currency"])); configuredCurrency != "" {
		currency = configuredCurrency
	}
	return qaCostPricing{
		Currency: currency,
		Source:   "env",
		Models:   models,
	}
}

func estimateQACost(models map[string]map[string]float64, model string, promptTokens int, completionTokens int) float64 {
	price := models[model]
	if price == nil {
		price = models["*"]
	}
	if price == nil {
		return 0
	}
	promptPer1K := price["prompt_per_1k"]
	if promptPer1K == 0 {
		promptPer1K = price["input_per_1k"]
	}
	completionPer1K := price["completion_per_1k"]
	if completionPer1K == 0 {
		completionPer1K = price["output_per_1k"]
	}
	return (float64(promptTokens)/1000.0)*promptPer1K + (float64(completionTokens)/1000.0)*completionPer1K
}

func safeFloat(value interface{}) float64 {
	switch typed := value.(type) {
	case float64:
		return typed
	case float32:
		return float64(typed)
	case int:
		return float64(typed)
	case int64:
		return float64(typed)
	case json.Number:
		parsed, err := typed.Float64()
		if err == nil {
			return parsed
		}
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(typed), 64)
		if err == nil {
			return parsed
		}
	}
	return 0
}

func asString(value interface{}) string {
	if value == nil {
		return ""
	}
	if str, ok := value.(string); ok {
		return str
	}
	return fmt.Sprint(value)
}

func buildQATraceWhere(query QATraceListQuery) (string, []interface{}) {
	clauses := []string{}
	args := []interface{}{}
	if qaType := strings.TrimSpace(query.QAType); qaType != "" {
		args = append(args, qaType)
		clauses = append(clauses, fmt.Sprintf("q.qa_type = $%d", len(args)))
	}
	if status := strings.TrimSpace(query.Status); status != "" {
		args = append(args, status)
		clauses = append(clauses, fmt.Sprintf("q.status = $%d", len(args)))
	}
	if traceID := strings.TrimSpace(query.TraceID); traceID != "" {
		args = append(args, traceID)
		clauses = append(clauses, fmt.Sprintf("q.trace_id = $%d", len(args)))
	}
	if query.OperatorID != nil {
		args = append(args, *query.OperatorID)
		clauses = append(clauses, fmt.Sprintf("q.operator_id = $%d", len(args)))
	}
	if keyword := strings.TrimSpace(query.Keyword); keyword != "" {
		args = append(args, "%"+keyword+"%")
		idx := len(args)
		clauses = append(clauses, fmt.Sprintf("(q.question ILIKE $%d OR q.answer_preview ILIKE $%d)", idx, idx))
	}
	// KB 作用域强制（M4-R1 审计 P0-2）：非空 kb_id 时按知识库归属过滤。
	if kbID := strings.TrimSpace(query.KBID); kbID != "" {
		args = append(args, kbID)
		clauses = append(clauses, fmt.Sprintf("q.kb_id = $%d", len(args)))
	}
	if len(clauses) == 0 {
		return "", args
	}
	return " WHERE " + strings.Join(clauses, " AND "), args
}

func normalizeQATracePagination(page int, pageSize int) (int, int) {
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

type qaTraceRowScanner interface {
	Scan(dest ...interface{}) error
}

func scanQATraceItem(scanner qaTraceRowScanner) (QATraceItem, error) {
	var item QATraceItem
	var traceID sql.NullString
	var operatorID sql.NullInt64
	var tenantID sql.NullString
	var projectID sql.NullString
	var kbID sql.NullString
	var model sql.NullString
	var generationSnapshot sql.NullString
	var topK sql.NullInt64
	var latencyMS sql.NullInt64
	var answerPreview sql.NullString
	var errorMessage sql.NullString
	if err := scanner.Scan(
		&item.ID,
		&traceID,
		&item.QAType,
		&item.Status,
		&item.Question,
		&operatorID,
		&tenantID,
		&projectID,
		&kbID,
		&model,
		&generationSnapshot,
		&topK,
		&latencyMS,
		&item.RetrievalCount,
		&item.CitationCount,
		&answerPreview,
		&errorMessage,
		&item.CreatedAt,
	); err != nil {
		return QATraceItem{}, err
	}
	item.TraceID = stringPtrFromNull(traceID)
	item.OperatorID = intPtrFromNull(operatorID)
	item.TenantID = stringPtrFromNull(tenantID)
	item.ProjectID = stringPtrFromNull(projectID)
	item.KBID = stringPtrFromNull(kbID)
	item.Model = stringPtrFromNull(model)
	item.ReasoningProfile = extractQAReasoningProfile(stringPtrFromNull(generationSnapshot))
	item.TopK = intPtrFromNull(topK)
	item.LatencyMS = intPtrFromNull(latencyMS)
	item.AnswerPreview = stringPtrFromNull(answerPreview)
	item.ErrorMessage = stringPtrFromNull(errorMessage)
	return item, nil
}

func scanQATraceDetail(scanner qaTraceRowScanner) (QATraceDetail, error) {
	var item QATraceItem
	var traceID sql.NullString
	var operatorID sql.NullInt64
	var tenantID sql.NullString
	var projectID sql.NullString
	var kbID sql.NullString
	var model sql.NullString
	var topK sql.NullInt64
	var latencyMS sql.NullInt64
	var answerPreview sql.NullString
	var errorMessage sql.NullString
	var retrievalSnapshot sql.NullString
	var generationSnapshot sql.NullString
	var responseSnapshot sql.NullString
	if err := scanner.Scan(
		&item.ID,
		&traceID,
		&item.QAType,
		&item.Status,
		&item.Question,
		&operatorID,
		&tenantID,
		&projectID,
		&kbID,
		&model,
		&topK,
		&latencyMS,
		&item.RetrievalCount,
		&item.CitationCount,
		&answerPreview,
		&errorMessage,
		&item.CreatedAt,
		&retrievalSnapshot,
		&generationSnapshot,
		&responseSnapshot,
	); err != nil {
		return QATraceDetail{}, err
	}
	item.TraceID = stringPtrFromNull(traceID)
	item.OperatorID = intPtrFromNull(operatorID)
	item.TenantID = stringPtrFromNull(tenantID)
	item.ProjectID = stringPtrFromNull(projectID)
	item.KBID = stringPtrFromNull(kbID)
	item.Model = stringPtrFromNull(model)
	item.TopK = intPtrFromNull(topK)
	item.LatencyMS = intPtrFromNull(latencyMS)
	item.AnswerPreview = stringPtrFromNull(answerPreview)
	item.ErrorMessage = stringPtrFromNull(errorMessage)
	return QATraceDetail{
		ID:                 item.ID,
		TraceID:            item.TraceID,
		QAType:             item.QAType,
		Status:             item.Status,
		Question:           item.Question,
		OperatorID:         item.OperatorID,
		TenantID:           item.TenantID,
		ProjectID:          item.ProjectID,
		KBID:               item.KBID,
		Model:              item.Model,
		TopK:               item.TopK,
		LatencyMS:          item.LatencyMS,
		RetrievalCount:     item.RetrievalCount,
		CitationCount:      item.CitationCount,
		AnswerPreview:      item.AnswerPreview,
		ErrorMessage:       item.ErrorMessage,
		CreatedAt:          item.CreatedAt,
		RetrievalSnapshot:  parseNullableJSON(stringPtrFromNull(retrievalSnapshot)),
		GenerationSnapshot: parseNullableJSON(stringPtrFromNull(generationSnapshot)),
		ResponseSnapshot:   parseNullableJSON(stringPtrFromNull(responseSnapshot)),
	}, nil
}

type qaTokenUsage struct {
	PromptTokens     int
	CompletionTokens int
	TotalTokens      int
}

func extractQATokenUsage(generationSnapshot *string) qaTokenUsage {
	parsed := parseNullableJSON(generationSnapshot)
	asMap, ok := parsed.(map[string]interface{})
	if !ok {
		return qaTokenUsage{}
	}
	usage, ok := asMap["usage"].(map[string]interface{})
	if !ok {
		return qaTokenUsage{}
	}
	prompt := nonNegativeInt(usage["prompt_tokens"])
	completion := nonNegativeInt(usage["completion_tokens"])
	total := nonNegativeInt(usage["total_tokens"])
	if total == 0 {
		total = prompt + completion
	}
	return qaTokenUsage{
		PromptTokens:     prompt,
		CompletionTokens: completion,
		TotalTokens:      total,
	}
}

func extractQAReasoningProfile(generationSnapshot *string) *string {
	parsed := parseNullableJSON(generationSnapshot)
	asMap, ok := parsed.(map[string]interface{})
	if !ok {
		return nil
	}
	raw, ok := asMap["reasoning_profile"]
	if !ok {
		return nil
	}
	profile := strings.TrimSpace(fmt.Sprintf("%v", raw))
	if profile == "" {
		return nil
	}
	return &profile
}

func nonNegativeInt(value interface{}) int {
	switch typed := value.(type) {
	case float64:
		return maxInt(0, int(typed))
	case int:
		return maxInt(0, typed)
	case string:
		parsed, err := strconv.Atoi(typed)
		if err != nil {
			return 0
		}
		return maxInt(0, parsed)
	default:
		return 0
	}
}

func averageInts(values []int) float64 {
	if len(values) == 0 {
		return 0
	}
	total := 0
	for _, value := range values {
		total += value
	}
	return roundFloat(float64(total)/float64(len(values)), 3)
}

func roundFloat(value float64, places int) float64 {
	if places <= 0 {
		return value
	}
	multiplier := 1.0
	for i := 0; i < places; i++ {
		multiplier *= 10
	}
	if value >= 0 {
		return float64(int(value*multiplier+0.5)) / multiplier
	}
	return float64(int(value*multiplier-0.5)) / multiplier
}

func maxInt(a int, b int) int {
	if a > b {
		return a
	}
	return b
}
