package adminstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"
)

// ReindexChunksJobType 是 §16.3 去重语义唯一的 job_type。
// 它刻意不出现在 supportedJobTypes 里：CreateJob 是裸 INSERT，不做去重，
// 让 reindex_chunks 走那条路等于把"同批 targets 重复入队"重新放开。
const ReindexChunksJobType = "reindex_chunks"

const (
	ReindexOutcomeCreated  = "created"
	ReindexOutcomeReused   = "reused"
	ReindexOutcomeRetried  = "retried"
	ReindexOutcomeReset    = "reset"
	ReindexOutcomeRejected = "rejected"
)

const (
	ReindexAuditActionCreated  = "job_created"
	ReindexAuditActionReused   = "job_reused"
	ReindexAuditActionRejected = "kb_chunk_reindex_failed"
)

const reindexRejectReasonRetryExhausted = "retry_exhausted"

var (
	ErrReindexScopeRequired  = errors.New("reindex_chunks targets scope required")
	ErrReindexEnqueueAnomaly = errors.New("reindex_chunks hit dedupe index but existing row is gone")
)

// ReindexTarget 是一条重建目标。kb_id/chunk_id 必填、target_revision >= 1（§8.1 无隐式扩范围）。
type ReindexTarget struct {
	KBID           string
	DocID          string
	TenantID       string
	ProjectID      string
	ChunkID        string
	TargetRevision int
}

type ReindexEnqueueRequest struct {
	Targets    []ReindexTarget
	Source     string
	TraceID    string
	MaxRetries int
	OperatorID *int
	IPAddress  *string
	UserAgent  *string
}

// ReindexEnqueueEntry 是一个 (kb_id, doc_id) 分组的结果；Job 是本次提交锁到的那一行，
// entry.Job.ID 即 §16.3 的 child_job_id（复用/重试/复位时它是既有行，不是新行）。
type ReindexEnqueueEntry struct {
	KBID        string
	DocID       string
	TargetsHash string
	TargetCount int
	Outcome     string
	Job         JobItem
}

type ReindexRejectedDetail struct {
	ChildJobID  *int     `json:"child_job_id"`
	KBID        string   `json:"kb_id"`
	DocID       string   `json:"doc_id"`
	TargetsHash string   `json:"targets_hash"`
	Reason      string   `json:"reason"`
	RetryCount  int      `json:"retry_count"`
	MaxRetries  int      `json:"max_retries"`
	ChunkIDs    []string `json:"chunk_ids"`
}

type ReindexEnqueueReport struct {
	Created        int
	Reused         int
	Retried        int
	Reset          int
	Rejected       int
	Targets        int
	Jobs           []ReindexEnqueueEntry
	RejectedDetail []ReindexRejectedDetail
}

