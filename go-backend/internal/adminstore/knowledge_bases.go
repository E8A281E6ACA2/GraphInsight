package adminstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5/pgconn"
)

// 知识库状态（契约 §5.1：active | archived | deleting）。
const (
	KBStatusActive   = "active"
	KBStatusArchived = "archived"
	KBStatusDeleting = "deleting"
)

var (
	ErrKBNotFound            = errors.New("knowledge base not found")
	ErrKBDuplicateName       = errors.New("knowledge base duplicate name")
	ErrKBValidation          = errors.New("knowledge base validation failed")
	ErrKBArchived            = errors.New("knowledge base archived")
	ErrKBInvalidState        = errors.New("knowledge base invalid state")
	ErrKBParentScopeRequired = errors.New("knowledge base parent scope required")
)

// KnowledgeBaseItem 是 knowledge_bases 表的一行（列定义来源 backend/admin/models.py class KnowledgeBase）。
// 注意 metadata 列在数据库中名为 "metadata"（SQLAlchemy 保留属性，迁移脚本同名列）。
type KnowledgeBaseItem struct {
	ID               string                 `json:"id"`
	TenantID         string                 `json:"tenant_id"`
	ProjectID        string                 `json:"project_id"`
	Name             string                 `json:"name"`
	Slug             *string                `json:"slug,omitempty"`
	Description      *string                `json:"description,omitempty"`
	Status           string                 `json:"status"`
	StoragePrefix    string                 `json:"storage_prefix"`
	ParserProfile    map[string]interface{} `json:"parser_profile,omitempty"`
	RetrievalProfile map[string]interface{} `json:"retrieval_profile,omitempty"`
	Metadata         map[string]interface{} `json:"metadata,omitempty"`
	CreatedBy        *int                   `json:"created_by,omitempty"`
	UpdatedBy        *int                   `json:"updated_by,omitempty"`
	CreatedAt        time.Time              `json:"created_at"`
	UpdatedAt        *time.Time             `json:"updated_at,omitempty"`
	ArchivedAt       *time.Time             `json:"archived_at,omitempty"`
}

// KBListQuery 目录查询。TenantID/ProjectID 为必填父作用域（契约 §3.2 结构性例外：
// 列出 KB 使用已授权的 tenant/project 父作用域；禁止无父作用域的全局列表）。
type KBListQuery struct {
	TenantID  string
	ProjectID string
	Status    string
	Page      int
	PageSize  int
}

type KBListResult struct {
	Items []KnowledgeBaseItem
	Total int
}

type KBCreateRequest struct {
	ID               string
	TenantID         string
	ProjectID        string
	Name             string
	Slug             *string
	Description      *string
	StoragePrefix    string
	ParserProfile    map[string]interface{}
	RetrievalProfile map[string]interface{}
	CreatedBy        *int
}

// KBUpdateRequest 部分更新：nil 字段保持不变。
// Slug 传空串表示清空为 NULL；Status 仅允许 active（恢复，清除 archived_at）/ archived（归档）。
type KBUpdateRequest struct {
	KBID             string
	Name             *string
	Slug             *string
	Description      *string
	ParserProfile    *map[string]interface{}
	RetrievalProfile *map[string]interface{}
	Status           *string
	UpdatedBy        *int
}

type KBArchiveRequest struct {
	KBID      string
	UpdatedBy *int
}

// KBDeleteRequest 仅做状态软转换（status='deleting'），不做任何数据删除（M2 边界）。
type KBDeleteRequest struct {
	KBID      string
	UpdatedBy *int
}

