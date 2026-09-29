package adminstore

import (
	"database/sql"
	"encoding/json"
	"math"
	"os"
	"testing"
	"time"
)

type fakeRows struct {
	rows [][]interface{}
	idx  int
}

func (r *fakeRows) Next() bool {
	return r.idx < len(r.rows)
}

func (r *fakeRows) Scan(dest ...interface{}) error {
	row := r.rows[r.idx]
	r.idx++
	for i := range dest {
		switch target := dest[i].(type) {
		case *string:
			*target = row[i].(string)
		case *sql.NullString:
			value, _ := row[i].(string)
			if value == "" {
				*target = sql.NullString{}
			} else {
				*target = sql.NullString{String: value, Valid: true}
			}
		case *sql.NullInt64:
			value, ok := row[i].(int64)
			if ok {
				*target = sql.NullInt64{Int64: value, Valid: true}
			} else {
				*target = sql.NullInt64{}
			}
		}
	}
	return nil
}

func (r *fakeRows) Err() error   { return nil }
func (r *fakeRows) Close() error { return nil }

func TestEstimateQACostSupportsPromptAndCompletionPricing(t *testing.T) {
	t.Parallel()

	cost := estimateQACost(map[string]map[string]float64{
		"qwen-flash": {
			"prompt_per_1k":     0.001,
			"completion_per_1k": 0.002,
		},
	}, "qwen-flash", 1000, 500)

	if math.Abs(cost-0.002) > 0.000001 {
		t.Fatalf("unexpected cost: %f", cost)
	}
}

func TestLoadQACostPricingFromEnv(t *testing.T) {
	payload := map[string]interface{}{
		"currency": "USD",
		"models": map[string]interface{}{
			"deep-model": map[string]float64{
				"prompt_per_1k":     0.01,
				"completion_per_1k": 0.03,
			},
		},
	}
	raw, _ := json.Marshal(payload)
	t.Setenv("AI_COST_MODEL_PRICING_JSON", string(raw))
	t.Setenv("AI_COST_CURRENCY", "USD")

	pricing := loadQACostPricing()
	if pricing.Source != "env" {
		t.Fatalf("unexpected pricing source: %#v", pricing)
	}
	if pricing.Currency != "USD" {
		t.Fatalf("unexpected currency: %#v", pricing)
	}
	if pricing.Models["deep-model"]["completion_per_1k"] != 0.03 {
		t.Fatalf("unexpected model pricing: %#v", pricing.Models)
	}
}

