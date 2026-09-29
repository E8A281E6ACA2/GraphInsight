package httpserver

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
	"graphinsight/go-backend/internal/orchestrator"
	"graphinsight/go-backend/internal/scope"
)

const (
	documentsSoftDeleteDirName = ".trash"
	documentsSoftDeleteMetaExt = ".meta.json"
	defaultSoftDeleteRetention = 7
	documentDryRunPreviewLimit = 20
)

var supportedDocumentExts = map[string]struct{}{
	".txt":      {},
	".md":       {},
	".markdown": {},
	".csv":      {},
	".json":     {},
	".log":      {},
	".docx":     {},
	".pdf":      {},
}

// vectorCleanupClient 是单文档删除后向量清理回调的窄接口；由 orchestrator.Client 实现
// （M4 FIX #4：删除成功后 best-effort 通知 Python 清理该文档向量）。
type vectorCleanupClient interface {
	DoJSONWithOptions(ctx context.Context, method, path, rawQuery string, body []byte, headers map[string]string, options orchestrator.RequestOptions) (int, []byte, error)
}

// vectorCleanupRequest 与 Python internal 端点 POST /api/internal/vector/delete-doc
// 的请求契约（body {doc_id, kb_id}；由并行交付的 Python 侧实现）。
type vectorCleanupRequest struct {
	DocID string `json:"doc_id"`
	KBID  string `json:"kb_id"`
}

// buildVectorCleanupHeaders 向量清理回调的转发头（X-Go-Orchestrator、X-Trace-Id
// 与 KB 作用域三元组，取服务端 KB 行的权威作用域）。
func buildVectorCleanupHeaders(r *http.Request, kb adminstore.KnowledgeBaseItem) map[string]string {
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
	if traceID == "" {
		traceID = newForwardTraceID()
	}
	return map[string]string{
		"X-Go-Orchestrator": "graphinsight-go",
		"X-Trace-Id":        traceID,
		"x-tenant-id":       kb.TenantID,
		"x-project-id":      kb.ProjectID,
		"x-kb-id":           kb.ID,
	}
}

// vectorCleanupAttempts 是单文档删除后向量清理回调的最大尝试次数（M4-R1 FIX P1-a）。
const vectorCleanupAttempts = 3

// requestVectorCleanup 单文档删除成功后的 best-effort 向量清理回调。
// 最多尝试 vectorCleanupAttempts 次（短退避）；全部失败返回 retryable=true，
// 由调用方写 document_vector_cleanup_failed 审计并标记可重试。
func requestVectorCleanup(r *http.Request, client vectorCleanupClient, logger *slog.Logger, docID string, kb adminstore.KnowledgeBaseItem) (attempted bool, success bool, retryable bool) {
	if client == nil {
		logger.Warn("vector cleanup skipped: orchestrator client unavailable", "doc_id", docID, "kb_id", kb.ID)
		return false, false, false
	}
	body, err := json.Marshal(vectorCleanupRequest{DocID: docID, KBID: kb.ID})
	if err != nil {
		logger.Warn("vector cleanup encode failed", "doc_id", docID, "kb_id", kb.ID, "error", err.Error())
		return true, false, true
	}
	var lastStatus int
	var lastErrMsg string
	for attempt := 1; attempt <= vectorCleanupAttempts; attempt++ {
		if r.Context().Err() != nil {
			return true, false, true
		}
		status, _, err := client.DoJSONWithOptions(
			r.Context(),
			http.MethodPost,
			"/api/internal/vector/delete-doc",
			"",
			body,
			buildVectorCleanupHeaders(r, kb),
			orchestrator.RequestOptions{Timeout: 15 * time.Second},
		)
		if err == nil && status < http.StatusBadRequest {
			return true, true, false
		}
		lastStatus = status
		lastErrMsg = fmt.Sprintf("status %d", lastStatus)
		if err != nil {
			lastErrMsg = err.Error()
		}
		logger.Warn("vector cleanup attempt failed",
			"doc_id", docID, "kb_id", kb.ID, "attempt", attempt, "detail", lastErrMsg)
		if attempt < vectorCleanupAttempts {
			select {
			case <-time.After(time.Duration(attempt) * 500 * time.Millisecond):
			case <-r.Context().Done():
				return true, false, true
			}
		}
	}
	return true, false, true
}

// auditVectorCleanupFailed 在向量清理最终失败时写审计（契约 §2.10：失败路径可追踪）。
func auditVectorCleanupFailed(r *http.Request, guard businessPermissionGuard, logger *slog.Logger, docID string, kb adminstore.KnowledgeBaseItem) {
	writer, ok := guard.adminStore.(businessAuditWriter)
	if !ok {
		return
	}
	var operatorID *int
	if raw := strings.TrimSpace(r.Header.Get("x-auth-user-id")); raw != "" {
		if id, err := strconv.Atoi(raw); err == nil && id > 0 {
			operatorID = &id
		}
	}
	var traceID *string
	if v := strings.TrimSpace(r.Header.Get("X-Trace-Id")); v != "" {
		traceID = &v
	}
	if err := writer.RecordBusinessAudit(r.Context(), adminstore.BusinessAuditRequest{
		OperatorID: operatorID,
		TenantID:   scopeStringPtr(kb.TenantID),
		ProjectID:  scopeStringPtr(kb.ProjectID),
		KBID:       scopeStringPtr(kb.ID),
		TraceID:    traceID,
		Action:     "document_vector_cleanup_failed",
		Resource:   "kb_document",
		ResourceID: scopeStringPtr(docID),
		Details: map[string]interface{}{
			"kb_id":     kb.ID,
			"doc_id":    docID,
			"retryable": true,
		},
		Status: "failed",
	}); err != nil {
		logger.Warn("write vector cleanup failure audit failed", "doc_id", docID, "error", err.Error())
	}
}

// deletedDocumentMeta 是回收站 meta 文件（.trash/*.meta.json）的内容。
// KBID/RelativePath/SHA256 为 M3 新增：恢复与回收站列表都按 kb 隔离，
// 恢复时按 same doc_id 重建注册表行。OriginalPath/TrashPath 仅存在于本地
// meta 文件中用于恢复，不得出现在任何 API 响应里。
type deletedDocumentMeta struct {
	DocID        string      `json:"doc_id"`
	KBID         string      `json:"kb_id,omitempty"`
	TenantID     string      `json:"tenant_id,omitempty"`
	ProjectID    string      `json:"project_id,omitempty"`
	RelativePath string      `json:"relative_path,omitempty"`
	SHA256       string      `json:"sha256,omitempty"`
	Name         string      `json:"name"`
	Ext          string      `json:"ext"`
	Size         int64       `json:"size"`
	OriginalPath string      `json:"original_path"`
	TrashPath    string      `json:"trash_path"`
	DeletedAt    int64       `json:"deleted_at"`
	ExpiresAt    int64       `json:"expires_at"`
	PurgeGraph   *bool       `json:"purge_graph"`
	Operator     interface{} `json:"operator"`
}

