package adminstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// 知识库文档注册表（knowledge_base_documents）访问层。
// 列契约来源：backend/admin/models.py class KnowledgeBaseDocument（M3-Python 冻结）。
// 相对路径契约：relative_path 不含 storage_prefix，禁止 ".."、绝对路径、反斜杠、盘符
// （backend/services/document_registry.py assert_safe_relative 同一套规则）。

// 文档状态与索引状态（契约 §8.1 / models.py 注释）。
const (
	DocumentStatusUploaded = "uploaded"
	DocumentStatusParsing  = "parsing"
	DocumentStatusIndexed  = "indexed"
	DocumentStatusFailed   = "failed"

	DocumentGraphStatusPending = "pending"
	DocumentGraphStatusStale   = "stale"
	DocumentGraphStatusIndexed = "indexed"

	DocumentVectorStatusPending = "pending"
	DocumentVectorStatusStale   = "stale"
	DocumentVectorStatusIndexed = "indexed"

	DocumentSourceTypeUpload = "upload"
)

var (
	ErrDocumentNotFound    = errors.New("document registry row not found")
	ErrDocumentValidation  = errors.New("document registry validation failed")
	ErrDocumentDuplicate   = errors.New("document registry duplicate content")
	ErrDocumentPathInvalid = errors.New("document relative path invalid")
)

// DocumentRegistryItem 是 knowledge_base_documents 的一行。
type DocumentRegistryItem struct {
	DocID          string     `json:"doc_id"`
	KBID           string     `json:"kb_id"`
	TenantID       string     `json:"tenant_id"`
	ProjectID      string     `json:"project_id"`
	Name           string     `json:"name"`
	RelativePath   string     `json:"relative_path"`
	SourceType     string     `json:"source_type"`
	SourceURI      *string    `json:"source_uri,omitempty"`
	MimeType       *string    `json:"mime_type,omitempty"`
	Size           int64      `json:"size"`
	SHA256         string     `json:"sha256"`
	Version        int        `json:"version"`
	Status         string     `json:"status"`
	ParserProvider *string    `json:"parser_provider,omitempty"`
	ParserVersion  *string    `json:"parser_version,omitempty"`
	GraphStatus    string     `json:"graph_status"`
	VectorStatus   string     `json:"vector_status"`
	ErrorSummary   *string    `json:"error_summary,omitempty"`
	CreatedBy      *int       `json:"created_by,omitempty"`
	UpdatedBy      *int       `json:"updated_by,omitempty"`
	CreatedAt      time.Time  `json:"created_at"`
	UpdatedAt      *time.Time `json:"updated_at,omitempty"`
}

// DocumentInsertRequest 上传入库请求。Version/Status 零值分别取 1/uploaded。
type DocumentInsertRequest struct {
	DocID        string
	KBID         string
	TenantID     string
	ProjectID    string
	Name         string
	RelativePath string
	SourceType   string
	MimeType     *string
	Size         int64
	SHA256       string
	Version      int
	Status       string
	CreatedBy    *int
	UpdatedBy    *int
}

// DocumentListQuery 按 KB 分页查询（注册表是文档列表的权威来源）。
type DocumentListQuery struct {
	KBID     string
	Page     int
	PageSize int
}

type DocumentListResult struct {
	Items []DocumentRegistryItem
	Total int
}

// DocumentStatusUpdate 局部状态更新：nil 字段保持不变。
type DocumentStatusUpdate struct {
	DocID        string
	Status       *string
	GraphStatus  *string
	VectorStatus *string
	ErrorSummary *string
	UpdatedBy    *int
}