// ListKnowledgeBases 分页列出父作用域内的知识库。禁止无父作用域的全量返回。
func (c *Client) ListKnowledgeBases(ctx context.Context, query KBListQuery) (KBListResult, error) {
	if c == nil || c.db == nil {
		return KBListResult{}, errors.New("admin store is not initialized")
	}
	if err := normalizeKBListQuery(&query); err != nil {
		return KBListResult{}, err
	}
	where, args := buildKBListWhere(query)

	var total int
	if err := c.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM knowledge_bases kb"+where, args...).Scan(&total); err != nil {
		return KBListResult{}, fmt.Errorf("count knowledge bases failed: %w", err)
	}

	listArgs := append([]interface{}{}, args...)
	limitIndex := len(listArgs) + 1
	offsetIndex := len(listArgs) + 2
	listArgs = append(listArgs, query.PageSize, (query.Page-1)*query.PageSize)
	rows, err := c.db.QueryContext(ctx, fmt.Sprintf(`
		SELECT
			kb.id,
			kb.tenant_id,
			kb.project_id,
			kb.name,
			kb.slug,
			kb.description,
			kb.status,
			kb.storage_prefix,
			kb.parser_profile,
			kb.retrieval_profile,
			kb."metadata",
			kb.created_by,
			kb.updated_by,
			kb.created_at,
			kb.updated_at,
			kb.archived_at
		FROM knowledge_bases kb
		%s
		ORDER BY kb.created_at DESC
		LIMIT $%d OFFSET $%d
	`, where, limitIndex, offsetIndex), listArgs...)
	if err != nil {
		return KBListResult{}, fmt.Errorf("query knowledge bases failed: %w", err)
	}
	defer rows.Close()

	items := []KnowledgeBaseItem{}
	for rows.Next() {
		item, err := scanKnowledgeBaseItem(rows)
		if err != nil {
			return KBListResult{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return KBListResult{}, fmt.Errorf("iterate knowledge bases failed: %w", err)
	}
	return KBListResult{Items: items, Total: total}, nil
}

// GetKnowledgeBase 按 id 读取知识库；作用域归属校验由 handler 完成后二次鉴权。
func (c *Client) GetKnowledgeBase(ctx context.Context, kbID string) (KnowledgeBaseItem, error) {
	if c == nil || c.db == nil {
		return KnowledgeBaseItem{}, errors.New("admin store is not initialized")
	}
	kbID = strings.TrimSpace(kbID)
	if kbID == "" {
		return KnowledgeBaseItem{}, ErrKBNotFound
	}
	row := c.db.QueryRowContext(ctx, `
		SELECT
			id,
			tenant_id,
			project_id,
			name,
			slug,
			description,
			status,
			storage_prefix,
			parser_profile,
			retrieval_profile,
			"metadata",
			created_by,
			updated_by,
			created_at,
			updated_at,
			archived_at
		FROM knowledge_bases
		WHERE id = $1
		LIMIT 1
	`, kbID)
	item, err := scanKnowledgeBaseItem(row)
	if errors.Is(err, sql.ErrNoRows) {
		return KnowledgeBaseItem{}, ErrKBNotFound
	}
	if err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("query knowledge base failed: %w", err)
	}
	return item, nil
}

// ListAuthorizedKnowledgeBases 按调用方已解析的授权集合取 active 知识库行（M4-R1 步骤 3
// 业务面 KB 目录）。本方法不做任何授权判断：授权集合必须由调用方经 AuthorizedKBIDs
// 解析后传入。语义：
//   - allKBs=true（global 绑定/legacy 放行的全量哨兵）→ 查全部 status='active' 行；
//   - 否则按显式 kbIDs 取行；kbIDs 为空返回空集合（fail-closed，不放大为全量）。
//
// archived/deleting 一律不进入结果（可选目录只暴露 active）。
func (c *Client) ListAuthorizedKnowledgeBases(ctx context.Context, kbIDs []string, allKBs bool) ([]KnowledgeBaseItem, error) {
	if c == nil || c.db == nil {
		return nil, errors.New("admin store is not initialized")
	}
	query := `
		SELECT
			id,
			tenant_id,
			project_id,
			name,
			slug,
			description,
			status,
			storage_prefix,
			parser_profile,
			retrieval_profile,
			"metadata",
			created_by,
			updated_by,
			created_at,
			updated_at,
			archived_at
		FROM knowledge_bases
		`
	args := []interface{}{}
	if allKBs {
		query += " WHERE status = $1"
		args = append(args, KBStatusActive)
	} else {
		normalized := normalizedKBIDSet(kbIDs)
		if len(normalized) == 0 {
			return []KnowledgeBaseItem{}, nil
		}
		query += " WHERE id = ANY($1) AND status = $2"
		args = append(args, normalized, KBStatusActive)
	}
	query += " ORDER BY created_at DESC"

	rows, err := c.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("query authorized knowledge bases failed: %w", err)
	}
	defer rows.Close()

	items := []KnowledgeBaseItem{}
	for rows.Next() {
		item, err := scanKnowledgeBaseItem(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate authorized knowledge bases failed: %w", err)
	}
	return items, nil
}