type deletedDocumentRecord struct {
	Meta     *deletedDocumentMeta
	MetaPath string
}

// documentTrashInput 汇总进入回收站前需要的全部上下文（kb 隔离字段一起落盘）。
type documentTrashInput struct {
	DocID        string
	KBID         string
	TenantID     string
	ProjectID    string
	RelativePath string
	SHA256       string
	Name         string
	FilePath     string
	PurgeGraph   bool
	Operator     string
}

// buildNativeDocumentsListHandler GET /api/documents（kb:read，强制 kb 作用域）。
// 列表权威来源是 knowledge_base_documents 注册表；响应不包含服务器绝对路径。
func buildNativeDocumentsListHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	docStore adminDocumentStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if !guard.allowRequest(w, r, "kb:read") {
			return
		}
		if docStore == nil {
			logger.Error("document registry store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "文档数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		kb, ok := resolveAuthorizedKBForRequest(w, r, logger, guard, kbStore, "kb:read")
		if !ok {
			return
		}
		page := boundedIntQuery(r, "page", 1, 1, 1_000_000)
		pageSize := boundedIntQuery(r, "page_size", 50, 1, 500)
		result, err := docStore.ListDocumentsByKB(r.Context(), adminstore.DocumentListQuery{
			KBID:     kb.ID,
			Page:     page,
			PageSize: pageSize,
		})
		if err != nil {
			logger.Error("list documents failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取文档列表失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		items := make([]map[string]interface{}, 0, len(result.Items))
		for _, row := range result.Items {
			items = append(items, documentRegistryItemResponse(cfg, kb, row))
		}
		WriteJSON(w, http.StatusOK, "ok", map[string]interface{}{
			"items":     items,
			"total":     result.Total,
			"page":      page,
			"page_size": pageSize,
			"kb_id":     kb.ID,
		})
	})
}

// documentRegistryItemResponse 把注册表行转换为列表响应（mtime 优先取文件、size 优先取注册表）。
func documentRegistryItemResponse(cfg config.Config, kb adminstore.KnowledgeBaseItem, row adminstore.DocumentRegistryItem) map[string]interface{} {
	updatedAt := int64(0)
	if filePath, scopeErr := resolveDocumentPhysicalPath(cfg, kb, row.RelativePath); scopeErr == nil {
		if info, statErr := os.Stat(filePath); statErr == nil {
			updatedAt = info.ModTime().UnixMilli()
		}
	}
	if updatedAt == 0 {
		if row.UpdatedAt != nil {
			updatedAt = row.UpdatedAt.UnixMilli()
		} else {
			updatedAt = row.CreatedAt.UnixMilli()
		}
	}
	return map[string]interface{}{
		"id":            row.DocID,
		"doc_id":        row.DocID,
		"kb_id":         row.KBID,
		"name":          row.Name,
		"ext":           strings.ToLower(filepath.Ext(row.Name)),
		"size":          row.Size,
		"updated_at":    updatedAt,
		"relative_path": row.RelativePath,
		"status":        row.Status,
		"graph_status":  row.GraphStatus,
		"vector_status": row.VectorStatus,
		"version":       row.Version,
	}
}

// buildNativeDeletedDocumentsListHandler GET /api/documents/deleted（kb:read，强制 kb 作用域）。
// 回收站仍基于文件系统 meta，但只返回当前 KB 的条目，且不再泄露绝对路径。
func buildNativeDeletedDocumentsListHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if !guard.allowRequest(w, r, "kb:read") {
			return
		}
		kb, ok := resolveAuthorizedKBForRequest(w, r, logger, guard, kbStore, "kb:read")
		if !ok {
			return
		}
		items, err := listDeletedDocumentItems(logger, cfg, kb.ID)
		if err != nil {
			logger.Error("list deleted documents failed", "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "获取回收站列表失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}
		WriteJSON(w, http.StatusOK, "ok", map[string]interface{}{"items": items, "kb_id": kb.ID})
	})
}

// buildNativeDocumentsUploadHandler POST /api/documents/upload（kb:write，强制 kb 作用域）。
// 流程：scope 解析 → KB 加载（active 校验）→ 二阶段权限 → 落盘（边写边算 SHA-256）→
// 服务端 UUID doc_id → 注册表行（status=uploaded，graph/vector=pending）。
func buildNativeDocumentsUploadHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	docStore adminDocumentStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if !guard.allowRequest(w, r, "kb:write") {
			return
		}
		if docStore == nil {
			logger.Error("document registry store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "文档数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		// kb 作用域来自 header/query，先于 multipart body 解析执行全部校验。
		kb, ok := resolveAuthorizedKBForRequest(w, r, logger, guard, kbStore, "kb:write")
		if !ok {
			return
		}
		if err := r.ParseMultipartForm(64 << 20); err != nil {
			logger.Error("parse upload form failed", "error", err.Error())
			WriteJSON(w, http.StatusBadRequest, "无效上传请求", map[string]string{"error_code": "INVALID_REQUEST"})
			return
		}

		files := r.MultipartForm.File["files"]
		if len(files) == 0 {
			WriteJSON(w, http.StatusBadRequest, "缺少上传文件", map[string]string{"error_code": "INVALID_REQUEST"})
			return
		}

		root, scopeErr := ensureKBDocumentRoot(cfg, kb)
		if scopeErr != nil {
			writeScopeError(w, scopeErr)
			return
		}

		uploaded := make([]map[string]interface{}, 0, len(files))
		skipped := make([]map[string]interface{}, 0)
		for _, header := range files {
			item, skippedItem := saveUploadedDocument(kb, docStore, root, header, r, logger)
			if item != nil {
				uploaded = append(uploaded, item)
			}
			if skippedItem != nil {
				skipped = append(skipped, skippedItem)
			}
		}

		WriteJSON(w, http.StatusOK, "上传完成", map[string]interface{}{
			"kb_id":    kb.ID,
			"uploaded": uploaded,
			"skipped":  skipped,
		})
	})
}