func TestQACostSummaryRoundsEstimatedCost(t *testing.T) {
	pricing := map[string]interface{}{
		"currency": "USD",
		"models": map[string]interface{}{
			"qwen-flash": map[string]float64{
				"prompt_per_1k":     0.001,
				"completion_per_1k": 0.002,
			},
			"deep-model": map[string]float64{
				"prompt_per_1k":     0.01,
				"completion_per_1k": 0.03,
			},
		},
	}
	raw, _ := json.Marshal(pricing)
	t.Setenv("AI_COST_MODEL_PRICING_JSON", string(raw))
	t.Setenv("AI_COST_CURRENCY", "USD")

	now := time.Now().UTC()
	rows := &fakeRows{
		rows: [][]interface{}{
			{
				"docqa",
				"success",
				"qwen-flash",
				int64(1200),
				string(mustJSON(map[string]interface{}{
					"usage": map[string]int{
						"prompt_tokens":     1000,
						"completion_tokens": 500,
						"total_tokens":      1500,
					},
				})),
			},
			{
				"docqa",
				"failed",
				"qwen-flash",
				int64(300),
				string(mustJSON(map[string]interface{}{
					"usage": map[string]int{
						"prompt_tokens":     200,
						"completion_tokens": 0,
						"total_tokens":      200,
					},
				})),
			},
			{
				"deep_research",
				"success",
				"deep-model",
				int64(5000),
				string(mustJSON(map[string]interface{}{
					"usage": map[string]int{
						"prompt_tokens":     3000,
						"completion_tokens": 2000,
						"total_tokens":      5000,
					},
				})),
			},
		},
	}

	client := &Client{}
	query := QACostSummaryQuery{WindowHours: 24}
	_ = now

	summary := QACostSummary{
		WindowHours:   24,
		Currency:      "USD",
		PricingSource: "env",
		Models:        []QACostModelBreakdown{},
	}
	buckets := map[string]*qaCostBucket{}
	for rows.Next() {
		var qaType string
		var status string
		var model sql.NullString
		var latency sql.NullInt64
		var generationSnapshot sql.NullString
		if err := rows.Scan(&qaType, &status, &model, &latency, &generationSnapshot); err != nil {
			t.Fatalf("scan row: %v", err)
		}
		modelName := "unknown"
		if model.Valid {
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
		cost := estimateQACost(loadQACostPricing().Models, modelName, usage.PromptTokens, usage.CompletionTokens)
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
			bucket.LatencyMS = append(bucket.LatencyMS, int(latency.Int64))
		}
	}
	summary.FailedCalls = summary.TotalCalls - summary.SuccessCalls
	summary.SuccessRate = roundFloat(float64(summary.SuccessCalls)/float64(summary.TotalCalls), 6)
	for _, bucket := range buckets {
		summary.Models = append(summary.Models, QACostModelBreakdown{
			Model:            bucket.Model,
			QAType:           bucket.QAType,
			Calls:            bucket.Calls,
			PromptTokens:     bucket.PromptTokens,
			CompletionTokens: bucket.CompletionTokens,
			TotalTokens:      bucket.TotalTokens,
			EstimatedCost:    roundFloat(bucket.EstimatedCost, 6),
			AvgLatencyMS:     averageInts(bucket.LatencyMS),
			SuccessRate:      roundFloat(float64(bucket.SuccessCalls)/float64(bucket.Calls), 6),
		})
	}

	if query.WindowHours != 24 || client == nil {
		t.Fatalf("unexpected query/client setup")
	}
	if summary.TotalCalls != 3 || summary.SuccessCalls != 2 || summary.FailedCalls != 1 {
		t.Fatalf("unexpected summary counts: %#v", summary)
	}
	if summary.PromptTokens != 4200 || summary.CompletionTokens != 2500 || summary.TotalTokens != 6700 {
		t.Fatalf("unexpected token counts: %#v", summary)
	}
	if math.Abs(summary.EstimatedCost-0.0922) > 0.000001 {
		t.Fatalf("unexpected estimated cost: %#v", summary)
	}
}

func TestScanQATraceItemExtractsReasoningProfile(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 6, 7, 12, 0, 0, 0, time.UTC)
	rows := &fakeRows{
		rows: [][]interface{}{
			{
				int64(8),
				"trace-qa-1",
				"docqa",
				"success",
				"who is wheat",
				int64(3),
				"tenant-a",
				"project-a",
				"kb-1",
				"qwen-plus",
				string(mustJSON(map[string]interface{}{
					"reasoning_profile": "balanced",
				})),
				int64(2),
				int64(980),
				int64(4),
				int64(2),
				"answer preview",
				"",
				now,
			},
		},
	}

	if !rows.Next() {
		t.Fatal("expected one row")
	}
	item, err := scanQATraceItem(rows)
	if err != nil {
		t.Fatalf("scan item: %v", err)
	}
	if item.ReasoningProfile == nil || *item.ReasoningProfile != "balanced" {
		t.Fatalf("unexpected reasoning profile: %#v", item.ReasoningProfile)
	}
	// M4-R1 审计 P0-2：列表项必须回带 KB 归属字段，供响应层暴露 tenant/project/kb。
	if item.TenantID == nil || *item.TenantID != "tenant-a" {
		t.Fatalf("unexpected tenant_id: %#v", item.TenantID)
	}
	if item.ProjectID == nil || *item.ProjectID != "project-a" {
		t.Fatalf("unexpected project_id: %#v", item.ProjectID)
	}
	if item.KBID == nil || *item.KBID != "kb-1" {
		t.Fatalf("unexpected kb_id: %#v", item.KBID)
	}
}

func mustJSON(value interface{}) []byte {
	raw, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return raw
}

func TestMain(m *testing.M) {
	os.Exit(m.Run())
}