// CreateKnowledgeBase 创建知识库。(tenant_id, project_id, name) 唯一冲突返回 ErrKBDuplicateName。
func (c *Client) CreateKnowledgeBase(ctx context.Context, req KBCreateRequest) (KnowledgeBaseItem, error) {
	if c == nil || c.db == nil {
		return KnowledgeBaseItem{}, errors.New("admin store is not initialized")
	}
	if err := validateKBCreateRequest(req); err != nil {
		return KnowledgeBaseItem{}, err
	}
	parserProfile, err := encodeKBProfile(req.ParserProfile)
	if err != nil {
		return KnowledgeBaseItem{}, err
	}
	retrievalProfile, err := encodeKBProfile(req.RetrievalProfile)
	if err != nil {
		return KnowledgeBaseItem{}, err
	}

	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("begin knowledge base create transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	// 与 users.go 相同的预检模式：唯一约束兜底（并发插入由 23505 映射兜底）。
	var existingID string
	if err := tx.QueryRowContext(ctx, `
		SELECT id
		FROM knowledge_bases
		WHERE tenant_id = $1 AND project_id = $2 AND name = $3
		LIMIT 1
	`, req.TenantID, req.ProjectID, req.Name).Scan(&existingID); err == nil {
		return KnowledgeBaseItem{}, ErrKBDuplicateName
	} else if !errors.Is(err, sql.ErrNoRows) {
		return KnowledgeBaseItem{}, fmt.Errorf("check knowledge base name conflict failed: %w", err)
	}

	row := tx.QueryRowContext(ctx, `
		INSERT INTO knowledge_bases (
			id,
			tenant_id,
			project_id,
			name,
			slug,
			description,
			status,
			storage_prefix,
			parser_profile,
			retrieval_profile,
			created_by,
			updated_by
		)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11)
		RETURNING
			id,
			tenant_id,
			project_id,
			name,
			slug,
			description,
			status,
			storage_prefix,
			parser_profile,
			retrieval_profile,
			"metadata",
			created_by,
			updated_by,
			created_at,
			updated_at,
			archived_at
	`, req.ID, req.TenantID, req.ProjectID, req.Name, req.Slug, req.Description,
		KBStatusActive, req.StoragePrefix, parserProfile, retrievalProfile, req.CreatedBy)
	item, err := scanKnowledgeBaseItem(row)
	if err != nil {
		return KnowledgeBaseItem{}, mapKnowledgeBaseConstraintError("insert knowledge base failed", err)
	}
	if err := tx.Commit(); err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("commit knowledge base create transaction failed: %w", err)
	}
	return item, nil
}

