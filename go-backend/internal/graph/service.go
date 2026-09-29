package graph

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
	"github.com/neo4j/neo4j-go-driver/v5/neo4j/dbtype"

	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/scope"
)

type Service struct {
	driver         neo4j.DriverWithContext
	database       string
	logger         *slog.Logger
	cfg            config.Config
	activeSig      string
	lastRefreshAt  time.Time
	refreshEvery   time.Duration
	connectionLock sync.RWMutex
}

var ErrNodeNotFound = errors.New("node not found")

// ErrKBScopeRequired 普通图谱接口缺少 KB 作用域（M4-R1 FIX #3，手册 §10.3）。
// 服务层是最后一道防线：即使 handler 被绕过，无 scope 也不执行任何 Neo4j 查询。
var ErrKBScopeRequired = scope.ErrScopeRequired()

const (
	defaultQueryTimeout = 20 * time.Second
	schemaQueryTimeout  = 5 * time.Second
)

func NewService(ctx context.Context, cfg config.Config, logger *slog.Logger) (*Service, error) {
	if cfg.Neo4jConfigSource == "admin" && strings.TrimSpace(cfg.Neo4jConfigResolutionErr) != "" {
		return nil, fmt.Errorf("resolve neo4j admin config failed: %s", cfg.Neo4jConfigResolutionErr)
	}
	service := &Service{
		logger:       logger,
		cfg:          cfg,
		refreshEvery: 5 * time.Second,
	}
	if err := service.refreshConnection(ctx, true); err != nil {
		return nil, err
	}
	return service, nil
}

func (s *Service) newSession(ctx context.Context) (neo4j.SessionWithContext, error) {
	if s == nil {
		return nil, errors.New("neo4j service is not initialized")
	}
	if err := s.ensureConnection(ctx); err != nil {
		return nil, err
	}

	s.connectionLock.RLock()
	driver := s.driver
	database := s.database
	s.connectionLock.RUnlock()
	if driver == nil {
		return nil, errors.New("neo4j driver is not initialized")
	}
	return driver.NewSession(ctx, neo4j.SessionConfig{DatabaseName: database}), nil
}

func (s *Service) ensureConnection(ctx context.Context) error {
	if s == nil {
		return errors.New("neo4j service is not initialized")
	}

	s.connectionLock.RLock()
	driverReady := s.driver != nil
	shouldRefresh := time.Since(s.lastRefreshAt) >= s.refreshEvery
	s.connectionLock.RUnlock()

	if !driverReady {
		return s.refreshConnection(ctx, true)
	}
	if shouldRefresh {
		return s.refreshConnection(ctx, false)
	}
	return nil
}

func (s *Service) refreshConnection(ctx context.Context, force bool) error {
	s.connectionLock.Lock()
	defer s.connectionLock.Unlock()

	if !force && s.driver != nil && time.Since(s.lastRefreshAt) < s.refreshEvery {
		return nil
	}

	resolved, err := config.ResolveNeo4jConfig(ctx, s.cfg)
	if err != nil {
		if s.driver != nil {
			s.lastRefreshAt = time.Now()
			if s.logger != nil {
				s.logger.Warn("refresh neo4j config failed; keeping current driver", "error", err.Error())
			}
			return nil
		}
		return fmt.Errorf("resolve neo4j config failed: %w", err)
	}

	signature := neo4jSignature(resolved)
	if !force && s.driver != nil && s.activeSig == signature {
		s.cfg = resolved
		s.lastRefreshAt = time.Now()
		return nil
	}

	driver, err := neo4j.NewDriverWithContext(
		resolved.Neo4jURI,
		neo4j.BasicAuth(resolved.Neo4jUser, resolved.Neo4jPassword, ""),
	)
	if err != nil {
		if s.driver != nil && !force {
			s.lastRefreshAt = time.Now()
			if s.logger != nil {
				s.logger.Warn("create refreshed neo4j driver failed; keeping current driver", "error", err.Error())
			}
			return nil
		}
		return fmt.Errorf("create neo4j driver failed: %w", err)
	}
	if err := driver.VerifyConnectivity(ctx); err != nil {
		_ = driver.Close(ctx)
		if s.driver != nil && !force {
			s.lastRefreshAt = time.Now()
			if s.logger != nil {
				s.logger.Warn("verify refreshed neo4j connectivity failed; keeping current driver", "error", err.Error())
			}
			return nil
		}
		return fmt.Errorf("verify neo4j connectivity failed: %w", err)
	}

	oldDriver := s.driver
	s.driver = driver
	s.database = resolved.Neo4jDatabase
	s.cfg = resolved
	s.activeSig = signature
	s.lastRefreshAt = time.Now()

	if oldDriver != nil {
		_ = oldDriver.Close(ctx)
	}
	return nil
}

func neo4jSignature(cfg config.Config) string {
	return strings.Join(
		[]string{
			strings.TrimSpace(cfg.Neo4jURI),
			strings.TrimSpace(cfg.Neo4jUser),
			strings.TrimSpace(cfg.Neo4jPassword),
			strings.TrimSpace(cfg.Neo4jDatabase),
			strings.TrimSpace(cfg.Neo4jConfigResolvedSource),
		},
		"|",
	)
}

func firstNonEmptyDatabase(value string) string {
	if trimmed := strings.TrimSpace(value); trimmed != "" {
		return trimmed
	}
	return "neo4j"
}

func (s *Service) Close(ctx context.Context) error {
	if s == nil {
		return nil
	}
	s.connectionLock.Lock()
	defer s.connectionLock.Unlock()
	if s.driver == nil {
		return nil
	}
	return s.driver.Close(ctx)
}

func (s *Service) RuntimeConnectionInfo() RuntimeConnectionInfo {
	if s == nil {
		return RuntimeConnectionInfo{}
	}
	s.connectionLock.RLock()
	defer s.connectionLock.RUnlock()
	return RuntimeConnectionInfo{
		URI:             strings.TrimSpace(s.cfg.Neo4jURI),
		Database:        firstNonEmptyDatabase(s.cfg.Neo4jDatabase),
		ConfigMode:      strings.TrimSpace(s.cfg.Neo4jConfigSource),
		ConfigSource:    strings.TrimSpace(s.cfg.Neo4jConfigResolvedSource),
		ResolutionError: strings.TrimSpace(s.cfg.Neo4jConfigResolutionErr),
	}
}