// EnqueueReindexChunks 是 Go 控制面的 §16.3 提交链：唯一索引去重 → 锁后按状态分支
// → 原地 retry/reset → child ID 回读 → 同事务审计留痕。
//
// 与 backend/services/reindex_queue.py:enqueue_on_connection 同一张分支表；差异只有两处，
// 都是刻意的：
//  1. Go 侧只有 Postgres（$N 占位 + FOR UPDATE），不需要 SQLite 方言分支；
//  2. "命中去重索引却读不到既有行"直接回滚报错，不像 Python 那样先计 reused 再由调用层抛错。
//
// 审计必须在整批分支跑完后统一写，才能带出与 Python 同形状的报告级聚合计数。
func (c *Client) EnqueueReindexChunks(ctx context.Context, req ReindexEnqueueRequest) (ReindexEnqueueReport, error) {
	report := ReindexEnqueueReport{Jobs: []ReindexEnqueueEntry{}, RejectedDetail: []ReindexRejectedDetail{}}
	if c == nil || c.db == nil {
		return report, errors.New("admin store is not initialized")
	}
	groups, err := groupReindexTargets(req.Targets)
	if err != nil {
		return report, err
	}
	if req.MaxRetries < 0 || req.MaxRetries > 20 {
		return report, ErrJobValidation
	}
	source := strings.TrimSpace(req.Source)
	if source == "" {
		source = "admin_api"
	}

	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return report, fmt.Errorf("begin reindex_chunks enqueue transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	for _, key := range sortedReindexGroupKeys(groups) {
		group := groups[key]
		canonical := make([]CanonicalTarget, 0, len(group))
		chunkIDs := make([]string, 0, len(group))
		for _, target := range group {
			canonical = append(canonical, CanonicalTarget{ChunkID: target.ChunkID, TargetRevision: target.TargetRevision})
			chunkIDs = append(chunkIDs, target.ChunkID)
		}
		sort.SliceStable(canonical, func(i, j int) bool {
			if canonical[i].ChunkID != canonical[j].ChunkID {
				return canonical[i].ChunkID < canonical[j].ChunkID
			}
			return canonical[i].TargetRevision < canonical[j].TargetRevision
		})
		targetsHash := CanonicalTargetsHash(canonical)
		entry := ReindexEnqueueEntry{KBID: key.KBID, DocID: key.DocID, TargetsHash: targetsHash, TargetCount: len(canonical)}

		created, err := insertReindexJob(ctx, tx, req, key, group, canonical, targetsHash)
		if err != nil {
			return report, err
		}
		if created != nil {
			entry.Outcome = ReindexOutcomeCreated
			entry.Job = *created
			report.Created++
		} else {
			outcome, item, err := resolveReindexConflict(ctx, tx, req, key, targetsHash)
			if err != nil {
				return report, err
			}
			entry.Outcome = outcome
			entry.Job = item
			if outcome == ReindexOutcomeRejected {
				report.Rejected++
				retryCount, maxRetries := 0, req.MaxRetries
				if item.ID > 0 {
					retryCount, maxRetries = item.RetryCount, item.MaxRetries
				}
				report.RejectedDetail = append(report.RejectedDetail, ReindexRejectedDetail{
					ChildJobID:  optionalInt(entry.Job.ID),
					KBID:        key.KBID,
					DocID:       key.DocID,
					TargetsHash: targetsHash,
					Reason:      reindexRejectReasonRetryExhausted,
					RetryCount:  retryCount,
					MaxRetries:  maxRetries,
					ChunkIDs:    chunkIDs,
				})
			} else {
				report.increment(outcome)
			}
		}
		report.Targets += entry.TargetCount
		report.Jobs = append(report.Jobs, entry)
	}

	for index := range report.Jobs {
		if err := writeReindexAudit(ctx, tx, req, &report, &report.Jobs[index]); err != nil {
			return report, err
		}
	}
	if err := tx.Commit(); err != nil {
		return report, fmt.Errorf("commit reindex_chunks enqueue transaction failed: %w", err)
	}
	return report, nil
}

func (r *ReindexEnqueueReport) increment(outcome string) {
	switch outcome {
	case ReindexOutcomeReused:
		r.Reused++
	case ReindexOutcomeRetried:
		r.Retried++
	case ReindexOutcomeReset:
		r.Reset++
	}
}

// insertReindexJob 尝试新增行；返回 nil 表示被 (job_type, kb_id, targets_hash) 部分唯一索引拦住。
// ON CONFLICT 的 WHERE 谓词必须与索引谓词逐字一致（v3.2.1 冻结写法）。
func insertReindexJob(ctx context.Context, tx *sql.Tx, req ReindexEnqueueRequest, key reindexGroupKey, group []ReindexTarget, canonical []CanonicalTarget, targetsHash string) (*JobItem, error) {
	tenantID, projectID := "", ""
	for _, target := range group {
		if tenantID == "" {
			tenantID = target.TenantID
		}
		if projectID == "" {
			projectID = target.ProjectID
		}
	}
	payload := map[string]interface{}{
		"kb_id":       key.KBID,
		"tenant_id":   tenantID,
		"project_id":  projectID,
		"doc_id":      trimmedOrNilString(key.DocID),
		"source":      req.Source,
		"targets":     canonicalPayloadTargets(canonical),
		"max_retries": req.MaxRetries,
	}
	payloadText, err := encodeJobObject(payload)
	if err != nil {
		return nil, err
	}
	item, err := scanJobItem(tx.QueryRowContext(ctx, `
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
			trace_id,
			targets_hash
		)
		VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8, $9, $10)
		ON CONFLICT (job_type, kb_id, targets_hash) WHERE targets_hash IS NOT NULL DO NOTHING
		RETURNING
			`+jobColumns+`
	`, ReindexChunksJobType, JobStatusPending, trimmedOrNilString(tenantID), trimmedOrNilString(projectID), key.KBID, payloadText, req.MaxRetries, req.OperatorID, trimmedOrNilString(req.TraceID), targetsHash))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("insert reindex_chunks admin job failed: %w", err)
	}
	return &item, nil
}

