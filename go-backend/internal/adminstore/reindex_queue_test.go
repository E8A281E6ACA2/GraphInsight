package adminstore

import (
	"context"
	"errors"
	"reflect"
	"testing"
)

func TestGroupReindexTargetsRejectsIncompleteScope(t *testing.T) {
	cases := []struct {
		name    string
		targets []ReindexTarget
	}{
		{"空 targets", nil},
		{"缺 kb_id", []ReindexTarget{{ChunkID: "c1", TargetRevision: 1}}},
		{"缺 chunk_id", []ReindexTarget{{KBID: "kb-1", TargetRevision: 1}}},
		{"revision 为 0", []ReindexTarget{{KBID: "kb-1", ChunkID: "c1"}}},
		{"revision 为负", []ReindexTarget{{KBID: "kb-1", ChunkID: "c1", TargetRevision: -3}}},
		{"非法 UTF-8", []ReindexTarget{{KBID: "kb-1", ChunkID: "\xff\xfe", TargetRevision: 1}}},
	}
	for _, tc := range cases {
		_, err := groupReindexTargets(tc.targets)
		if !errors.Is(err, ErrReindexScopeRequired) {
			t.Errorf("%s 期望 ErrReindexScopeRequired，实际 %v", tc.name, err)
		}
	}
}

// 跨文档不合并：否则 targets_hash 把无关文档绑成一个 job，复用判定失去文档级可操作性。
func TestGroupReindexTargetsSplitsByDocAndNormalizesWhitespace(t *testing.T) {
	groups, err := groupReindexTargets([]ReindexTarget{
		{KBID: " kb-1 ", DocID: " doc-a ", ChunkID: " c1 ", TenantID: " t1 ", ProjectID: " p1 ", TargetRevision: 2},
		{KBID: "kb-1", DocID: "doc-a", ChunkID: "c2", TargetRevision: 1},
		{KBID: "kb-1", DocID: "doc-b", ChunkID: "c3", TargetRevision: 1},
	})
	if err != nil {
		t.Fatalf("合法 targets 被拒: %v", err)
	}
	keys := sortedReindexGroupKeys(groups)
	want := []reindexGroupKey{{KBID: "kb-1", DocID: "doc-a"}, {KBID: "kb-1", DocID: "doc-b"}}
	if !reflect.DeepEqual(keys, want) {
		t.Fatalf("分组键不符期望：%+v", keys)
	}
	first := groups[want[0]]
	if len(first) != 2 || first[0].ChunkID != "c1" || first[0].TenantID != "t1" || first[0].ProjectID != "p1" {
		t.Fatalf("同文档应合并且作用域已归一化，实际 %+v", first)
	}
}

func TestReindexAuditActionForOutcome(t *testing.T) {
	if got := reindexAuditActionForOutcome(ReindexOutcomeCreated); got != ReindexAuditActionCreated {
		t.Fatalf("created 必须落 job_created，实际 %q", got)
	}
	for _, outcome := range []string{ReindexOutcomeReused, ReindexOutcomeRetried, ReindexOutcomeReset} {
		if got := reindexAuditActionForOutcome(outcome); got != ReindexAuditActionReused {
			t.Fatalf("%s 必须落 job_reused（一律不新建行），实际 %q", outcome, got)
		}
	}
	if ReindexAuditActionRejected != "kb_chunk_reindex_failed" {
		t.Fatalf("超限拒绝 action 与 Python 侧不一致：%q", ReindexAuditActionRejected)
	}
}

// details 键集合是跨语言契约：Python audit_details() 缺一个键或多一个旧键名 job_id，
// 运维按 child_job_id 检索就会漏掉 Go 写的行。
func TestReindexAuditDetailsCarriesFrozenKeys(t *testing.T) {
	entry := &ReindexEnqueueEntry{
		KBID:        "kb-1",
		TargetsHash: "abc",
		TargetCount: 2,
		Outcome:     ReindexOutcomeReused,
		Job:         JobItem{ID: 42, Status: JobStatusPending},
	}
	report := &ReindexEnqueueReport{Created: 1, Reused: 2, Retried: 3, Reset: 4, Rejected: 5}
	details := reindexAuditDetails(entry, report, JobStatusPending, "admin_api")

	if _, exists := details["job_id"]; exists {
		t.Fatal("details 不得带旧键名 job_id")
	}
	want := []string{
		"job_type", "kb_id", "status", "outcome", "targets_hash", "child_job_id",
		"target_count", "source", "created", "reused", "retried", "reset", "rejected",
	}
	for _, key := range want {
		if _, ok := details[key]; !ok {
			t.Errorf("details 缺键 %s", key)
		}
	}
	if len(details) != len(want) {
		t.Errorf("details 键数量 %d，期望 %d：%#v", len(details), len(want), details)
	}
	if got := details["child_job_id"]; got == nil || *(got.(*int)) != 42 {
		t.Fatalf("child_job_id 必须是本次锁到的既有行 id，实际 %#v", details["child_job_id"])
	}
	if details["job_type"] != ReindexChunksJobType {
		t.Fatalf("job_type 必须是 %s，实际 %v", ReindexChunksJobType, details["job_type"])
	}
}

// 存储层未初始化时必须结构化报错，不能 panic 也不能静默返回空报告（假绿）。
func TestEnqueueReindexChunksFailsClosedWithoutStore(t *testing.T) {
	report, err := (&Client{}).EnqueueReindexChunks(context.Background(), ReindexEnqueueRequest{
		Targets: []ReindexTarget{{KBID: "kb-1", ChunkID: "c1", TargetRevision: 1}},
	})
	if err == nil {
		t.Fatal("未初始化的 store 必须报错")
	}
	if report.Jobs != nil && len(report.Jobs) != 0 {
		t.Fatalf("报错时不得带回任何 job 行：%+v", report.Jobs)
	}
}

// 去重不变量的守门石：reindex_chunks 走 CreateJob（裸 INSERT）会绕过 §16.3，
// 所以它必须永远不在通用白名单里。
func TestReindexChunksStaysOutOfGenericCreateJobWhitelist(t *testing.T) {
	if _, ok := supportedJobTypes[ReindexChunksJobType]; ok {
		t.Fatal("reindex_chunks 不得进 supportedJobTypes：CreateJob 不做去重")
	}
	if err := validateJobCreateRequest(JobCreateRequest{JobType: ReindexChunksJobType}); !errors.Is(err, ErrJobValidation) {
		t.Fatalf("CreateJob(reindex_chunks) 必须被白名单拒掉，实际 %v", err)
	}
}