// saveUploadedDocument 保存单个上传文件并写入注册表。
// 注册表写入失败时删除已落盘文件，避免不可追踪的孤儿文件（手册 §7.3）。
func saveUploadedDocument(
	kb adminstore.KnowledgeBaseItem,
	docStore adminDocumentStore,
	root string,
	header *multipart.FileHeader,
	r *http.Request,
	logger *slog.Logger,
) (map[string]interface{}, map[string]interface{}) {
	originalName := ""
	if header != nil {
		originalName = header.Filename
	}
	skip := func(reason string) (map[string]interface{}, map[string]interface{}) {
		return nil, map[string]interface{}{"name": originalName, "reason": reason}
	}

	filename := sanitizeDocumentRelativeName(originalName)
	if filename == "" {
		return skip("文件名无效")
	}
	ext := strings.ToLower(filepath.Ext(filename))
	if _, ok := supportedDocumentExts[ext]; !ok {
		return skip("不支持的文件类型")
	}
	if header == nil {
		return skip("文件名无效")
	}

	finalName := filename
	target := filepath.Join(root, finalName)
	if _, err := os.Stat(target); err == nil {
		stamp := time.Now().Unix()
		finalName = renameDocumentWithStamp(filename, stamp)
		target = filepath.Join(root, finalName)
		for idx := 1; ; idx++ {
			if _, err := os.Stat(target); os.IsNotExist(err) {
				break
			}
			finalName = renameDocumentWithStampIndex(filename, stamp, idx)
			target = filepath.Join(root, finalName)
		}
	}

	src, err := header.Open()
	if err != nil {
		return skip(err.Error())
	}
	defer src.Close()

	hasher := sha256.New()
	dst, err := os.Create(target)
	if err != nil {
		return skip(err.Error())
	}
	if _, err := io.Copy(dst, io.TeeReader(src, hasher)); err != nil {
		_ = dst.Close()
		_ = os.Remove(target)
		return skip(err.Error())
	}
	if err := dst.Close(); err != nil {
		_ = os.Remove(target)
		return skip(err.Error())
	}

	info, err := os.Stat(target)
	if err != nil {
		_ = os.Remove(target)
		return skip(err.Error())
	}
	shaHex := hex.EncodeToString(hasher.Sum(nil))

	docID := scope.NewUUID()
	var mimeTypePtr *string
	if mimeType := mime.TypeByExtension(ext); strings.TrimSpace(mimeType) != "" {
		mimeTypePtr = &mimeType
	}
	operatorID := optionalIntHeader(r, "x-auth-user-id")
	_, err = docStore.InsertDocument(r.Context(), adminstore.DocumentInsertRequest{
		DocID:        docID,
		KBID:         kb.ID,
		TenantID:     kb.TenantID,
		ProjectID:    kb.ProjectID,
		Name:         finalName,
		RelativePath: finalName,
		SourceType:   adminstore.DocumentSourceTypeUpload,
		MimeType:     mimeTypePtr,
		Size:         info.Size(),
		SHA256:       shaHex,
		Version:      1,
		Status:       adminstore.DocumentStatusUploaded,
		CreatedBy:    operatorID,
		UpdatedBy:    operatorID,
	})
	if err != nil {
		_ = os.Remove(target)
		if errors.Is(err, adminstore.ErrDocumentDuplicate) {
			return skip("相同内容已存在于该知识库")
		}
		logger.Error("insert document registry row failed", "kb_id", kb.ID, "error", err.Error())
		return skip("文档注册失败")
	}

	return map[string]interface{}{
		"id":            docID,
		"doc_id":        docID,
		"kb_id":         kb.ID,
		"name":          finalName,
		"ext":           ext,
		"size":          info.Size(),
		"sha256":        shaHex,
		"relative_path": finalName,
	}, nil
}