func (s *Service) CheckHealth(ctx context.Context) error {
	session, err := s.newSession(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = session.Close(ctx) }()

	result, err := session.Run(
		ctx,
		"RETURN 1 AS ok",
		nil,
		neo4j.WithTxTimeout(2*time.Second),
		neo4j.WithTxMetadata(map[string]any{"app": "graphinsight", "operation": "health"}),
	)
	if err != nil {
		return err
	}
	if !result.Next(ctx) {
		if err := result.Err(); err != nil {
			return err
		}
		return errors.New("neo4j health probe returned no rows")
	}
	return result.Err()
}

func (s *Service) CountGraph(ctx context.Context) (GraphCounts, error) {
	session, err := s.newSession(ctx)
	if err != nil {
		return GraphCounts{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	result, err := session.Run(ctx, `
CALL {
  MATCH (n)
  RETURN count(n) AS node_count
}
CALL {
  MATCH ()-[r]->()
  RETURN count(r) AS relationship_count
}
RETURN node_count, relationship_count
`, nil, neo4j.WithTxTimeout(5*time.Second), neo4j.WithTxMetadata(map[string]any{"app": "graphinsight", "operation": "monitor.countGraph"}))
	if err != nil {
		return GraphCounts{}, err
	}
	if !result.Next(ctx) {
		if err := result.Err(); err != nil {
			return GraphCounts{}, err
		}
		return GraphCounts{}, nil
	}
	record := result.Record()
	if err := result.Err(); err != nil {
		return GraphCounts{}, err
	}
	return GraphCounts{
		NodeCount:         int64FromRecord(record, "node_count"),
		RelationshipCount: int64FromRecord(record, "relationship_count"),
	}, nil
}

func (s *Service) GetDocumentGraphTotals(ctx context.Context) (DocumentGraphStats, error) {
	session, err := s.newSession(ctx)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	return s.getDocumentGraphTotals(ctx, session)
}

// GetDocumentGraphTotalsForKB 返回目标 KB 的文档图谱统计（与 getDocumentGraphTotals
// 同口径，但所有计数追加 kb_id 条件）。文档删除/清空的 verify_before/verify_after
// 差值校验必须使用本方法，避免把其他 KB 的图谱规模计入 delta（M4 FIX #5）。
func (s *Service) GetDocumentGraphTotalsForKB(ctx context.Context, kbID string) (DocumentGraphStats, error) {
	kbID = strings.TrimSpace(kbID)
	if kbID == "" {
		return DocumentGraphStats{}, fmt.Errorf("kb id is empty")
	}
	session, err := s.newSession(ctx)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	return s.getDocumentGraphTotalsForKB(ctx, session, kbID)
}

// PreviewDeleteDocumentGraph 预览删除单个文档的图谱投影。
// M3：doc_id 与 kb_id 双条件，跨 KB 的 doc_id 不可见也不可删。
func (s *Service) PreviewDeleteDocumentGraph(ctx context.Context, docID string, kbID string) (DocumentGraphStats, error) {
	docID = strings.TrimSpace(docID)
	kbID = strings.TrimSpace(kbID)
	if docID == "" || kbID == "" {
		return DocumentGraphStats{}, fmt.Errorf("doc id and kb id are required")
	}

	session, err := s.newSession(ctx)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	relations, err := s.countDocumentRelationsForDoc(ctx, session, docID, kbID)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	chunks, err := scalarCount(ctx, session, "MATCH (c:Chunk {doc_id: $doc_id, kb_id: $kb_id}) RETURN count(c) AS c", map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.previewDelete.chunks")
	if err != nil {
		return DocumentGraphStats{}, err
	}
	documents, err := scalarCount(ctx, session, "MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id}) RETURN count(d) AS c", map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.previewDelete.documents")
	if err != nil {
		return DocumentGraphStats{}, err
	}

	return DocumentGraphStats{
		Documents:      documents,
		Chunks:         chunks,
		Relations:      relations,
		OrphanEntities: 0,
	}, nil
}

// DeleteDocumentGraph 删除单个文档的图谱投影（Document/Chunk/关系/孤儿实体）。
// M3：全部 MATCH/DELETE 增加 kb_id 条件，防止 doc_id 碰撞跨 KB 误删。
func (s *Service) DeleteDocumentGraph(ctx context.Context, docID string, kbID string) (DocumentGraphStats, error) {
	docID = strings.TrimSpace(docID)
	kbID = strings.TrimSpace(kbID)
	if docID == "" || kbID == "" {
		return DocumentGraphStats{}, fmt.Errorf("doc id and kb id are required")
	}

	session, err := s.newSession(ctx)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	statsAny, err := session.ExecuteWrite(ctx, func(tx neo4j.ManagedTransaction) (any, error) {
		relations, err := scalarCountTx(ctx, tx, `
MATCH (:Document {doc_id: $doc_id, kb_id: $kb_id})-[r:HAS_CHUNK]->(:Chunk)
RETURN count(r) AS c
`, map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.delete.relations.hasChunk")
		if err != nil {
			return nil, err
		}
		mentions, err := scalarCountTx(ctx, tx, `
MATCH (:Chunk {doc_id: $doc_id, kb_id: $kb_id})-[r:MENTIONS]->(:Entity)
RETURN count(r) AS c
`, map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.delete.relations.mentions")
		if err != nil {
			return nil, err
		}
		entityRelations, err := scalarCountTx(ctx, tx, `
MATCH (:Entity)-[r]->(:Entity)
WHERE r.doc_id = $doc_id AND r.kb_id = $kb_id
RETURN count(r) AS c
`, map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.delete.relations.entities")
		if err != nil {
			return nil, err
		}
		relations += mentions + entityRelations

		if relations > 0 {
			if _, err := tx.Run(ctx, `
MATCH (:Entity)-[r]->(:Entity)
WHERE r.doc_id = $doc_id AND r.kb_id = $kb_id
DELETE r
`, map[string]any{"doc_id": docID, "kb_id": kbID}); err != nil {
				return nil, err
			}
		}

		chunks, err := scalarCountTx(ctx, tx, "MATCH (c:Chunk {doc_id: $doc_id, kb_id: $kb_id}) RETURN count(c) AS c", map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.delete.chunks")
		if err != nil {
			return nil, err
		}
		if chunks > 0 {
			if _, err := tx.Run(ctx, "MATCH (c:Chunk {doc_id: $doc_id, kb_id: $kb_id}) DETACH DELETE c", map[string]any{"doc_id": docID, "kb_id": kbID}); err != nil {
				return nil, err
			}
		}

		documents, err := scalarCountTx(ctx, tx, "MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id}) RETURN count(d) AS c", map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.delete.documents")
		if err != nil {
			return nil, err
		}
		if documents > 0 {
			if _, err := tx.Run(ctx, "MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id}) DETACH DELETE d", map[string]any{"doc_id": docID, "kb_id": kbID}); err != nil {
				return nil, err
			}
		}

		orphanEntities, err := cleanupOrphanEntitiesTx(ctx, tx, kbID)
		if err != nil {
			return nil, err
		}

		return DocumentGraphStats{
			Documents:      documents,
			Chunks:         chunks,
			Relations:      relations,
			OrphanEntities: orphanEntities,
		}, nil
	})
	if err != nil {
		return DocumentGraphStats{}, err
	}
	stats, _ := statsAny.(DocumentGraphStats)
	return stats, nil
}

// PreviewClearDocumentGraph 预览清空目标 KB 的图谱投影（scope 内统计）。
func (s *Service) PreviewClearDocumentGraph(ctx context.Context, kbID string) (DocumentGraphStats, error) {
	kbID = strings.TrimSpace(kbID)
	if kbID == "" {
		return DocumentGraphStats{}, fmt.Errorf("kb id is empty")
	}
	session, err := s.newSession(ctx)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	totals, err := s.getDocumentGraphTotalsForKB(ctx, session, kbID)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	totals.OrphanEntities = 0
	return totals, nil
}

// ClearDocumentGraph 清空目标 KB 的图谱投影，保留 source='document_ingest' 守卫，
// 所有语句追加 kb_id 条件；孤儿实体清理同样限定在该 KB 内。
func (s *Service) ClearDocumentGraph(ctx context.Context, kbID string) (DocumentGraphStats, error) {
	kbID = strings.TrimSpace(kbID)
	if kbID == "" {
		return DocumentGraphStats{}, fmt.Errorf("kb id is empty")
	}
	session, err := s.newSession(ctx)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	params := map[string]any{"kb_id": kbID}
	statsAny, err := session.ExecuteWrite(ctx, func(tx neo4j.ManagedTransaction) (any, error) {
		relations, err := scalarCountTx(ctx, tx, `
MATCH (d:Document {source: 'document_ingest', kb_id: $kb_id})-[r:HAS_CHUNK]->(:Chunk)
RETURN count(r) AS c
`, params, "documents.clear.relations.hasChunk")
		if err != nil {
			return nil, err
		}
		mentions, err := scalarCountTx(ctx, tx, `
MATCH (c:Chunk)-[r:MENTIONS]->(:Entity)
WHERE c.kb_id = $kb_id AND (c.source = 'document_ingest' OR c.doc_id IS NOT NULL)
RETURN count(r) AS c
`, params, "documents.clear.relations.mentions")
		if err != nil {
			return nil, err
		}
		entityRelations, err := scalarCountTx(ctx, tx, `
MATCH (:Entity)-[r]->(:Entity)
WHERE r.kb_id = $kb_id AND (r.source = 'document_ingest' OR r.doc_id IS NOT NULL)
RETURN count(r) AS c
`, params, "documents.clear.relations.entities")
		if err != nil {
			return nil, err
		}
		relations += mentions + entityRelations

		if relations > 0 {
			if _, err := tx.Run(ctx, `
MATCH (:Entity)-[r]->(:Entity)
WHERE r.kb_id = $kb_id AND (r.source = 'document_ingest' OR r.doc_id IS NOT NULL)
DELETE r
`, params); err != nil {
				return nil, err
			}
		}

		chunks, err := scalarCountTx(ctx, tx, `
MATCH (c:Chunk)
WHERE c.kb_id = $kb_id AND (c.source = 'document_ingest' OR c.doc_id IS NOT NULL)
RETURN count(c) AS c
`, params, "documents.clear.chunks")
		if err != nil {
			return nil, err
		}
		if chunks > 0 {
			if _, err := tx.Run(ctx, `
MATCH (c:Chunk)
WHERE c.kb_id = $kb_id AND (c.source = 'document_ingest' OR c.doc_id IS NOT NULL)
DETACH DELETE c
`, params); err != nil {
				return nil, err
			}
		}

		documents, err := scalarCountTx(ctx, tx, `
MATCH (d:Document {source: 'document_ingest', kb_id: $kb_id})
RETURN count(d) AS c
`, params, "documents.clear.documents")
		if err != nil {
			return nil, err
		}
		if documents > 0 {
			if _, err := tx.Run(ctx, `
MATCH (d:Document {source: 'document_ingest', kb_id: $kb_id})
DETACH DELETE d
`, params); err != nil {
				return nil, err
			}
		}

		orphanEntities, err := cleanupOrphanEntitiesTx(ctx, tx, kbID)
		if err != nil {
			return nil, err
		}

		return DocumentGraphStats{
			Documents:      documents,
			Chunks:         chunks,
			Relations:      relations,
			OrphanEntities: orphanEntities,
		}, nil
	})
	if err != nil {
		return DocumentGraphStats{}, err
	}
	stats, _ := statsAny.(DocumentGraphStats)
	return stats, nil
}

func (s *Service) ExecuteQuery(ctx context.Context, cypher string, parameters map[string]interface{}) (QueryResponse, error) {
	if strings.TrimSpace(cypher) == "" {
		return QueryResponse{}, fmt.Errorf("cypher is empty")
	}
	if parameters == nil {
		parameters = map[string]interface{}{}
	}

	start := time.Now()
	session, err := s.newSession(ctx)
	if err != nil {
		return QueryResponse{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	result, err := session.Run(
		ctx,
		cypher,
		parameters,
		neo4j.WithTxTimeout(defaultQueryTimeout),
		neo4j.WithTxMetadata(map[string]any{"app": "graphinsight", "operation": "query"}),
	)
	if err != nil {
		return QueryResponse{}, err
	}

	nodeMap := map[string]Node{}
	edges := make([]Edge, 0, 128)
	edgeSet := map[string]struct{}{}

	for result.Next(ctx) {
		record := result.Record()
		for _, value := range record.Values {
			parseValue(value, nodeMap, &edges, edgeSet)
		}
	}
	if err := result.Err(); err != nil {
		return QueryResponse{}, err
	}

	nodes := make([]Node, 0, len(nodeMap))
	for _, node := range nodeMap {
		nodes = append(nodes, node)
	}

	elapsed := time.Since(start).Seconds()
	return QueryResponse{
		Nodes: nodes,
		Edges: edges,
		Stats: Stats{
			NodeCount:     len(nodes),
			EdgeCount:     len(edges),
			ExecutionTime: float64(int(elapsed*1000)) / 1000,
		},
	}, nil
}

// requireScopedKBID 归一化并强制 KB 作用域（契约 §3.1/§10.3）。
func requireScopedKBID(kbID string) (string, error) {
	normalized, scopeErr := scope.NormalizeScopeID("kb_id", kbID)
	if scopeErr != nil {
		return "", scopeErr
	}
	if normalized == "" {
		return "", ErrKBScopeRequired
	}
	return normalized, nil
}

func (s *Service) DiscoverSchema(ctx context.Context, kbID string) (GraphSchemaResponse, error) {
	// M4-R1 FIX #3：schema 发现限定在单一 KB 作用域内，禁止全局目录返回。
	scopedKBID, err := requireScopedKBID(kbID)
	if err != nil {
		return GraphSchemaResponse{}, err
	}
	params := map[string]any{"kb_id": scopedKBID}
	start := time.Now()
	session, err := s.newSession(ctx)
	if err != nil {
		return GraphSchemaResponse{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	labels, err := s.discoverLabels(ctx, session, params)
	if err != nil && s.logger != nil {
		s.logger.Warn("schema label discovery failed", "error", err.Error())
	}
	relationships, err := s.discoverRelationships(ctx, session, params)
	if err != nil && s.logger != nil {
		s.logger.Warn("schema relationship discovery failed", "error", err.Error())
	}
	patterns, err := s.discoverPatterns(ctx, session, params)
	if err != nil && s.logger != nil {
		s.logger.Warn("schema pattern discovery failed", "error", err.Error())
	}
	nodeProperties, err := s.discoverNodeProperties(ctx, session, params)
	if err != nil && s.logger != nil {
		s.logger.Warn("schema node property discovery failed", "error", err.Error())
	}
	relProperties, err := s.discoverRelProperties(ctx, session, params)
	if err != nil && s.logger != nil {
		s.logger.Warn("schema relationship property discovery failed", "error", err.Error())
	}
	nodeCount, edgeCount, err := s.discoverCounts(ctx, session, params)
	if err != nil && s.logger != nil {
		s.logger.Warn("schema count discovery failed", "error", err.Error())
	}

	elapsed := time.Since(start).Seconds()
	return GraphSchemaResponse{
		Labels:         labels,
		Relationships:  relationships,
		Patterns:       patterns,
		NodeProperties: nodeProperties,
		RelProperties:  relProperties,
		SampleQuery:    buildSampleQuery(labels, patterns),
		Stats: GraphSchemaStats{
			NodeCount:     nodeCount,
			EdgeCount:     edgeCount,
			ExecutionTime: float64(int(elapsed*1000)) / 1000,
		},
	}, nil
}

func (s *Service) getDocumentGraphTotals(ctx context.Context, session neo4j.SessionWithContext) (DocumentGraphStats, error) {
	relations, err := s.countDocumentRelations(ctx, session)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	chunks, err := scalarCount(ctx, session, `
MATCH (c:Chunk)
WHERE c.source = 'document_ingest' OR c.doc_id IS NOT NULL
RETURN count(c) AS c
`, nil, "documents.totals.chunks")
	if err != nil {
		return DocumentGraphStats{}, err
	}
	documents, err := scalarCount(ctx, session, `
MATCH (d:Document {source: 'document_ingest'})
RETURN count(d) AS c
`, nil, "documents.totals.documents")
	if err != nil {
		return DocumentGraphStats{}, err
	}
	entities, err := scalarCount(ctx, session, `
MATCH (e:Entity {source: 'document_ingest'})
RETURN count(e) AS c
`, nil, "documents.totals.entities")
	if err != nil {
		return DocumentGraphStats{}, err
	}

	return DocumentGraphStats{
		Documents: documents,
		Chunks:    chunks,
		Relations: relations,
		Entities:  entities,
	}, nil
}

func (s *Service) countDocumentRelationsForDoc(ctx context.Context, session neo4j.SessionWithContext, docID string, kbID string) (int64, error) {
	hasChunk, err := scalarCount(ctx, session, `
MATCH (:Document {doc_id: $doc_id, kb_id: $kb_id})-[r:HAS_CHUNK]->(:Chunk)
RETURN count(r) AS c
`, map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.countRelationsForDoc.hasChunk")
	if err != nil {
		return 0, err
	}
	mentions, err := scalarCount(ctx, session, `
MATCH (:Chunk {doc_id: $doc_id, kb_id: $kb_id})-[r:MENTIONS]->(:Entity)
RETURN count(r) AS c
`, map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.countRelationsForDoc.mentions")
	if err != nil {
		return 0, err
	}
	entityRelations, err := scalarCount(ctx, session, `
MATCH (:Entity)-[r]->(:Entity)
WHERE r.doc_id = $doc_id AND r.kb_id = $kb_id
RETURN count(r) AS c
`, map[string]any{"doc_id": docID, "kb_id": kbID}, "documents.countRelationsForDoc.entities")
	if err != nil {
		return 0, err
	}
	return hasChunk + mentions + entityRelations, nil
}

// getDocumentGraphTotalsForKB 与 getDocumentGraphTotals 相同口径，但限定在目标 KB
// （PreviewClearDocumentGraph 的 scope 内统计使用）。
func (s *Service) getDocumentGraphTotalsForKB(ctx context.Context, session neo4j.SessionWithContext, kbID string) (DocumentGraphStats, error) {
	params := map[string]any{"kb_id": kbID}
	relations, err := s.countDocumentRelationsForKB(ctx, session, kbID)
	if err != nil {
		return DocumentGraphStats{}, err
	}
	chunks, err := scalarCount(ctx, session, `
MATCH (c:Chunk)
WHERE c.kb_id = $kb_id AND (c.source = 'document_ingest' OR c.doc_id IS NOT NULL)
RETURN count(c) AS c
`, params, "documents.totalsKB.chunks")
	if err != nil {
		return DocumentGraphStats{}, err
	}
	documents, err := scalarCount(ctx, session, `
MATCH (d:Document {source: 'document_ingest', kb_id: $kb_id})
RETURN count(d) AS c
`, params, "documents.totalsKB.documents")
	if err != nil {
		return DocumentGraphStats{}, err
	}
	entities, err := scalarCount(ctx, session, `
MATCH (e:Entity {source: 'document_ingest', kb_id: $kb_id})
RETURN count(e) AS c
`, params, "documents.totalsKB.entities")
	if err != nil {
		return DocumentGraphStats{}, err
	}

	return DocumentGraphStats{
		Documents: documents,
		Chunks:    chunks,
		Relations: relations,
		Entities:  entities,
	}, nil
}

func (s *Service) countDocumentRelationsForKB(ctx context.Context, session neo4j.SessionWithContext, kbID string) (int64, error) {
	params := map[string]any{"kb_id": kbID}
	hasChunk, err := scalarCount(ctx, session, `
MATCH (d:Document {source: 'document_ingest', kb_id: $kb_id})-[r:HAS_CHUNK]->(:Chunk)
RETURN count(r) AS c
`, params, "documents.countRelationsKB.hasChunk")
	if err != nil {
		return 0, err
	}
	mentions, err := scalarCount(ctx, session, `
MATCH (c:Chunk)-[r:MENTIONS]->(:Entity)
WHERE c.kb_id = $kb_id AND (c.source = 'document_ingest' OR c.doc_id IS NOT NULL)
RETURN count(r) AS c
`, params, "documents.countRelationsKB.mentions")
	if err != nil {
		return 0, err
	}
	entityRelations, err := scalarCount(ctx, session, `
MATCH (:Entity)-[r]->(:Entity)
WHERE r.kb_id = $kb_id AND (r.source = 'document_ingest' OR r.doc_id IS NOT NULL)
RETURN count(r) AS c
`, params, "documents.countRelationsKB.entities")
	if err != nil {
		return 0, err
	}
	return hasChunk + mentions + entityRelations, nil
}

func (s *Service) countDocumentRelations(ctx context.Context, session neo4j.SessionWithContext) (int64, error) {
	hasChunk, err := scalarCount(ctx, session, `
MATCH (:Document {source: 'document_ingest'})-[r:HAS_CHUNK]->(:Chunk)
RETURN count(r) AS c
`, nil, "documents.countRelations.hasChunk")
	if err != nil {
		return 0, err
	}
	mentions, err := scalarCount(ctx, session, `
MATCH (c:Chunk)-[r:MENTIONS]->(:Entity)
WHERE c.source = 'document_ingest' OR c.doc_id IS NOT NULL
RETURN count(r) AS c
`, nil, "documents.countRelations.mentions")
	if err != nil {
		return 0, err
	}
	entityRelations, err := scalarCount(ctx, session, `
MATCH (:Entity)-[r]->(:Entity)
WHERE r.source = 'document_ingest' OR r.doc_id IS NOT NULL
RETURN count(r) AS c
`, nil, "documents.countRelations.entities")
	if err != nil {
		return 0, err
	}
	return hasChunk + mentions + entityRelations, nil
}

func scalarCount(
	ctx context.Context,
	session neo4j.SessionWithContext,
	cypher string,
	params map[string]any,
	operation string,
) (int64, error) {
	result, err := session.Run(
		ctx,
		cypher,
		params,
		neo4j.WithTxTimeout(schemaQueryTimeout),
		neo4j.WithTxMetadata(map[string]any{"app": "graphinsight", "operation": operation}),
	)
	if err != nil {
		return 0, err
	}
	if !result.Next(ctx) {
		if err := result.Err(); err != nil {
			return 0, err
		}
		return 0, nil
	}
	record := result.Record()
	if err := result.Err(); err != nil {
		return 0, err
	}
	return int64FromRecord(record, "c"), nil
}

func scalarCountTx(
	ctx context.Context,
	tx neo4j.ManagedTransaction,
	cypher string,
	params map[string]any,
	operation string,
) (int64, error) {
	result, err := tx.Run(
		ctx,
		cypher,
		params,
	)
	if err != nil {
		return 0, err
	}
	if !result.Next(ctx) {
		if err := result.Err(); err != nil {
			return 0, err
		}
		return 0, nil
	}
	record := result.Record()
	if err := result.Err(); err != nil {
		return 0, err
	}
	_ = operation
	return int64FromRecord(record, "c"), nil
}

func cleanupOrphanEntitiesTx(ctx context.Context, tx neo4j.ManagedTransaction, kbID string) (int64, error) {
	params := map[string]any{"kb_id": kbID}
	count, err := scalarCountTx(ctx, tx, `
MATCH (e:Entity {source: 'document_ingest', kb_id: $kb_id})
WHERE NOT (e)--()
RETURN count(e) AS c
`, params, "documents.cleanupOrphanEntities.preview")
	if err != nil {
		return 0, err
	}
	if count == 0 {
		return 0, nil
	}
	if _, err := tx.Run(ctx, `
MATCH (e:Entity {source: 'document_ingest', kb_id: $kb_id})
WHERE NOT (e)--()
DELETE e
`, params); err != nil {
		return 0, err
	}
	return count, nil
}

func (s *Service) discoverLabels(ctx context.Context, session neo4j.SessionWithContext, params map[string]any) ([]GraphLabelSummary, error) {
	// M4-R1 FIX #3：不再使用全局目录 CALL db.labels()，改为按 kb_id 反查该 KB 实际存在的标签。
	result, err := session.Run(ctx, `
MATCH (n)
WHERE n.kb_id = $kb_id
WITH n LIMIT 1000
UNWIND labels(n) AS label
RETURN label, count(*) AS count
ORDER BY label ASC
LIMIT 50
`, params, schemaTxOptions("schema.labels")...)
	if err != nil {
		return nil, err
	}
	items := make([]GraphLabelSummary, 0, 16)
	for result.Next(ctx) {
		record := result.Record()
		items = append(items, GraphLabelSummary{
			Label: stringFromRecord(record, "label"),
			Count: int64FromRecord(record, "count"),
		})
	}
	return items, result.Err()
}

func (s *Service) discoverRelationships(ctx context.Context, session neo4j.SessionWithContext, params map[string]any) ([]GraphRelationshipSummary, error) {
	// M4-R1 FIX #3：不再使用全局目录 CALL db.relationshipTypes()，改为按 kb_id 反查该 KB 实际存在的关系类型。
	result, err := session.Run(ctx, `
MATCH (a)-[r]->(b)
WHERE a.kb_id = $kb_id
WITH r LIMIT 1000
RETURN type(r) AS type, count(*) AS count
ORDER BY type ASC
LIMIT 50
`, params, schemaTxOptions("schema.relationshipTypes")...)
	if err != nil {
		return nil, err
	}
	items := make([]GraphRelationshipSummary, 0, 16)
	for result.Next(ctx) {
		record := result.Record()
		items = append(items, GraphRelationshipSummary{
			Type:  stringFromRecord(record, "type"),
			Count: int64FromRecord(record, "count"),
		})
	}
	return items, result.Err()
}

func (s *Service) discoverPatterns(ctx context.Context, session neo4j.SessionWithContext, params map[string]any) ([]GraphPatternSummary, error) {
	result, err := session.Run(ctx, `
MATCH (a)
WHERE a.kb_id = $kb_id
WITH a LIMIT 200
MATCH (a)-[r]->(b)
RETURN labels(a) AS source_labels, type(r) AS relationship, labels(b) AS target_labels, count(*) AS count
ORDER BY count DESC
LIMIT 50
`, params, schemaTxOptions("schema.patterns")...)
	if err != nil {
		return nil, err
	}
	items := make([]GraphPatternSummary, 0, 16)
	for result.Next(ctx) {
		record := result.Record()
		items = append(items, GraphPatternSummary{
			SourceLabels: stringsFromRecord(record, "source_labels"),
			Relationship: stringFromRecord(record, "relationship"),
			TargetLabels: stringsFromRecord(record, "target_labels"),
			Count:        int64FromRecord(record, "count"),
		})
	}
	return items, result.Err()
}

func (s *Service) discoverNodeProperties(ctx context.Context, session neo4j.SessionWithContext, params map[string]any) ([]GraphPropertySummary, error) {
	result, err := session.Run(ctx, `
MATCH (n)
WHERE n.kb_id = $kb_id
WITH n LIMIT 200
UNWIND labels(n) AS label
UNWIND keys(n) AS key
RETURN label, key, count(*) AS count
ORDER BY count DESC
LIMIT 100
`, params, schemaTxOptions("schema.nodeProperties")...)
	if err != nil {
		return nil, err
	}
	items := make([]GraphPropertySummary, 0, 32)
	for result.Next(ctx) {
		record := result.Record()
		items = append(items, GraphPropertySummary{
			Owner: stringFromRecord(record, "label"),
			Key:   stringFromRecord(record, "key"),
			Count: int64FromRecord(record, "count"),
		})
	}
	return items, result.Err()
}

func (s *Service) discoverRelProperties(ctx context.Context, session neo4j.SessionWithContext, params map[string]any) ([]GraphPropertySummary, error) {
	result, err := session.Run(ctx, `
MATCH (a)
WHERE a.kb_id = $kb_id
WITH a LIMIT 200
MATCH (a)-[r]->()
UNWIND keys(r) AS key
RETURN type(r) AS type, key, count(*) AS count
ORDER BY count DESC
LIMIT 100
`, params, schemaTxOptions("schema.relProperties")...)
	if err != nil {
		return nil, err
	}
	items := make([]GraphPropertySummary, 0, 32)
	for result.Next(ctx) {
		record := result.Record()
		items = append(items, GraphPropertySummary{
			Owner: stringFromRecord(record, "type"),
			Key:   stringFromRecord(record, "key"),
			Count: int64FromRecord(record, "count"),
		})
	}
	return items, result.Err()
}

func (s *Service) discoverCounts(ctx context.Context, session neo4j.SessionWithContext, params map[string]any) (int64, int64, error) {
	result, err := session.Run(ctx, `
MATCH (n)
WHERE n.kb_id = $kb_id
WITH n LIMIT 1000
OPTIONAL MATCH (n)-[r]-()
RETURN count(DISTINCT n) AS node_count, count(DISTINCT r) AS edge_count
`, params, schemaTxOptions("schema.countSample")...)
	if err != nil {
		return 0, 0, err
	}
	if !result.Next(ctx) {
		if err := result.Err(); err != nil {
			return 0, 0, err
		}
		return 0, 0, nil
	}
	record := result.Record()
	if err := result.Err(); err != nil {
		return 0, 0, err
	}
	return int64FromRecord(record, "node_count"), int64FromRecord(record, "edge_count"), nil
}

func (s *Service) ExpandNode(ctx context.Context, req ExpandRequest) (QueryResponse, error) {
	nodeID := strings.TrimSpace(req.NodeID)
	if nodeID == "" {
		return QueryResponse{}, fmt.Errorf("node id is empty")
	}
	// M4-R1 FIX #3：扩展查询强制 KB 作用域，禁止跨 KB 逆邻域泄漏。
	scopedKBID, err := requireScopedKBID(req.KBID)
	if err != nil {
		return QueryResponse{}, err
	}
	direction := strings.ToLower(strings.TrimSpace(req.Direction))
	if direction == "" {
		direction = "both"
	}
	if direction != "in" && direction != "out" && direction != "both" {
		return QueryResponse{}, fmt.Errorf("invalid direction: %s", direction)
	}

	limit := req.Limit
	if limit <= 0 {
		limit = 20
	}
	if limit > 500 {
		limit = 500
	}

	pattern := "(n)-[r]-(m)"
	if direction == "in" {
		pattern = "(n)<-[r]-(m)"
	} else if direction == "out" {
		pattern = "(n)-[r]->(m)"
	}

	relTypes := make([]string, 0, len(req.RelationshipTypes))
	for _, item := range req.RelationshipTypes {
		v := strings.TrimSpace(item)
		if v == "" {
			continue
		}
		relTypes = append(relTypes, v)
	}

	parsedInt, parseErr := strconv.ParseInt(nodeID, 10, 64)
	hasIntID := parseErr == nil

	cypher := fmt.Sprintf(`
MATCH %s
WHERE (elementId(n) = $node_id_str OR ($has_int_id AND id(n) = $node_id_int))
  AND n.kb_id = $kb_id AND m.kb_id = $kb_id
  AND (size($rel_types) = 0 OR type(r) IN $rel_types)
RETURN n, r, m
LIMIT $limit
`, pattern)

	params := map[string]interface{}{
		"node_id_str": nodeID,
		"node_id_int": parsedInt,
		"has_int_id":  hasIntID,
		"kb_id":       scopedKBID,
		"rel_types":   relTypes,
		"limit":       limit,
	}
	return s.ExecuteQuery(ctx, cypher, params)
}

func (s *Service) GetNodeDetail(ctx context.Context, nodeID string, kbID string) (NodeDetail, error) {
	nodeID = strings.TrimSpace(nodeID)
	if nodeID == "" {
		return NodeDetail{}, fmt.Errorf("node id is empty")
	}
	// M4-R1 FIX #3：节点详情强制 KB 作用域，跨 KB 访问命中不到节点。
	scopedKBID, scopeErr := requireScopedKBID(kbID)
	if scopeErr != nil {
		return NodeDetail{}, scopeErr
	}

	parsedInt, err := strconv.ParseInt(nodeID, 10, 64)
	hasIntID := err == nil

	session, err := s.newSession(ctx)
	if err != nil {
		return NodeDetail{}, err
	}
	defer func() { _ = session.Close(ctx) }()

	result, err := session.Run(ctx, `
MATCH (n)
WHERE (elementId(n) = $node_id_str OR ($has_int_id AND id(n) = $node_id_int))
  AND n.kb_id = $kb_id
RETURN n
LIMIT 1
`, map[string]interface{}{
		"node_id_str": nodeID,
		"node_id_int": parsedInt,
		"has_int_id":  hasIntID,
		"kb_id":       scopedKBID,
	})
	if err != nil {
		return NodeDetail{}, err
	}
	if !result.Next(ctx) {
		if err := result.Err(); err != nil {
			return NodeDetail{}, err
		}
		return NodeDetail{}, ErrNodeNotFound
	}

	value, ok := result.Record().Get("n")
	if !ok {
		return NodeDetail{}, fmt.Errorf("node result missing field n")
	}
	node, ok := value.(dbtype.Node)
	if !ok {
		return NodeDetail{}, fmt.Errorf("node result has unexpected type %T", value)
	}

	id := node.ElementId
	if id == "" {
		id = strconv.FormatInt(node.Id, 10)
	}
	properties := normalizeMap(node.Props)
	return NodeDetail{
		ID:         id,
		Labels:     append([]string(nil), node.Labels...),
		Properties: properties,
		Media:      buildMediaFromProperties(properties),
	}, nil
}

func buildSampleQuery(labels []GraphLabelSummary, patterns []GraphPatternSummary) string {
	if len(patterns) > 0 {
		pattern := patterns[0]
		source := firstNonEmpty(pattern.SourceLabels)
		target := firstNonEmpty(pattern.TargetLabels)
		relationship := strings.TrimSpace(pattern.Relationship)
		if source != "" && target != "" && relationship != "" {
			return fmt.Sprintf(`// 自动发现：按当前图数据库最常见的关系模式查询
MATCH (n:%s)-[r:%s]->(m:%s)
RETURN n, r, m
LIMIT 80`, quoteCypherName(source), quoteCypherName(relationship), quoteCypherName(target))
		}
	}
	if len(labels) > 0 && strings.TrimSpace(labels[0].Label) != "" {
		return fmt.Sprintf(`// 自动发现：当前图数据库暂未发现关系，先查看节点
MATCH (n:%s)
RETURN n
LIMIT 80`, quoteCypherName(labels[0].Label))
	}
	return `// 当前图数据库暂未发现节点，先执行一个通用探测查询
MATCH (n)
RETURN n
LIMIT 80`
}

func quoteCypherName(value string) string {
	return "`" + strings.ReplaceAll(strings.TrimSpace(value), "`", "``") + "`"
}

func schemaTxOptions(operation string) []func(*neo4j.TransactionConfig) {
	return []func(*neo4j.TransactionConfig){
		neo4j.WithTxTimeout(schemaQueryTimeout),
		neo4j.WithTxMetadata(map[string]any{"app": "graphinsight", "operation": operation}),
	}
}

func firstNonEmpty(items []string) string {
	for _, item := range items {
		if strings.TrimSpace(item) != "" {
			return strings.TrimSpace(item)
		}
	}
	return ""
}

func stringFromRecord(record *neo4j.Record, key string) string {
	value, ok := record.Get(key)
	if !ok || value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	return fmt.Sprintf("%v", value)
}

func stringsFromRecord(record *neo4j.Record, key string) []string {
	value, ok := record.Get(key)
	if !ok || value == nil {
		return []string{}
	}
	switch v := value.(type) {
	case []string:
		return append([]string(nil), v...)
	case []interface{}:
		items := make([]string, 0, len(v))
		for _, item := range v {
			if item == nil {
				continue
			}
			items = append(items, fmt.Sprintf("%v", item))
		}
		return items
	default:
		return []string{fmt.Sprintf("%v", value)}
	}
}

func int64FromRecord(record *neo4j.Record, key string) int64 {
	value, ok := record.Get(key)
	if !ok || value == nil {
		return 0
	}
	switch v := value.(type) {
	case int:
		return int64(v)
	case int8:
		return int64(v)
	case int16:
		return int64(v)
	case int32:
		return int64(v)
	case int64:
		return v
	case float32:
		return int64(v)
	case float64:
		return int64(v)
	default:
		parsed, err := strconv.ParseInt(fmt.Sprintf("%v", value), 10, 64)
		if err != nil {
			return 0
		}
		return parsed
	}
}

func ClassifyQueryError(err error) (int, map[string]interface{}) {
	if err == nil {
		return 200, map[string]interface{}{}
	}
	msg := err.Error()
	lower := strings.ToLower(msg)

	if strings.Contains(lower, "cypher is empty") || strings.Contains(lower, "syntax") || strings.Contains(lower, "invalid input") {
		return 400, map[string]interface{}{
			"error":   "Invalid Cypher query",
			"code":    "INVALID_QUERY",
			"message": msg,
		}
	}
	if strings.Contains(lower, "invalid direction") {
		return 400, map[string]interface{}{
			"error":   "Invalid direction",
			"code":    "INVALID_DIRECTION",
			"message": "Direction must be 'in', 'out', or 'both'",
		}
	}
	if strings.Contains(lower, "node id is empty") {
		return 400, map[string]interface{}{
			"error":   "Invalid node id",
			"code":    "INVALID_NODE_ID",
			"message": "nodeId is required",
		}
	}
	if strings.Contains(lower, "dbms.memory.transaction.total.max") || strings.Contains(lower, "memorypooloutofmemory") || strings.Contains(lower, "memory pool") {
		return 503, map[string]interface{}{
			"error":   "Database transaction memory exhausted",
			"code":    "DATABASE_MEMORY_EXHAUSTED",
			"message": "Neo4j transaction memory is exhausted. Restart Neo4j or terminate the heavy query, then retry with a narrower Cypher query.",
		}
	}
	if strings.Contains(lower, "transaction timed out") || strings.Contains(lower, "transaction timeout") || strings.Contains(lower, "terminated due to timeout") {
		return 503, map[string]interface{}{
			"error":   "Query timeout",
			"code":    "QUERY_TIMEOUT",
			"message": "Cypher query timed out. Narrow the query with labels, relationship types, indexed properties, or a lower LIMIT.",
		}
	}
	if strings.Contains(lower, "unable to retrieve routing information") || strings.Contains(lower, "service unavailable") || strings.Contains(lower, "connection") || strings.Contains(lower, "connectivity") {
		return 503, map[string]interface{}{
			"error":   "Database unavailable",
			"code":    "DATABASE_UNAVAILABLE",
			"message": "Cannot connect to Neo4j database",
		}
	}
	return 500, map[string]interface{}{
		"error":   "Internal server error",
		"code":    "INTERNAL_ERROR",
		"message": msg,
	}
}

func parseValue(value interface{}, nodeMap map[string]Node, edges *[]Edge, edgeSet map[string]struct{}) {
	switch v := value.(type) {
	case dbtype.Node:
		upsertNode(v, nodeMap)
	case dbtype.Relationship:
		upsertRelationship(v, edges, edgeSet)
	case dbtype.Path:
		for _, n := range v.Nodes {
			upsertNode(n, nodeMap)
		}
		for _, r := range v.Relationships {
			upsertRelationship(r, edges, edgeSet)
		}
	case []interface{}:
		for _, item := range v {
			parseValue(item, nodeMap, edges, edgeSet)
		}
	case map[string]interface{}:
		for _, item := range v {
			parseValue(item, nodeMap, edges, edgeSet)
		}
	}
}

func upsertNode(node dbtype.Node, nodeMap map[string]Node) {
	nodeID := node.ElementId
	if nodeID == "" {
		nodeID = strconv.FormatInt(node.Id, 10)
	}
	if _, exists := nodeMap[nodeID]; exists {
		return
	}
	nodeMap[nodeID] = Node{
		ID:         nodeID,
		Labels:     append([]string(nil), node.Labels...),
		Properties: normalizeMap(node.Props),
	}
}

func upsertRelationship(rel dbtype.Relationship, edges *[]Edge, edgeSet map[string]struct{}) {
	edgeID := rel.ElementId
	if edgeID == "" {
		edgeID = strconv.FormatInt(rel.Id, 10)
	}
	if _, exists := edgeSet[edgeID]; exists {
		return
	}
	edgeSet[edgeID] = struct{}{}

	props := normalizeMap(rel.Props)
	edgeType := rel.Type
	if label, ok := props["label"].(string); ok && strings.TrimSpace(label) != "" {
		edgeType = label
	}

	source := rel.StartElementId
	if source == "" {
		source = strconv.FormatInt(rel.StartId, 10)
	}
	target := rel.EndElementId
	if target == "" {
		target = strconv.FormatInt(rel.EndId, 10)
	}

	*edges = append(*edges, Edge{
		ID:         edgeID,
		Source:     source,
		Target:     target,
		Type:       edgeType,
		Properties: props,
	})
}

func normalizeMap(input map[string]interface{}) map[string]interface{} {
	if len(input) == 0 {
		return map[string]interface{}{}
	}
	result := make(map[string]interface{}, len(input))
	for k, v := range input {
		result[k] = normalizeValue(v)
	}
	return result
}

func normalizeValue(value interface{}) interface{} {
	switch v := value.(type) {
	case nil:
		return nil
	case string, bool, float64, float32, int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64:
		return v
	case []byte:
		return string(v)
	case time.Time:
		return v.Format(time.RFC3339)
	case []interface{}:
		items := make([]interface{}, 0, len(v))
		for _, item := range v {
			items = append(items, normalizeValue(item))
		}
		return items
	case map[string]interface{}:
		return normalizeMap(v)
	default:
		if s, ok := v.(fmt.Stringer); ok {
			return s.String()
		}
		return fmt.Sprintf("%v", v)
	}
}