// resolveReindexConflict 锁后回读既有行并按 §16.3 分支表处理，一律不新建行。
// FOR UPDATE 是并发唯一性的第二道线：两个并发同 hash 重提交会在这里串行，
// 后一个看到前一个刚重置出的 pending 行，于是判 reused 而不是二次重置。
func resolveReindexConflict(ctx context.Context, tx *sql.Tx, req ReindexEnqueueRequest, key reindexGroupKey, targetsHash string) (string, JobItem, error) {
	existing, err := scanJobItem(tx.QueryRowContext(ctx, `
		SELECT
			`+jobColumns+`
		FROM admin_jobs
		WHERE job_type = $1 AND kb_id = $2 AND targets_hash = $3
		ORDER BY id
		LIMIT 1
		FOR UPDATE
	`, ReindexChunksJobType, key.KBID, targetsHash))
	if errors.Is(err, sql.ErrNoRows) {
		return "", JobItem{}, ErrReindexEnqueueAnomaly
	}
	if err != nil {
		return "", JobItem{}, fmt.Errorf("lock existing reindex_chunks admin job failed: %w", err)
	}

	switch existing.Status {
	case JobStatusPending, JobStatusRunning, JobStatusSucceeded:
		return ReindexOutcomeReused, existing, nil
	case JobStatusFailed:
		if existing.RetryCount >= existing.MaxRetries {
			return ReindexOutcomeRejected, existing, nil
		}
		reset, err := resetReindexJob(ctx, tx, existing.ID, req.TraceID, true)
		if err != nil {
			return "", JobItem{}, err
		}
		return ReindexOutcomeRetried, reset, nil
	case JobStatusCancelled:
		reset, err := resetReindexJob(ctx, tx, existing.ID, req.TraceID, false)
		if err != nil {
			return "", JobItem{}, err
		}
		return ReindexOutcomeReset, reset, nil
	default:
		// 未知状态一律按复用处理：不冒险重置别人正在管的行。
		return ReindexOutcomeReused, existing, nil
	}
}

// resetReindexJob 原地重置既有行为 pending。consumeRetryQuota=true 时 retry_count+1
// （failed 重试消耗配额），false 时归零（人工取消不消耗配额）。清掉的是 worker 上一轮的
// 执行痕迹（result/error/claim/心跳），否则 worker 会读到过期结论。
func resetReindexJob(ctx context.Context, tx *sql.Tx, jobID int, traceID string, consumeRetryQuota bool) (JobItem, error) {
	retryExpression := "0"
	if consumeRetryQuota {
		retryExpression = "COALESCE(retry_count, 0) + 1"
	}
	item, err := scanJobItem(tx.QueryRowContext(ctx, `
		UPDATE admin_jobs
		SET
			status = 'pending',
			retry_count = `+retryExpression+`,
			started_at = NULL,
			finished_at = NULL,
			error_message = NULL,
			result = NULL,
			claimed_by = NULL,
			claim_expires_at = NULL,
			last_heartbeat_at = NULL,
			trace_id = COALESCE(NULLIF($2, ''), trace_id),
			updated_at = NOW()
		WHERE id = $1
		RETURNING
			`+jobColumns+`
	`, jobID, traceID))
	if err != nil {
		return JobItem{}, fmt.Errorf("reset reindex_chunks admin job failed: %w", err)
	}
	return item, nil
}

// reindexAuditDetails 是审计 details 的冻结形状，逐键对齐 Python 侧 audit_details()。
// 键名 child_job_id 是两条写入路径（任务中心提交 / CLI backfill）的统一口径，
// 退回旧键名 job_id 会让运维按 child_job_id 检索时漏行。
func reindexAuditDetails(entry *ReindexEnqueueEntry, report *ReindexEnqueueReport, status string, source string) map[string]interface{} {
	return map[string]interface{}{
		"job_type":     ReindexChunksJobType,
		"kb_id":        entry.KBID,
		"status":       status,
		"outcome":      entry.Outcome,
		"targets_hash": entry.TargetsHash,
		"child_job_id": optionalInt(entry.Job.ID),
		"target_count": entry.TargetCount,
		"source":       source,
		"created":      report.Created,
		"reused":       report.Reused,
		"retried":      report.Retried,
		"reset":        report.Reset,
		"rejected":     report.Rejected,
	}
}