// UpdateKnowledgeBase 部分更新。归档库仅允许恢复（status=active）；
// deleting 状态拒绝任何更新；名称冲突返回 ErrKBDuplicateName。
func (c *Client) UpdateKnowledgeBase(ctx context.Context, req KBUpdateRequest) (KnowledgeBaseItem, error) {
	if c == nil || c.db == nil {
		return KnowledgeBaseItem{}, errors.New("admin store is not initialized")
	}
	if err := validateKBUpdateRequest(req); err != nil {
		return KnowledgeBaseItem{}, err
	}
	var parserProfile *string
	if req.ParserProfile != nil {
		encoded, err := encodeKBProfile(*req.ParserProfile)
		if err != nil {
			return KnowledgeBaseItem{}, err
		}
		parserProfile = encoded
	}
	var retrievalProfile *string
	if req.RetrievalProfile != nil {
		encoded, err := encodeKBProfile(*req.RetrievalProfile)
		if err != nil {
			return KnowledgeBaseItem{}, err
		}
		retrievalProfile = encoded
	}

	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("begin knowledge base update transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	current, err := getKnowledgeBaseForUpdate(ctx, tx, req.KBID)
	if errors.Is(err, sql.ErrNoRows) {
		return KnowledgeBaseItem{}, ErrKBNotFound
	}
	if err != nil {
		return KnowledgeBaseItem{}, err
	}
	if current.Status == KBStatusDeleting {
		return KnowledgeBaseItem{}, ErrKBInvalidState
	}
	if current.Status == KBStatusArchived && (req.Status == nil || *req.Status != KBStatusActive) {
		return KnowledgeBaseItem{}, ErrKBArchived
	}
	if req.Name != nil && *req.Name != current.Name {
		var existingID string
		if err := tx.QueryRowContext(ctx, `
			SELECT id
			FROM knowledge_bases
			WHERE tenant_id = $1 AND project_id = $2 AND name = $3 AND id <> $4
			LIMIT 1
		`, current.TenantID, current.ProjectID, *req.Name, req.KBID).Scan(&existingID); err == nil {
			return KnowledgeBaseItem{}, ErrKBDuplicateName
		} else if !errors.Is(err, sql.ErrNoRows) {
			return KnowledgeBaseItem{}, fmt.Errorf("check knowledge base name conflict failed: %w", err)
		}
	}

	row := tx.QueryRowContext(ctx, `
		UPDATE knowledge_bases
		SET
			name = COALESCE($2, name),
			slug = CASE WHEN $3 IS NULL THEN slug WHEN $3 = '' THEN NULL ELSE $3 END,
			description = COALESCE($4, description),
			parser_profile = COALESCE($5, parser_profile),
			retrieval_profile = COALESCE($6, retrieval_profile),
			status = COALESCE($7, status),
			archived_at = CASE
				WHEN $7 = $8 THEN NOW()
				WHEN $7 = $9 THEN NULL
				ELSE archived_at
			END,
			updated_by = COALESCE($10, updated_by),
			updated_at = NOW()
		WHERE id = $1
		RETURNING
			id,
			tenant_id,
			project_id,
			name,
			slug,
			description,
			status,
			storage_prefix,
			parser_profile,
			retrieval_profile,
			"metadata",
			created_by,
			updated_by,
			created_at,
			updated_at,
			archived_at
	`, req.KBID, req.Name, req.Slug, req.Description, parserProfile, retrievalProfile,
		req.Status, KBStatusArchived, KBStatusActive, req.UpdatedBy)
	item, err := scanKnowledgeBaseItem(row)
	if err != nil {
		return KnowledgeBaseItem{}, mapKnowledgeBaseConstraintError("update knowledge base failed", err)
	}
	if err := tx.Commit(); err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("commit knowledge base update transaction failed: %w", err)
	}
	return item, nil
}