// buildNativeDocumentDeleteHandler DELETE /api/documents/{doc_id}（kb:delete，强制 kb 作用域）。
// 注册表行必须存在且属于当前 KB（否则 404 KB_NOT_FOUND，不存在泄露）；
// 回收站 meta 记录 kb_id；图谱清除按 (doc_id, kb_id) 双条件；成功后删除注册表行。
// M4 FIX #5：verify 前后图谱统计使用 KB-scoped totals；M4 FIX #4：删除成功后
// best-effort 调用 Python 向量清理端点，失败不影响删除。
func buildNativeDocumentDeleteHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	docStore adminDocumentStore,
	graphSvc graphService,
	vectorClient vectorCleanupClient,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if !guard.allowRequest(w, r, "kb:delete") {
			return
		}
		if docStore == nil {
			logger.Error("document registry store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "文档数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		kb, ok := resolveAuthorizedKBForRequest(w, r, logger, guard, kbStore, "kb:delete")
		if !ok {
			return
		}

		docID := strings.TrimSpace(strings.TrimPrefix(r.URL.Path, "/api/documents/"))
		if docID == "" || strings.Contains(docID, "/") {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}

		purgeGraph := parseBoolQuery(r, "purge_graph", true)
		softDelete := parseBoolQuery(r, "soft_delete", true)
		dryRun := parseBoolQuery(r, "dry_run", false)
		verifyAfter := parseBoolQuery(r, "verify_after", true)

		row, err := docStore.GetDocument(r.Context(), docID)
		if errors.Is(err, adminstore.ErrDocumentNotFound) || (err == nil && row.KBID != kb.ID) {
			// 归属不匹配与不存在返回同一错误，避免跨 KB 存在性泄露。
			WriteJSON(w, http.StatusNotFound, "知识库文档不存在", map[string]string{"error_code": scope.CodeKBNotFound})
			return
		}
		if err != nil {
			logger.Error("get document registry row failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "删除文档失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		filePath, pathScopeErr := resolveDocumentPhysicalPath(cfg, kb, row.RelativePath)
		fileExists := false
		if pathScopeErr == nil {
			if info, statErr := os.Stat(filePath); statErr == nil && !info.IsDir() {
				fileExists = true
			}
		}

		beforeActiveDocs, err := registryDocumentTotal(r, docStore, kb.ID)
		if err != nil {
			logger.Error("count active documents failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "删除文档失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		var beforeGraph *graph.DocumentGraphStats
		var graphPreview *graph.DocumentGraphStats
		if purgeGraph {
			if graphSvc == nil {
				WriteJSON(w, http.StatusServiceUnavailable, "图谱服务不可用", map[string]string{"error_code": "DATABASE_UNAVAILABLE"})
				return
			}
			totals, err := graphSvc.GetDocumentGraphTotalsForKB(r.Context(), kb.ID)
			if err != nil {
				logger.Error("get document graph totals failed", "doc_id", docID, "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "删除文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			beforeGraph = &totals
			preview, err := graphSvc.PreviewDeleteDocumentGraph(r.Context(), docID, kb.ID)
			if err != nil {
				logger.Error("preview delete document graph failed", "doc_id", docID, "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "删除文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			graphPreview = &preview
		}

		if dryRun {
			var afterGraphEstimate map[string]interface{}
			if beforeGraph != nil && graphPreview != nil {
				afterGraphEstimate = map[string]interface{}{
					"documents": maxInt64(beforeGraph.Documents-graphPreview.Documents, 0),
					"chunks":    maxInt64(beforeGraph.Chunks-graphPreview.Chunks, 0),
					"relations": maxInt64(beforeGraph.Relations-graphPreview.Relations, 0),
				}
			}
			WriteJSON(w, http.StatusOK, "删除预览完成", map[string]interface{}{
				"doc_id":  docID,
				"kb_id":   kb.ID,
				"dry_run": true,
				"mode":    deleteModeName(softDelete),
				"candidate_file": map[string]interface{}{
					"exists":        fileExists,
					"name":          row.Name,
					"relative_path": row.RelativePath,
				},
				"graph": graphStatsMap(graphPreview),
				"verification_preview": map[string]interface{}{
					"before_active_documents": beforeActiveDocs,
					"after_active_documents":  maxInt(beforeActiveDocs-boolToInt(true), 0),
					"after_graph_estimate":    afterGraphEstimate,
				},
			})
			return
		}

		fileDeleted := false
		fileAction := "none"
		var deletedEntry map[string]interface{}
		if fileExists {
			if softDelete {
				meta, err := softDeleteDocumentFile(cfg, documentTrashInput{
					DocID:        row.DocID,
					KBID:         kb.ID,
					TenantID:     kb.TenantID,
					ProjectID:    kb.ProjectID,
					RelativePath: row.RelativePath,
					SHA256:       row.SHA256,
					Name:         row.Name,
					FilePath:     filePath,
					PurgeGraph:   purgeGraph,
					Operator:     currentOperator(r),
				})
				if err != nil {
					logger.Error("soft delete document failed", "doc_id", docID, "error", err.Error())
					WriteJSON(w, http.StatusInternalServerError, "删除文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
					return
				}
				deletedEntry = deletedMetaResponseMap(&meta)
				fileAction = "soft_deleted"
			} else {
				if err := os.Remove(filePath); err != nil && !os.IsNotExist(err) {
					logger.Error("hard delete document failed", "doc_id", docID, "error", err.Error())
					WriteJSON(w, http.StatusInternalServerError, "删除文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
					return
				}
				fileAction = "hard_deleted"
			}
			fileDeleted = true
		}

		var graphStats *graph.DocumentGraphStats
		if purgeGraph {
			stats, err := graphSvc.DeleteDocumentGraph(r.Context(), docID, kb.ID)
			if err != nil {
				logger.Error("delete document graph failed", "doc_id", docID, "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "删除文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			graphStats = &stats
		}

		// 文件与图谱处理成功后删除注册表行（注册表行是删除操作权威变更）。
		if err := docStore.DeleteDocumentRow(r.Context(), docID, kb.ID); err != nil &&
			!errors.Is(err, adminstore.ErrDocumentNotFound) {
			logger.Error("delete document registry row failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "删除文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}

		var verification map[string]interface{}
		if verifyAfter {
			afterActiveDocs, err := registryDocumentTotal(r, docStore, kb.ID)
			if err != nil {
				logger.Error("count active documents after delete failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "删除文档失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			var afterGraph *graph.DocumentGraphStats
			if purgeGraph {
				stats, err := graphSvc.GetDocumentGraphTotalsForKB(r.Context(), kb.ID)
				if err != nil {
					logger.Error("get document graph totals after delete failed", "error", err.Error())
					WriteJSON(w, http.StatusInternalServerError, "删除文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
					return
				}
				afterGraph = &stats
			}
			deletedCount, err := countDeletedDocuments(logger, cfg, kb.ID)
			if err != nil {
				logger.Error("count deleted documents failed", "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "删除文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			verification = buildDocumentVerification(beforeActiveDocs, afterActiveDocs, deletedCount, beforeGraph, afterGraph)
		}

		// M4 FIX #4 / M4-R1 FIX P1-a：删除成功后的向量清理回调；带重试，
		// 最终失败写 document_vector_cleanup_failed 审计并在响应中标记可重试，
		// 不改变删除本身的结果（文件/图谱/注册表已一致）。
		cleanupAttempted, cleanupSuccess, cleanupRetryable := requestVectorCleanup(r, vectorClient, logger, docID, kb)
		if cleanupAttempted && !cleanupSuccess {
			auditVectorCleanupFailed(r, guard, logger, docID, kb)
		}

		WriteJSON(w, http.StatusOK, "删除完成", map[string]interface{}{
			"doc_id":        docID,
			"kb_id":         kb.ID,
			"dry_run":       false,
			"mode":          deleteModeName(softDelete),
			"file_deleted":  fileDeleted,
			"file_action":   fileAction,
			"deleted_entry": deletedEntry,
			"graph":         graphStatsMap(graphStats),
			"verification":  verification,
			"vector_cleanup": map[string]interface{}{
				"attempted": cleanupAttempted,
				"success":   cleanupSuccess,
				"retryable": cleanupRetryable,
			},
		})
	})
}

// buildNativeDocumentsClearHandler DELETE /api/documents（kb:delete，强制 kb 作用域）。
// 只清空当前 KB 的文件 + 注册表行 + 图谱；缺失 kb 的全局清空一律 KB_SCOPE_REQUIRED。
func buildNativeDocumentsClearHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	docStore adminDocumentStore,
	graphSvc graphService,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if !guard.allowRequest(w, r, "kb:delete") {
			return
		}
		if docStore == nil {
			logger.Error("document registry store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "文档数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		kb, ok := resolveAuthorizedKBForRequest(w, r, logger, guard, kbStore, "kb:delete")
		if !ok {
			return
		}

		purgeGraph := parseBoolQuery(r, "purge_graph", true)
		softDelete := parseBoolQuery(r, "soft_delete", true)
		dryRun := parseBoolQuery(r, "dry_run", false)
		verifyAfter := parseBoolQuery(r, "verify_after", true)

		rows, err := listAllRegistryRows(r, docStore, kb.ID)
		if err != nil {
			logger.Error("collect kb documents failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "清空知识库失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		beforeActiveDocs := len(rows)

		var beforeGraph *graph.DocumentGraphStats
		var graphPreview *graph.DocumentGraphStats
		if purgeGraph {
			if graphSvc == nil {
				WriteJSON(w, http.StatusServiceUnavailable, "图谱服务不可用", map[string]string{"error_code": "DATABASE_UNAVAILABLE"})
				return
			}
			totals, err := graphSvc.GetDocumentGraphTotalsForKB(r.Context(), kb.ID)
			if err != nil {
				logger.Error("get document graph totals failed", "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "清空知识库失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			beforeGraph = &totals
			preview, err := graphSvc.PreviewClearDocumentGraph(r.Context(), kb.ID)
			if err != nil {
				logger.Error("preview clear document graph failed", "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "清空知识库失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			graphPreview = &preview
		}

		if dryRun {
			namesPreview := make([]string, 0, minInt(len(rows), documentDryRunPreviewLimit))
			for _, row := range rows[:minInt(len(rows), documentDryRunPreviewLimit)] {
				namesPreview = append(namesPreview, row.Name)
			}
			WriteJSON(w, http.StatusOK, "清空预览完成", map[string]interface{}{
				"dry_run":                 true,
				"kb_id":                   kb.ID,
				"mode":                    deleteModeName(softDelete),
				"candidate_files":         beforeActiveDocs,
				"candidate_names_preview": namesPreview,
				"graph":                   graphStatsMap(graphPreview),
			})
			return
		}

		removedFiles := 0
		removedErrors := make([]string, 0)
		deletedEntries := make([]map[string]interface{}, 0)
		for _, row := range rows {
			filePath, pathScopeErr := resolveDocumentPhysicalPath(cfg, kb, row.RelativePath)
			fileExists := false
			if pathScopeErr == nil {
				if info, statErr := os.Stat(filePath); statErr == nil && !info.IsDir() {
					fileExists = true
				}
			}
			if fileExists && softDelete {
				meta, delErr := softDeleteDocumentFile(cfg, documentTrashInput{
					DocID:        row.DocID,
					KBID:         kb.ID,
					TenantID:     kb.TenantID,
					ProjectID:    kb.ProjectID,
					RelativePath: row.RelativePath,
					SHA256:       row.SHA256,
					Name:         row.Name,
					FilePath:     filePath,
					PurgeGraph:   purgeGraph,
					Operator:     currentOperator(r),
				})
				if delErr != nil {
					removedErrors = append(removedErrors, fmt.Sprintf("%s: %v", row.Name, delErr))
					logger.Warn("soft delete document during clear failed", "doc_id", row.DocID, "error", delErr.Error())
					continue
				}
				deletedEntries = append(deletedEntries, deletedMetaResponseMap(&meta))
				removedFiles++
			} else if fileExists {
				if err := os.Remove(filePath); err != nil && !os.IsNotExist(err) {
					removedErrors = append(removedErrors, fmt.Sprintf("%s: %v", row.Name, err))
					logger.Warn("hard delete document during clear failed", "doc_id", row.DocID, "error", err.Error())
					continue
				}
				removedFiles++
			} else {
				// 文件缺失（或路径非法）时仅清理注册表行，保持注册表与文件系统一致。
				removedFiles++
			}
			if err := docStore.DeleteDocumentRow(r.Context(), row.DocID, kb.ID); err != nil &&
				!errors.Is(err, adminstore.ErrDocumentNotFound) {
				removedErrors = append(removedErrors, fmt.Sprintf("%s: registry: %v", row.Name, err))
				logger.Warn("delete document registry row during clear failed", "doc_id", row.DocID, "error", err.Error())
			}
		}

		var graphStats *graph.DocumentGraphStats
		if purgeGraph {
			stats, err := graphSvc.ClearDocumentGraph(r.Context(), kb.ID)
			if err != nil {
				logger.Error("clear document graph failed", "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "清空知识库失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			graphStats = &stats
		}

		var verification map[string]interface{}
		if verifyAfter {
			afterActiveDocs, err := registryDocumentTotal(r, docStore, kb.ID)
			if err != nil {
				logger.Error("count active documents after clear failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "清空知识库失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			var afterGraph *graph.DocumentGraphStats
			if purgeGraph {
				stats, err := graphSvc.GetDocumentGraphTotalsForKB(r.Context(), kb.ID)
				if err != nil {
					logger.Error("get document graph totals after clear failed", "error", err.Error())
					WriteJSON(w, http.StatusInternalServerError, "清空知识库失败", map[string]string{"error_code": "INTERNAL_ERROR"})
					return
				}
				afterGraph = &stats
			}
			deletedCount, err := countDeletedDocuments(logger, cfg, kb.ID)
			if err != nil {
				logger.Error("count deleted documents after clear failed", "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "清空知识库失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			verification = buildDocumentVerification(beforeActiveDocs, afterActiveDocs, deletedCount, beforeGraph, afterGraph)
		}

		WriteJSON(w, http.StatusOK, "知识库已清空", map[string]interface{}{
			"kb_id":              kb.ID,
			"dry_run":            false,
			"mode":               deleteModeName(softDelete),
			"removed_files":      removedFiles,
			"failed_files":       len(removedErrors),
			"errors_preview":     removedErrors[:minInt(len(removedErrors), documentDryRunPreviewLimit)],
			"soft_deleted_files": len(deletedEntries),
			"graph":              graphStatsMap(graphStats),
			"verification":       verification,
		})
	})
}

// buildNativeDocumentRestoreHandler POST /api/documents/{doc_id}/restore（kb:write，强制 kb 作用域）。
// 回收站 meta 必须匹配当前 KB；恢复到 storage_prefix 相对位置；
// 以原 doc_id 重建注册表行（先建行、后移动文件，失败可重试）。
func buildNativeDocumentRestoreHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	docStore adminDocumentStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if !guard.allowRequest(w, r, "kb:write") {
			return
		}
		if docStore == nil {
			logger.Error("document registry store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "文档数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		kb, ok := resolveAuthorizedKBForRequest(w, r, logger, guard, kbStore, "kb:write")
		if !ok {
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/restore") {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}

		docID := strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/documents/"), "/restore"))
		if docID == "" || strings.Contains(docID, "/") {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}

		verifyAfter := parseBoolQuery(r, "verify_after", true)
		record, err := findDeletedDocumentRecordByID(logger, cfg, docID)
		if err != nil {
			logger.Error("find deleted document failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}
		if record == nil || record.Meta == nil || record.Meta.KBID != kb.ID {
			// meta 不属于当前 KB 与不存在同响应，避免跨 KB 回收站存在性泄露。
			WriteJSON(w, http.StatusNotFound, "回收站中未找到该文档", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		meta := record.Meta

		trashPath := filepath.Clean(strings.TrimSpace(meta.TrashPath))
		if trashPath == "" {
			WriteJSON(w, http.StatusNotFound, "回收站文件已不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		if _, err := os.Stat(trashPath); err != nil {
			if os.IsNotExist(err) {
				WriteJSON(w, http.StatusNotFound, "回收站文件已不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			logger.Error("stat trash file failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}

		// 恢复目标：storage_prefix 下的相对位置（优先 meta.RelativePath，旧 meta 回退 meta.Name）。
		relativePath := strings.TrimSpace(meta.RelativePath)
		if relativePath == "" {
			relativePath = sanitizeDocumentRelativeName(meta.Name)
		}
		targetPath, scopeErr := resolveDocumentPhysicalPath(cfg, kb, relativePath)
		if scopeErr != nil {
			writeScopeError(w, scopeErr)
			return
		}
		if _, err := os.Stat(targetPath); err == nil {
			stamp := time.Now().Unix()
			targetPath = filepath.Join(filepath.Dir(targetPath), renameDocumentWithStamp(filepath.Base(targetPath), stamp))
		}
		finalRelative := filepath.Base(targetPath)
		if err := validateRelativeSegments(finalRelative); err != nil {
			logger.Error("restored relative path invalid", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}

		beforeActiveDocs, err := registryDocumentTotal(r, docStore, kb.ID)
		if err != nil {
			logger.Error("count active documents before restore failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "恢复文档失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		shaHex, size, err := hashFileContents(trashPath)
		if err != nil {
			logger.Error("hash trash file failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}

		// 同一 doc_id 恢复：先幂等清理潜在残留行，再以原 doc_id 重建注册表行。
		if err := docStore.DeleteDocumentRow(r.Context(), docID, kb.ID); err != nil &&
			!errors.Is(err, adminstore.ErrDocumentNotFound) {
			logger.Error("clear stale registry row before restore failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}
		var mimeTypePtr *string
		if mimeType := mime.TypeByExtension(strings.ToLower(filepath.Ext(finalRelative))); strings.TrimSpace(mimeType) != "" {
			mimeTypePtr = &mimeType
		}
		operatorID := optionalIntHeader(r, "x-auth-user-id")
		if _, err := docStore.InsertDocument(r.Context(), adminstore.DocumentInsertRequest{
			DocID:        docID,
			KBID:         kb.ID,
			TenantID:     kb.TenantID,
			ProjectID:    kb.ProjectID,
			Name:         finalRelative,
			RelativePath: finalRelative,
			SourceType:   adminstore.DocumentSourceTypeUpload,
			MimeType:     mimeTypePtr,
			Size:         size,
			SHA256:       shaHex,
			Version:      1,
			Status:       adminstore.DocumentStatusUploaded,
			CreatedBy:    operatorID,
			UpdatedBy:    operatorID,
		}); err != nil {
			logger.Error("reinsert document registry row failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}

		if err := os.MkdirAll(filepath.Dir(targetPath), 0o755); err != nil {
			_ = docStore.DeleteDocumentRow(r.Context(), docID, kb.ID)
			logger.Error("ensure restore target dir failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}
		if err := os.Rename(trashPath, targetPath); err != nil {
			// 回滚注册表行，保留回收站 meta 与文件，恢复操作可重试。
			_ = docStore.DeleteDocumentRow(r.Context(), docID, kb.ID)
			logger.Error("restore document move failed", "doc_id", docID, "error", err.Error())
			WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
			return
		}
		_ = os.Remove(record.MetaPath)

		var verification map[string]interface{}
		if verifyAfter {
			afterActiveDocs, err := registryDocumentTotal(r, docStore, kb.ID)
			if err != nil {
				logger.Error("count active documents after restore failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "恢复文档失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			deletedCount, err := countDeletedDocuments(logger, cfg, kb.ID)
			if err != nil {
				logger.Error("count deleted documents after restore failed", "error", err.Error())
				WriteJSON(w, http.StatusInternalServerError, "恢复文档失败", map[string]string{"error_code": "INTERNAL_ERROR"})
				return
			}
			verification = buildDocumentVerification(beforeActiveDocs, afterActiveDocs, deletedCount, nil, nil)
		}

		WriteJSON(w, http.StatusOK, "恢复完成", map[string]interface{}{
			"doc_id":          docID,
			"original_doc_id": docID,
			"kb_id":           kb.ID,
			"restored_name":   finalRelative,
			"relative_path":   finalRelative,
			"graph_restored":  false,
			"note":            "仅恢复文档文件，图谱需重新构建",
			"verification":    verification,
		})
	})
}

// listDeletedDocumentItems 列出回收站条目；kbID 非空时按 meta.kb_id 过滤。
// 响应不包含 original_path/trash_path（绝对路径泄露修复）。
func listDeletedDocumentItems(logger *slog.Logger, cfg config.Config, kbID string) ([]map[string]interface{}, error) {
	trashDir, err := ensureTrashDir(cfg)
	if err != nil {
		return nil, err
	}
	metaFiles, err := collectDeletedMetaFiles(trashDir)
	if err != nil {
		return nil, err
	}

	nowMS := time.Now().UnixMilli()
	items := make([]map[string]interface{}, 0, len(metaFiles))
	for _, metaPath := range metaFiles {
		meta, err := loadDeletedDocumentMeta(metaPath)
		if err != nil {
			logger.Warn("load deleted document meta failed", "meta_path", metaPath, "error", err.Error())
			continue
		}
		if meta.DocID == "" {
			continue
		}
		if strings.TrimSpace(kbID) != "" && meta.KBID != kbID {
			continue
		}
		if meta.ExpiresAt > 0 && meta.ExpiresAt <= nowMS {
			cleanupExpiredDeletedItem(logger, metaPath, meta.TrashPath)
			continue
		}
		if strings.TrimSpace(meta.TrashPath) == "" {
			continue
		}
		if _, err := os.Stat(meta.TrashPath); err != nil {
			if os.IsNotExist(err) {
				_ = os.Remove(metaPath)
				continue
			}
			return nil, err
		}

		var remainingMS interface{}
		if meta.ExpiresAt > 0 {
			remaining := meta.ExpiresAt - nowMS
			if remaining < 0 {
				remaining = 0
			}
			remainingMS = remaining
		}

		items = append(items, map[string]interface{}{
			"doc_id":        meta.DocID,
			"kb_id":         meta.KBID,
			"name":          firstNonEmpty(meta.Name, filepath.Base(meta.TrashPath)),
			"ext":           firstNonEmpty(meta.Ext, strings.ToLower(filepath.Ext(meta.TrashPath))),
			"size":          meta.Size,
			"relative_path": meta.RelativePath,
			"deleted_at":    meta.DeletedAt,
			"expires_at":    meta.ExpiresAt,
			"remaining_ms":  remainingMS,
			"purge_graph":   deletedMetaPurgeGraph(meta),
			"operator":      meta.Operator,
		})
	}

	sort.Slice(items, func(i, j int) bool {
		left, _ := items[i]["deleted_at"].(int64)
		right, _ := items[j]["deleted_at"].(int64)
		return left > right
	})
	return items, nil
}

func ensureTrashDir(cfg config.Config) (string, error) {
	primary, err := ensureDir(cfg.DocumentStoragePath)
	if err != nil {
		return "", err
	}
	trashDir := filepath.Join(primary, documentsSoftDeleteDirName)
	if err := os.MkdirAll(trashDir, 0o755); err != nil {
		return "", err
	}
	return trashDir, nil
}

func ensureDir(path string) (string, error) {
	cleaned := filepath.Clean(strings.TrimSpace(path))
	if err := os.MkdirAll(cleaned, 0o755); err != nil {
		return "", err
	}
	return cleaned, nil
}

func collectDeletedMetaFiles(trashDir string) ([]string, error) {
	files := make([]string, 0)
	err := filepath.WalkDir(trashDir, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		if strings.HasSuffix(d.Name(), documentsSoftDeleteMetaExt) {
			files = append(files, filepath.Clean(path))
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return files, nil
}

func loadDeletedDocumentMeta(metaPath string) (*deletedDocumentMeta, error) {
	raw, err := os.ReadFile(metaPath)
	if err != nil {
		return nil, err
	}
	var meta deletedDocumentMeta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return nil, err
	}
	if meta.TrashPath != "" {
		meta.TrashPath = filepath.Clean(meta.TrashPath)
	}
	if meta.OriginalPath != "" {
		meta.OriginalPath = filepath.Clean(meta.OriginalPath)
	}
	return &meta, nil
}

func cleanupExpiredDeletedItem(logger *slog.Logger, metaPath string, trashPath string) {
	if strings.TrimSpace(trashPath) != "" {
		if err := os.Remove(trashPath); err != nil && !os.IsNotExist(err) {
			logger.Warn("remove expired trash file failed", "trash_path", trashPath, "error", err.Error())
		}
	}
	if err := os.Remove(metaPath); err != nil && !os.IsNotExist(err) {
		logger.Warn("remove expired trash meta failed", "meta_path", metaPath, "error", err.Error())
	}
}

func firstNonEmpty(value string, fallback string) string {
	trimmed := strings.TrimSpace(value)
	if trimmed != "" {
		return trimmed
	}
	return fallback
}

func deletedMetaPurgeGraph(meta *deletedDocumentMeta) bool {
	if meta == nil || meta.PurgeGraph == nil {
		return true
	}
	return *meta.PurgeGraph
}

func deletedMetaResponseMap(meta *deletedDocumentMeta) map[string]interface{} {
	if meta == nil {
		return nil
	}
	item := map[string]interface{}{
		"doc_id":        meta.DocID,
		"kb_id":         meta.KBID,
		"name":          firstNonEmpty(meta.Name, filepath.Base(meta.TrashPath)),
		"ext":           firstNonEmpty(meta.Ext, strings.ToLower(filepath.Ext(meta.TrashPath))),
		"size":          meta.Size,
		"relative_path": meta.RelativePath,
		"deleted_at":    meta.DeletedAt,
		"expires_at":    meta.ExpiresAt,
		"purge_graph":   deletedMetaPurgeGraph(meta),
		"operator":      meta.Operator,
	}
	if strings.TrimSpace(meta.SHA256) != "" {
		item["sha256"] = meta.SHA256
	}
	return item
}

func renameDocumentWithStamp(filename string, stamp int64) string {
	ext := filepath.Ext(filename)
	stem := strings.TrimSuffix(filename, ext)
	return stem + "_" + strconv.FormatInt(stamp, 10) + ext
}

func renameDocumentWithStampIndex(filename string, stamp int64, idx int) string {
	ext := filepath.Ext(filename)
	stem := strings.TrimSuffix(filename, ext)
	return stem + "_" + strconv.FormatInt(stamp, 10) + "_" + strconv.Itoa(idx) + ext
}

func parseBoolQuery(r *http.Request, key string, fallback bool) bool {
	raw := strings.TrimSpace(r.URL.Query().Get(key))
	if raw == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(raw)
	if err != nil {
		return fallback
	}
	return parsed
}

// registryDocumentTotal 返回 KB 的注册表行总数（验证计数使用）。
func registryDocumentTotal(r *http.Request, docStore adminDocumentStore, kbID string) (int, error) {
	result, err := docStore.ListDocumentsByKB(r.Context(), adminstore.DocumentListQuery{KBID: kbID, Page: 1, PageSize: 1})
	if err != nil {
		return 0, err
	}
	return result.Total, nil
}

// listAllRegistryRows 分页拉取 KB 的全部注册表行（clear 使用）。
func listAllRegistryRows(r *http.Request, docStore adminDocumentStore, kbID string) ([]adminstore.DocumentRegistryItem, error) {
	rows := make([]adminstore.DocumentRegistryItem, 0)
	page := 1
	for {
		result, err := docStore.ListDocumentsByKB(r.Context(), adminstore.DocumentListQuery{
			KBID:     kbID,
			Page:     page,
			PageSize: 500,
		})
		if err != nil {
			return nil, err
		}
		rows = append(rows, result.Items...)
		if len(result.Items) == 0 || len(rows) >= result.Total {
			break
		}
		page++
	}
	return rows, nil
}

func countDeletedDocuments(logger *slog.Logger, cfg config.Config, kbID string) (int, error) {
	items, err := listDeletedDocumentItems(logger, cfg, kbID)
	if err != nil {
		return 0, err
	}
	return len(items), nil
}

func findDeletedDocumentRecordByID(logger *slog.Logger, cfg config.Config, docID string) (*deletedDocumentRecord, error) {
	trashDir, err := ensureTrashDir(cfg)
	if err != nil {
		return nil, err
	}
	metaFiles, err := collectDeletedMetaFiles(trashDir)
	if err != nil {
		return nil, err
	}
	nowMS := time.Now().UnixMilli()
	for _, metaPath := range metaFiles {
		meta, err := loadDeletedDocumentMeta(metaPath)
		if err != nil {
			logger.Warn("load deleted document meta failed", "meta_path", metaPath, "error", err.Error())
			continue
		}
		if meta == nil || strings.TrimSpace(meta.DocID) != docID {
			continue
		}
		if meta.ExpiresAt > 0 && meta.ExpiresAt <= nowMS {
			cleanupExpiredDeletedItem(logger, metaPath, meta.TrashPath)
			continue
		}
		return &deletedDocumentRecord{Meta: meta, MetaPath: metaPath}, nil
	}
	return nil, nil
}

// softDeleteDocumentFile 将文档移入回收站并写 meta（含 kb 隔离字段），失败时回滚移动。
func softDeleteDocumentFile(cfg config.Config, input documentTrashInput) (deletedDocumentMeta, error) {
	info, err := os.Stat(input.FilePath)
	if err != nil {
		return deletedDocumentMeta{}, err
	}
	trashDir, err := ensureTrashDir(cfg)
	if err != nil {
		return deletedDocumentMeta{}, err
	}
	deletedAt := time.Now().UnixMilli()
	expiresAt := deletedAt + int64(softDeleteRetentionDays())*24*60*60*1000
	suffix := strings.ToLower(filepath.Ext(input.FilePath))
	trashPath := filepath.Join(trashDir, fmt.Sprintf("%s_%d%s", input.DocID, deletedAt, suffix))
	for idx := 1; ; idx++ {
		if _, err := os.Stat(trashPath); os.IsNotExist(err) {
			break
		}
		trashPath = filepath.Join(trashDir, fmt.Sprintf("%s_%d_%d%s", input.DocID, deletedAt, idx, suffix))
	}
	metaPath := trashPath + documentsSoftDeleteMetaExt
	if err := os.Rename(input.FilePath, trashPath); err != nil {
		return deletedDocumentMeta{}, err
	}

	meta := deletedDocumentMeta{
		DocID:        input.DocID,
		KBID:         input.KBID,
		TenantID:     input.TenantID,
		ProjectID:    input.ProjectID,
		RelativePath: input.RelativePath,
		SHA256:       input.SHA256,
		Name:         firstNonEmpty(input.Name, filepath.Base(input.FilePath)),
		Ext:          suffix,
		Size:         info.Size(),
		OriginalPath: filepath.Clean(input.FilePath),
		TrashPath:    filepath.Clean(trashPath),
		DeletedAt:    deletedAt,
		ExpiresAt:    expiresAt,
		PurgeGraph:   boolPtr(input.PurgeGraph),
	}
	if strings.TrimSpace(input.Operator) != "" {
		meta.Operator = input.Operator
	}
	raw, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		_ = os.Rename(trashPath, input.FilePath)
		return deletedDocumentMeta{}, err
	}
	if err := os.WriteFile(metaPath, raw, 0o644); err != nil {
		_ = os.Rename(trashPath, input.FilePath)
		return deletedDocumentMeta{}, err
	}
	return meta, nil
}

func softDeleteRetentionDays() int {
	raw := strings.TrimSpace(os.Getenv("DOC_SOFT_DELETE_RETENTION_DAYS"))
	if raw == "" {
		return defaultSoftDeleteRetention
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value < 1 {
		return defaultSoftDeleteRetention
	}
	return value
}

// hashFileContents 计算文件 SHA-256 与大小（恢复时重建注册表行使用）。
func hashFileContents(path string) (string, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	hasher := sha256.New()
	size, err := io.Copy(hasher, file)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(hasher.Sum(nil)), size, nil
}

func buildDocumentVerification(
	beforeActiveDocs int,
	afterActiveDocs int,
	deletedDocuments int,
	beforeGraph *graph.DocumentGraphStats,
	afterGraph *graph.DocumentGraphStats,
) map[string]interface{} {
	checks := map[string]interface{}{
		"active_documents_non_increase": afterActiveDocs <= beforeActiveDocs,
		"active_documents_delta":        afterActiveDocs - beforeActiveDocs,
	}
	if beforeGraph != nil && afterGraph != nil {
		checks["graph_documents_non_increase"] = afterGraph.Documents <= beforeGraph.Documents
		checks["graph_documents_delta"] = afterGraph.Documents - beforeGraph.Documents
		checks["graph_chunks_non_increase"] = afterGraph.Chunks <= beforeGraph.Chunks
		checks["graph_chunks_delta"] = afterGraph.Chunks - beforeGraph.Chunks
		checks["graph_relations_non_increase"] = afterGraph.Relations <= beforeGraph.Relations
		checks["graph_relations_delta"] = afterGraph.Relations - beforeGraph.Relations
	}
	return map[string]interface{}{
		"before": map[string]interface{}{
			"active_documents": beforeActiveDocs,
			"graph":            graphStatsMap(beforeGraph),
		},
		"after": map[string]interface{}{
			"active_documents":  afterActiveDocs,
			"deleted_documents": deletedDocuments,
			"graph":             graphStatsMap(afterGraph),
		},
		"checks": checks,
	}
}

func graphStatsMap(stats *graph.DocumentGraphStats) map[string]interface{} {
	if stats == nil {
		return nil
	}
	result := map[string]interface{}{
		"documents":       stats.Documents,
		"chunks":          stats.Chunks,
		"relations":       stats.Relations,
		"orphan_entities": stats.OrphanEntities,
	}
	if stats.Entities > 0 {
		result["entities"] = stats.Entities
	}
	return result
}

func hasGraphChanges(stats *graph.DocumentGraphStats) bool {
	if stats == nil {
		return false
	}
	return stats.Documents > 0 || stats.Chunks > 0 || stats.Relations > 0 || stats.OrphanEntities > 0
}

func currentOperator(r *http.Request) string {
	if r == nil {
		return ""
	}
	if value := strings.TrimSpace(r.Header.Get("x-auth-user-email")); value != "" {
		return value
	}
	return strings.TrimSpace(r.Header.Get("x-auth-user-name"))
}

func deleteModeName(softDelete bool) string {
	if softDelete {
		return "soft_delete"
	}
	return "hard_delete"
}

func boolToInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

func maxInt(left int, right int) int {
	if left > right {
		return left
	}
	return right
}

func maxInt64(left int64, right int64) int64 {
	if left > right {
		return left
	}
	return right
}

func minInt(left int, right int) int {
	if left < right {
		return left
	}
	return right
}

func boolPtr(value bool) *bool {
	v := value
	return &v
}
