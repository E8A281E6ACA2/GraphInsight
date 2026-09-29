package httpserver

import (
	"context"

	"graphinsight/go-backend/internal/graph"
)

type graphService interface {
	CheckHealth(ctx context.Context) error
	RuntimeConnectionInfo() graph.RuntimeConnectionInfo
	CountGraph(ctx context.Context) (graph.GraphCounts, error)
	GetDocumentGraphTotals(ctx context.Context) (graph.DocumentGraphStats, error)
	GetDocumentGraphTotalsForKB(ctx context.Context, kbID string) (graph.DocumentGraphStats, error)
	PreviewDeleteDocumentGraph(ctx context.Context, docID string, kbID string) (graph.DocumentGraphStats, error)
	DeleteDocumentGraph(ctx context.Context, docID string, kbID string) (graph.DocumentGraphStats, error)
	PreviewClearDocumentGraph(ctx context.Context, kbID string) (graph.DocumentGraphStats, error)
	ClearDocumentGraph(ctx context.Context, kbID string) (graph.DocumentGraphStats, error)
	ExecuteQuery(ctx context.Context, cypher string, parameters map[string]interface{}) (graph.QueryResponse, error)
	DiscoverSchema(ctx context.Context, kbID string) (graph.GraphSchemaResponse, error)
	ExpandNode(ctx context.Context, req graph.ExpandRequest) (graph.QueryResponse, error)
	GetNodeDetail(ctx context.Context, nodeID string, kbID string) (graph.NodeDetail, error)
}