func writeReindexAudit(ctx context.Context, tx *sql.Tx, req ReindexEnqueueRequest, report *ReindexEnqueueReport, entry *ReindexEnqueueEntry) error {
	switch entry.Outcome {
	case ReindexOutcomeRejected:
		var detail *ReindexRejectedDetail
		for index := range report.RejectedDetail {
			if report.RejectedDetail[index].KBID == entry.KBID && report.RejectedDetail[index].TargetsHash == entry.TargetsHash {
				detail = &report.RejectedDetail[index]
				break
			}
		}
		if detail == nil {
			return fmt.Errorf("reindex_chunks 拒绝留痕缺少对应的 rejected_detail: kb_id=%s targets_hash=%s", entry.KBID, entry.TargetsHash)
		}
		message := "reindex_chunks 重试额度已用尽，拒绝再次入队（§16.3）"
		return insertJobAuditLogEntry(ctx, tx, ReindexAuditActionRejected, entry.Job, req.OperatorID, nil, req.IPAddress, req.UserAgent, map[string]interface{}{
			"job_type":      ReindexChunksJobType,
			"kb_id":         entry.KBID,
			"reason":        detail.Reason,
			"targets_hash":  entry.TargetsHash,
			"child_job_id":  detail.ChildJobID,
			"retry_count":   detail.RetryCount,
			"max_retries":   detail.MaxRetries,
			"chunk_ids":     detail.ChunkIDs,
			"submit_source": req.Source,
		}, "failed", &message)
	default:
		return insertJobAuditLogEntry(ctx, tx, reindexAuditActionForOutcome(entry.Outcome), entry.Job, req.OperatorID, nil, req.IPAddress, req.UserAgent,
			reindexAuditDetails(entry, report, entry.Job.Status, req.Source), "success", nil)
	}
}

func reindexAuditActionForOutcome(outcome string) string {
	if outcome == ReindexOutcomeCreated {
		return ReindexAuditActionCreated
	}
	return ReindexAuditActionReused
}

type reindexGroupKey struct {
	KBID  string
	DocID string
}

// groupReindexTargets 按 (kb_id, doc_id) 分组并校验每条 target。
// 跨文档不合并：否则 targets_hash 会把无关文档的重建绑在一起，复用判定失去文档级可操作性。
func groupReindexTargets(targets []ReindexTarget) (map[reindexGroupKey][]ReindexTarget, error) {
	if len(targets) == 0 {
		return nil, ErrReindexScopeRequired
	}
	groups := map[reindexGroupKey][]ReindexTarget{}
	for _, raw := range targets {
		kbID := strings.TrimSpace(raw.KBID)
		chunkID := strings.TrimSpace(raw.ChunkID)
		if kbID == "" || chunkID == "" {
			return nil, ErrReindexScopeRequired
		}
		if !utf8.ValidString(kbID) || !utf8.ValidString(chunkID) {
			return nil, ErrReindexScopeRequired
		}
		if raw.TargetRevision < 1 {
			return nil, ErrReindexScopeRequired
		}
		key := reindexGroupKey{KBID: kbID, DocID: strings.TrimSpace(raw.DocID)}
		target := ReindexTarget{
			KBID:           kbID,
			DocID:          key.DocID,
			TenantID:       strings.TrimSpace(raw.TenantID),
			ProjectID:      strings.TrimSpace(raw.ProjectID),
			ChunkID:        chunkID,
			TargetRevision: raw.TargetRevision,
		}
		groups[key] = append(groups[key], target)
	}
	return groups, nil
}

func sortedReindexGroupKeys(groups map[reindexGroupKey][]ReindexTarget) []reindexGroupKey {
	keys := make([]reindexGroupKey, 0, len(groups))
	for key := range groups {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].KBID != keys[j].KBID {
			return keys[i].KBID < keys[j].KBID
		}
		return keys[i].DocID < keys[j].DocID
	})
	return keys
}

func optionalInt(value int) *int {
	if value <= 0 {
		return nil
	}
	return &value
}

func canonicalPayloadTargets(canonical []CanonicalTarget) []map[string]interface{} {
	targets := make([]map[string]interface{}, 0, len(canonical))
	for _, item := range canonical {
		targets = append(targets, map[string]interface{}{
			"chunk_id":        item.ChunkID,
			"target_revision": item.TargetRevision,
		})
	}
	return targets
}

func trimmedOrNilString(value string) interface{} {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return nil
	}
	return trimmed
}