// InsertDocument 写入一条文档注册表行。
// doc_id 唯一或 (kb_id, sha256, version) 唯一冲突返回 ErrDocumentDuplicate。
func (c *Client) InsertDocument(ctx context.Context, req DocumentInsertRequest) (DocumentRegistryItem, error) {
	if c == nil || c.db == nil {
		return DocumentRegistryItem{}, errors.New("admin store is not initialized")
	}
	if err := validateDocumentInsertRequest(req); err != nil {
		return DocumentRegistryItem{}, err
	}
	version := req.Version
	if version < 1 {
		version = 1
	}
	status := req.Status
	if strings.TrimSpace(status) == "" {
		status = DocumentStatusUploaded
	}
	sourceType := req.SourceType
	if strings.TrimSpace(sourceType) == "" {
		sourceType = DocumentSourceTypeUpload
	}

	row := c.db.QueryRowContext(ctx, `
		INSERT INTO knowledge_base_documents (
			doc_id,
			kb_id,
			tenant_id,
			project_id,
			name,
			relative_path,
			source_type,
			source_uri,
			mime_type,
			size,
			sha256,
			version,
			status,
			parser_provider,
			parser_version,
			graph_status,
			vector_status,
			error_summary,
			created_by,
			updated_by
		)
		VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, $8, $9, $10, $11, $12, NULL, NULL, $13, $14, NULL, $15, $15)
		RETURNING
			doc_id,
			kb_id,
			tenant_id,
			project_id,
			name,
			relative_path,
			source_type,
			source_uri,
			mime_type,
			size,
			sha256,
			version,
			status,
			parser_provider,
			parser_version,
			graph_status,
			vector_status,
			error_summary,
			created_by,
			updated_by,
			created_at,
			updated_at
	`, req.DocID, req.KBID, req.TenantID, req.ProjectID, req.Name, req.RelativePath,
		sourceType, req.MimeType, req.Size, req.SHA256, version, status,
		DocumentGraphStatusPending, DocumentVectorStatusPending, req.CreatedBy)
	item, err := scanDocumentRegistryItem(row)
	if err != nil {
		return DocumentRegistryItem{}, mapDocumentConstraintError("insert document registry row failed", err)
	}
	return item, nil
}

// GetDocument 按 doc_id 读取注册表行；kb 归属校验由调用方完成（handler 必须比对 item.KBID）。
func (c *Client) GetDocument(ctx context.Context, docID string) (DocumentRegistryItem, error) {
	if c == nil || c.db == nil {
		return DocumentRegistryItem{}, errors.New("admin store is not initialized")
	}
	docID = strings.TrimSpace(docID)
	if docID == "" {
		return DocumentRegistryItem{}, ErrDocumentNotFound
	}
	row := c.db.QueryRowContext(ctx, `
		SELECT
			doc_id,
			kb_id,
			tenant_id,
			project_id,
			name,
			relative_path,
			source_type,
			source_uri,
			mime_type,
			size,
			sha256,
			version,
			status,
			parser_provider,
			parser_version,
			graph_status,
			vector_status,
			error_summary,
			created_by,
			updated_by,
			created_at,
			updated_at
		FROM knowledge_base_documents
		WHERE doc_id = $1
		LIMIT 1
	`, docID)
	item, err := scanDocumentRegistryItem(row)
	if errors.Is(err, sql.ErrNoRows) {
		return DocumentRegistryItem{}, ErrDocumentNotFound
	}
	if err != nil {
		return DocumentRegistryItem{}, fmt.Errorf("query document registry row failed: %w", err)
	}
	return item, nil
}

