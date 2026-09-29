package graph

type QueryRequest struct {
	Cypher     string                 `json:"cypher"`
	Parameters map[string]interface{} `json:"parameters,omitempty"`
}

type ExpandRequest struct {
	NodeID            string   `json:"nodeId"`
	Direction         string   `json:"direction,omitempty"`
	RelationshipTypes []string `json:"relationshipTypes,omitempty"`
	Limit             int      `json:"limit,omitempty"`
	// KBID 是 M4-R1 FIX #3 的硬作用域：普通工作台图谱接口禁止无 KB 查询。
	// 由 handler 从 header/query/body 归一化后注入，服务层空值直接拒绝。
	KBID string `json:"kb_id,omitempty"`
}

type Node struct {
	ID         string                 `json:"id"`
	Labels     []string               `json:"labels"`
	Properties map[string]interface{} `json:"properties"`
}

type Edge struct {
	ID         string                 `json:"id"`
	Source     string                 `json:"source"`
	Target     string                 `json:"target"`
	Type       string                 `json:"type"`
	Properties map[string]interface{} `json:"properties"`
}

type MediaResource struct {
	Filename  string   `json:"filename"`
	URL       string   `json:"url"`
	Thumbnail string   `json:"thumbnail,omitempty"`
	Duration  *float64 `json:"duration,omitempty"`
}

type NodeDetail struct {
	ID         string                     `json:"id"`
	Labels     []string                   `json:"labels"`
	Properties map[string]interface{}     `json:"properties"`
	Media      map[string][]MediaResource `json:"media"`
}

type Stats struct {
	NodeCount     int     `json:"nodeCount"`
	EdgeCount     int     `json:"edgeCount"`
	ExecutionTime float64 `json:"executionTime"`
}

type QueryResponse struct {
	Nodes []Node `json:"nodes"`
	Edges []Edge `json:"edges"`
	Stats Stats  `json:"stats"`
}

type GraphSchemaResponse struct {
	Labels         []GraphLabelSummary        `json:"labels"`
	Relationships  []GraphRelationshipSummary `json:"relationships"`
	Patterns       []GraphPatternSummary      `json:"patterns"`
	NodeProperties []GraphPropertySummary     `json:"nodeProperties"`
	RelProperties  []GraphPropertySummary     `json:"relProperties"`
	SampleQuery    string                     `json:"sampleQuery"`
	Stats          GraphSchemaStats           `json:"stats"`
}

type GraphLabelSummary struct {
	Label string `json:"label"`
	Count int64  `json:"count"`
}

type GraphRelationshipSummary struct {
	Type  string `json:"type"`
	Count int64  `json:"count"`
}

type GraphPatternSummary struct {
	SourceLabels []string `json:"sourceLabels"`
	Relationship string   `json:"relationship"`
	TargetLabels []string `json:"targetLabels"`
	Count        int64    `json:"count"`
}

type GraphPropertySummary struct {
	Owner string `json:"owner"`
	Key   string `json:"key"`
	Count int64  `json:"count"`
}

type GraphSchemaStats struct {
	NodeCount     int64   `json:"nodeCount"`
	EdgeCount     int64   `json:"edgeCount"`
	ExecutionTime float64 `json:"executionTime"`
}

type GraphCounts struct {
	NodeCount         int64 `json:"node_count"`
	RelationshipCount int64 `json:"relationship_count"`
}

type RuntimeConnectionInfo struct {
	URI             string `json:"uri"`
	Database        string `json:"database"`
	ConfigMode      string `json:"config_mode"`
	ConfigSource    string `json:"config_source"`
	ResolutionError string `json:"resolution_error,omitempty"`
}

type DocumentGraphStats struct {
	Documents      int64 `json:"documents"`
	Chunks         int64 `json:"chunks"`
	Relations      int64 `json:"relations"`
	OrphanEntities int64 `json:"orphan_entities"`
	Entities       int64 `json:"entities,omitempty"`
}