// ArchiveKnowledgeBase 归档知识库（status='archived', archived_at=NOW()）。重复归档幂等成功。
func (c *Client) ArchiveKnowledgeBase(ctx context.Context, req KBArchiveRequest) (KnowledgeBaseItem, error) {
	if c == nil || c.db == nil {
		return KnowledgeBaseItem{}, errors.New("admin store is not initialized")
	}
	if strings.TrimSpace(req.KBID) == "" {
		return KnowledgeBaseItem{}, ErrKBNotFound
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("begin knowledge base archive transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	current, err := getKnowledgeBaseForUpdate(ctx, tx, req.KBID)
	if errors.Is(err, sql.ErrNoRows) {
		return KnowledgeBaseItem{}, ErrKBNotFound
	}
	if err != nil {
		return KnowledgeBaseItem{}, err
	}
	if current.Status == KBStatusDeleting {
		return KnowledgeBaseItem{}, ErrKBInvalidState
	}
	row := tx.QueryRowContext(ctx, `
		UPDATE knowledge_bases
		SET
			status = $2,
			archived_at = NOW(),
			updated_by = COALESCE($3, updated_by),
			updated_at = NOW()
		WHERE id = $1
		RETURNING
			id,
			tenant_id,
			project_id,
			name,
			slug,
			description,
			status,
			storage_prefix,
			parser_profile,
			retrieval_profile,
			"metadata",
			created_by,
			updated_by,
			created_at,
			updated_at,
			archived_at
	`, req.KBID, KBStatusArchived, req.UpdatedBy)
	item, err := scanKnowledgeBaseItem(row)
	if err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("archive knowledge base failed: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("commit knowledge base archive transaction failed: %w", err)
	}
	return item, nil
}

// DeleteKnowledgeBase 仅做状态软转换 status='deleting'（M2 不执行文件/图谱/向量删除）。
// 已处于 deleting 状态时幂等返回当前记录。数据清理由后续任务中心任务执行。
func (c *Client) DeleteKnowledgeBase(ctx context.Context, req KBDeleteRequest) (KnowledgeBaseItem, error) {
	if c == nil || c.db == nil {
		return KnowledgeBaseItem{}, errors.New("admin store is not initialized")
	}
	if strings.TrimSpace(req.KBID) == "" {
		return KnowledgeBaseItem{}, ErrKBNotFound
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("begin knowledge base delete transaction failed: %w", err)
	}
	defer rollbackUnlessCommitted(tx)

	current, err := getKnowledgeBaseForUpdate(ctx, tx, req.KBID)
	if errors.Is(err, sql.ErrNoRows) {
		return KnowledgeBaseItem{}, ErrKBNotFound
	}
	if err != nil {
		return KnowledgeBaseItem{}, err
	}
	if current.Status == KBStatusDeleting {
		if err := tx.Commit(); err != nil {
			return KnowledgeBaseItem{}, fmt.Errorf("commit knowledge base delete transaction failed: %w", err)
		}
		return current, nil
	}
	row := tx.QueryRowContext(ctx, `
		UPDATE knowledge_bases
		SET
			status = $2,
			updated_by = COALESCE($3, updated_by),
			updated_at = NOW()
		WHERE id = $1
		RETURNING
			id,
			tenant_id,
			project_id,
			name,
			slug,
			description,
			status,
			storage_prefix,
			parser_profile,
			retrieval_profile,
			"metadata",
			created_by,
			updated_by,
			created_at,
			updated_at,
			archived_at
	`, req.KBID, KBStatusDeleting, req.UpdatedBy)
	item, err := scanKnowledgeBaseItem(row)
	if err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("mark knowledge base deleting failed: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return KnowledgeBaseItem{}, fmt.Errorf("commit knowledge base delete transaction failed: %w", err)
	}
	return item, nil
}

type kbRowScanner interface {
	Scan(dest ...interface{}) error
}

func scanKnowledgeBaseItem(scanner kbRowScanner) (KnowledgeBaseItem, error) {
	var item KnowledgeBaseItem
	var slug sql.NullString
	var description sql.NullString
	var parserProfile sql.NullString
	var retrievalProfile sql.NullString
	var metadata sql.NullString
	var createdBy sql.NullInt64
	var updatedBy sql.NullInt64
	var updatedAt sql.NullTime
	var archivedAt sql.NullTime
	if err := scanner.Scan(
		&item.ID,
		&item.TenantID,
		&item.ProjectID,
		&item.Name,
		&slug,
		&description,
		&item.Status,
		&item.StoragePrefix,
		&parserProfile,
		&retrievalProfile,
		&metadata,
		&createdBy,
		&updatedBy,
		&item.CreatedAt,
		&updatedAt,
		&archivedAt,
	); err != nil {
		return KnowledgeBaseItem{}, err
	}
	item.Slug = stringPtrFromNull(slug)
	item.Description = stringPtrFromNull(description)
	item.ParserProfile = parseObjectJSONOrNil(stringPtrFromNull(parserProfile))
	item.RetrievalProfile = parseObjectJSONOrNil(stringPtrFromNull(retrievalProfile))
	item.Metadata = parseObjectJSONOrNil(stringPtrFromNull(metadata))
	item.CreatedBy = intPtrFromNull(createdBy)
	item.UpdatedBy = intPtrFromNull(updatedBy)
	if updatedAt.Valid {
		value := updatedAt.Time
		item.UpdatedAt = &value
	}
	if archivedAt.Valid {
		value := archivedAt.Time
		item.ArchivedAt = &value
	}
	return item, nil
}

func getKnowledgeBaseForUpdate(ctx context.Context, tx *sql.Tx, kbID string) (KnowledgeBaseItem, error) {
	row := tx.QueryRowContext(ctx, `
		SELECT
			id,
			tenant_id,
			project_id,
			name,
			slug,
			description,
			status,
			storage_prefix,
			parser_profile,
			retrieval_profile,
			"metadata",
			created_by,
			updated_by,
			created_at,
			updated_at,
			archived_at
		FROM knowledge_bases
		WHERE id = $1
		LIMIT 1
		FOR UPDATE
	`, strings.TrimSpace(kbID))
	item, err := scanKnowledgeBaseItem(row)
	if err != nil {
		return KnowledgeBaseItem{}, err
	}
	return item, nil
}

// buildKBListWhere 父作用域两列为硬条件（由 normalizeKBListQuery 保证非空），status 可选。
func buildKBListWhere(query KBListQuery) (string, []interface{}) {
	clauses := []string{}
	args := []interface{}{}
	if tenantID := strings.TrimSpace(query.TenantID); tenantID != "" {
		args = append(args, tenantID)
		clauses = append(clauses, fmt.Sprintf("kb.tenant_id = $%d", len(args)))
	}
	if projectID := strings.TrimSpace(query.ProjectID); projectID != "" {
		args = append(args, projectID)
		clauses = append(clauses, fmt.Sprintf("kb.project_id = $%d", len(args)))
	}
	if status := strings.TrimSpace(query.Status); status != "" {
		args = append(args, status)
		clauses = append(clauses, fmt.Sprintf("kb.status = $%d", len(args)))
	}
	if len(clauses) == 0 {
		return "", args
	}
	return " WHERE " + strings.Join(clauses, " AND "), args
}

// normalizeKBListQuery 规范化分页并强制父作用域；status 过滤值合法。
func normalizeKBListQuery(query *KBListQuery) error {
	query.TenantID = strings.TrimSpace(query.TenantID)
	query.ProjectID = strings.TrimSpace(query.ProjectID)
	query.Status = strings.TrimSpace(query.Status)
	if query.TenantID == "" || query.ProjectID == "" {
		return ErrKBParentScopeRequired
	}
	if query.Status != "" && !isValidKBStatus(query.Status) {
		return ErrKBValidation
	}
	if query.Page < 1 {
		query.Page = 1
	}
	if query.PageSize < 1 {
		query.PageSize = 20
	}
	if query.PageSize > 200 {
		query.PageSize = 200
	}
	return nil
}

func validateKBCreateRequest(req KBCreateRequest) error {
	if strings.TrimSpace(req.ID) == "" {
		return ErrKBValidation
	}
	if strings.TrimSpace(req.TenantID) == "" || strings.TrimSpace(req.ProjectID) == "" {
		return ErrKBValidation
	}
	if len(req.TenantID) > 100 || len(req.ProjectID) > 100 {
		return ErrKBValidation
	}
	name := strings.TrimSpace(req.Name)
	if name == "" || utf8.RuneCountInString(name) > 200 {
		return ErrKBValidation
	}
	if req.Slug != nil && utf8.RuneCountInString(strings.TrimSpace(*req.Slug)) > 100 {
		return ErrKBValidation
	}
	if strings.TrimSpace(req.StoragePrefix) == "" || len(req.StoragePrefix) > 255 {
		return ErrKBValidation
	}
	return nil
}

func validateKBUpdateRequest(req KBUpdateRequest) error {
	if strings.TrimSpace(req.KBID) == "" {
		return ErrKBValidation
	}
	if req.Name != nil {
		name := strings.TrimSpace(*req.Name)
		if name == "" || utf8.RuneCountInString(name) > 200 {
			return ErrKBValidation
		}
	}
	if req.Slug != nil && utf8.RuneCountInString(strings.TrimSpace(*req.Slug)) > 100 {
		return ErrKBValidation
	}
	if req.Status != nil && *req.Status != KBStatusActive && *req.Status != KBStatusArchived {
		return ErrKBValidation
	}
	return nil
}

func isValidKBStatus(status string) bool {
	switch strings.TrimSpace(status) {
	case KBStatusActive, KBStatusArchived, KBStatusDeleting:
		return true
	default:
		return false
	}
}

func encodeKBProfile(profile map[string]interface{}) (*string, error) {
	if profile == nil {
		return nil, nil
	}
	encoded, err := json.Marshal(profile)
	if err != nil {
		return nil, fmt.Errorf("encode knowledge base profile failed: %w", err)
	}
	value := string(encoded)
	return &value, nil
}

// mapKnowledgeBaseConstraintError 将并发插入触发的唯一约束冲突（uq_knowledge_base_scope_name）
// 映射为 ErrKBDuplicateName；主键冲突与其他错误原样返回。
func mapKnowledgeBaseConstraintError(message string, err error) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" && !strings.HasSuffix(pgErr.ConstraintName, "_pkey") {
		return ErrKBDuplicateName
	}
	return fmt.Errorf("%s: %w", message, err)
}