// ListDocumentsByKB 分页列出目标 KB 的注册表行（禁止无 kb 全量列表）。
func (c *Client) ListDocumentsByKB(ctx context.Context, query DocumentListQuery) (DocumentListResult, error) {
	if c == nil || c.db == nil {
		return DocumentListResult{}, errors.New("admin store is not initialized")
	}
	kbID := strings.TrimSpace(query.KBID)
	if kbID == "" {
		return DocumentListResult{}, ErrDocumentValidation
	}
	page, pageSize := query.Page, query.PageSize
	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = 200
	}
	if pageSize > 500 {
		pageSize = 500
	}

	var total int
	if err := c.db.QueryRowContext(ctx,
		"SELECT COUNT(*) FROM knowledge_base_documents WHERE kb_id = $1", kbID).Scan(&total); err != nil {
		return DocumentListResult{}, fmt.Errorf("count knowledge base documents failed: %w", err)
	}

	rows, err := c.db.QueryContext(ctx, `
		SELECT
			doc_id,
			kb_id,
			tenant_id,
			project_id,
			name,
			relative_path,
			source_type,
			source_uri,
			mime_type,
			size,
			sha256,
			version,
			status,
			parser_provider,
			parser_version,
			graph_status,
			vector_status,
			error_summary,
			created_by,
			updated_by,
			created_at,
			updated_at
		FROM knowledge_base_documents
		WHERE kb_id = $1
		ORDER BY created_at DESC, doc_id ASC
		LIMIT $2 OFFSET $3
	`, kbID, pageSize, (page-1)*pageSize)
	if err != nil {
		return DocumentListResult{}, fmt.Errorf("query knowledge base documents failed: %w", err)
	}
	defer rows.Close()

	items := []DocumentRegistryItem{}
	for rows.Next() {
		item, err := scanDocumentRegistryItem(rows)
		if err != nil {
			return DocumentListResult{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return DocumentListResult{}, fmt.Errorf("iterate knowledge base documents failed: %w", err)
	}
	return DocumentListResult{Items: items, Total: total}, nil
}

// MarkDocumentStatus 更新 status / graph_status / vector_status / error_summary（供后续 pipeline 使用）。
func (c *Client) MarkDocumentStatus(ctx context.Context, req DocumentStatusUpdate) (DocumentRegistryItem, error) {
	if c == nil || c.db == nil {
		return DocumentRegistryItem{}, errors.New("admin store is not initialized")
	}
	if strings.TrimSpace(req.DocID) == "" {
		return DocumentRegistryItem{}, ErrDocumentNotFound
	}
	if req.Status != nil && !isValidDocumentStatus(*req.Status) {
		return DocumentRegistryItem{}, ErrDocumentValidation
	}
	if req.GraphStatus != nil && !isValidDocumentIndexStatus(*req.GraphStatus) {
		return DocumentRegistryItem{}, ErrDocumentValidation
	}
	if req.VectorStatus != nil && !isValidDocumentIndexStatus(*req.VectorStatus) {
		return DocumentRegistryItem{}, ErrDocumentValidation
	}
	row := c.db.QueryRowContext(ctx, `
		UPDATE knowledge_base_documents
		SET
			status = COALESCE($2, status),
			graph_status = COALESCE($3, graph_status),
			vector_status = COALESCE($4, vector_status),
			error_summary = COALESCE($5, error_summary),
			updated_by = COALESCE($6, updated_by),
			updated_at = NOW()
		WHERE doc_id = $1
		RETURNING
			doc_id,
			kb_id,
			tenant_id,
			project_id,
			name,
			relative_path,
			source_type,
			source_uri,
			mime_type,
			size,
			sha256,
			version,
			status,
			parser_provider,
			parser_version,
			graph_status,
			vector_status,
			error_summary,
			created_by,
			updated_by,
			created_at,
			updated_at
	`, strings.TrimSpace(req.DocID), req.Status, req.GraphStatus, req.VectorStatus, req.ErrorSummary, req.UpdatedBy)
	item, err := scanDocumentRegistryItem(row)
	if errors.Is(err, sql.ErrNoRows) {
		return DocumentRegistryItem{}, ErrDocumentNotFound
	}
	if err != nil {
		return DocumentRegistryItem{}, fmt.Errorf("update document registry status failed: %w", err)
	}
	return item, nil
}

// DeleteDocumentRow 删除单行，且必须匹配 kb（跨 KB 删除返回 ErrDocumentNotFound，不存在泄露）。
func (c *Client) DeleteDocumentRow(ctx context.Context, docID string, kbID string) error {
	if c == nil || c.db == nil {
		return errors.New("admin store is not initialized")
	}
	docID = strings.TrimSpace(docID)
	kbID = strings.TrimSpace(kbID)
	if docID == "" || kbID == "" {
		return ErrDocumentNotFound
	}
	result, err := c.db.ExecContext(ctx,
		"DELETE FROM knowledge_base_documents WHERE doc_id = $1 AND kb_id = $2", docID, kbID)
	if err != nil {
		return fmt.Errorf("delete document registry row failed: %w", err)
	}
	if affected, err := result.RowsAffected(); err == nil && affected == 0 {
		return ErrDocumentNotFound
	}
	return nil
}

// DeleteDocumentRowsByKB 清空目标 KB 的全部注册表行，返回删除行数。
func (c *Client) DeleteDocumentRowsByKB(ctx context.Context, kbID string) (int64, error) {
	if c == nil || c.db == nil {
		return 0, errors.New("admin store is not initialized")
	}
	kbID = strings.TrimSpace(kbID)
	if kbID == "" {
		return 0, ErrDocumentValidation
	}
	result, err := c.db.ExecContext(ctx,
		"DELETE FROM knowledge_base_documents WHERE kb_id = $1", kbID)
	if err != nil {
		return 0, fmt.Errorf("delete document registry rows by kb failed: %w", err)
	}
	return result.RowsAffected()
}

func scanDocumentRegistryItem(scanner kbRowScanner) (DocumentRegistryItem, error) {
	var item DocumentRegistryItem
	var sourceURI sql.NullString
	var mimeType sql.NullString
	var parserProvider sql.NullString
	var parserVersion sql.NullString
	var errorSummary sql.NullString
	var createdBy sql.NullInt64
	var updatedBy sql.NullInt64
	var updatedAt sql.NullTime
	if err := scanner.Scan(
		&item.DocID,
		&item.KBID,
		&item.TenantID,
		&item.ProjectID,
		&item.Name,
		&item.RelativePath,
		&item.SourceType,
		&sourceURI,
		&mimeType,
		&item.Size,
		&item.SHA256,
		&item.Version,
		&item.Status,
		&parserProvider,
		&parserVersion,
		&item.GraphStatus,
		&item.VectorStatus,
		&errorSummary,
		&createdBy,
		&updatedBy,
		&item.CreatedAt,
		&updatedAt,
	); err != nil {
		return DocumentRegistryItem{}, err
	}
	item.SourceURI = stringPtrFromNull(sourceURI)
	item.MimeType = stringPtrFromNull(mimeType)
	item.ParserProvider = stringPtrFromNull(parserProvider)
	item.ParserVersion = stringPtrFromNull(parserVersion)
	item.ErrorSummary = stringPtrFromNull(errorSummary)
	item.CreatedBy = intPtrFromNull(createdBy)
	item.UpdatedBy = intPtrFromNull(updatedBy)
	if updatedAt.Valid {
		value := updatedAt.Time
		item.UpdatedAt = &value
	}
	return item, nil
}

func validateDocumentInsertRequest(req DocumentInsertRequest) error {
	if strings.TrimSpace(req.DocID) == "" || len(req.DocID) > 64 {
		return ErrDocumentValidation
	}
	if strings.TrimSpace(req.KBID) == "" || len(req.KBID) > 100 {
		return ErrDocumentValidation
	}
	if strings.TrimSpace(req.TenantID) == "" || len(req.TenantID) > 100 {
		return ErrDocumentValidation
	}
	if strings.TrimSpace(req.ProjectID) == "" || len(req.ProjectID) > 100 {
		return ErrDocumentValidation
	}
	name := strings.TrimSpace(req.Name)
	if name == "" || len(name) > 255 {
		return ErrDocumentValidation
	}
	if req.Size < 0 {
		return ErrDocumentValidation
	}
	sha := strings.ToLower(strings.TrimSpace(req.SHA256))
	if len(sha) != 64 || !isHex64(sha) {
		return ErrDocumentValidation
	}
	if req.MimeType != nil && len(*req.MimeType) > 120 {
		return ErrDocumentValidation
	}
	if req.Status != "" && !isValidDocumentStatus(req.Status) {
		return ErrDocumentValidation
	}
	if req.Version < 0 || req.Version > 1_000_000 {
		return ErrDocumentValidation
	}
	// relative_path 与 Python assert_safe_relative 同规则：禁止空、反斜杠、
	// 绝对路径、NUL、盘符、"."/".." 片段（第 9.2 节路径安全）。
	if err := ValidateDocumentRelativePath(req.RelativePath); err != nil {
		return err
	}
	return nil
}

// ValidateDocumentRelativePath 校验 storage_prefix / relative_path 片段。
// 规则与 backend/services/document_registry.py assert_safe_relative 对齐。
func ValidateDocumentRelativePath(value string) error {
	raw := strings.TrimSpace(value)
	if raw == "" {
		return ErrDocumentPathInvalid
	}
	if strings.HasPrefix(raw, "/") || strings.HasPrefix(raw, "\\") ||
		strings.Contains(raw, "\\") || strings.ContainsRune(raw, '\x00') {
		return ErrDocumentPathInvalid
	}
	if hasWindowsDrivePrefix(raw) {
		return ErrDocumentPathInvalid
	}
	cleaned := strings.Trim(raw, "/")
	if cleaned == "" || len(cleaned) > 500 {
		return ErrDocumentPathInvalid
	}
	for _, part := range strings.Split(cleaned, "/") {
		if part == "" || part == "." || part == ".." {
			return ErrDocumentPathInvalid
		}
	}
	return nil
}

// hasWindowsDrivePrefix 识别 "C:" 形式的盘符前缀（PureWindowsPath.drive 同语义）。
func hasWindowsDrivePrefix(value string) bool {
	if len(value) < 2 || value[1] != ':' {
		return false
	}
	c := value[0]
	return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
}

func isHex64(value string) bool {
	for _, ch := range value {
		if (ch < '0' || ch > '9') && (ch < 'a' || ch > 'f') {
			return false
		}
	}
	return true
}

func isValidDocumentStatus(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case DocumentStatusUploaded, DocumentStatusParsing, DocumentStatusIndexed, DocumentStatusFailed, "archived":
		return true
	default:
		return false
	}
}

func isValidDocumentIndexStatus(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case DocumentGraphStatusPending, DocumentGraphStatusStale, DocumentGraphStatusIndexed, DocumentStatusFailed:
		return true
	default:
		return false
	}
}

// mapDocumentConstraintError 将唯一约束冲突（doc_id 主键 / uq_kb_document_content_version）
// 映射为 ErrDocumentDuplicate；其余错误原样包装。
func mapDocumentConstraintError(message string, err error) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return ErrDocumentDuplicate
	}
	return fmt.Errorf("%s: %w", message, err)
}
