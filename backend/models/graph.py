"""
图数据模型
"""
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional


class Node(BaseModel):
    """节点模型"""
    id: str
    labels: List[str]
    properties: Dict[str, Any]


class Edge(BaseModel):
    """边模型"""
    id: str
    source: str
    target: str
    type: str
    properties: Dict[str, Any]


class GraphStats(BaseModel):
    """图统计信息"""
    nodeCount: int = Field(..., alias="nodeCount")
    edgeCount: int = Field(..., alias="edgeCount")
    executionTime: float = Field(..., alias="executionTime")
    
    class Config:
        populate_by_name = True


class GraphData(BaseModel):
    """图数据模型"""
    nodes: List[Node]
    edges: List[Edge]
    stats: Optional[GraphStats] = None


class QueryRequest(BaseModel):
    """查询请求模型"""
    cypher: str
    parameters: Optional[Dict[str, Any]] = Field(default_factory=dict)


class QueryResponse(GraphData):
    """查询响应模型"""
    pass


class ExpandRequest(BaseModel):
    """展开节点请求模型"""
    nodeId: str = Field(..., alias="nodeId")
    direction: str = "both"  # "in", "out", "both"
    relationshipTypes: Optional[List[str]] = Field(default=None, alias="relationshipTypes")
    limit: int = 20
    
    class Config:
        populate_by_name = True


class MediaResource(BaseModel):
    """多媒体资源模型"""
    filename: str
    url: str
    thumbnail: Optional[str] = None
    duration: Optional[float] = None


class NodeDetail(BaseModel):
    """节点详情模型"""
    id: str
    labels: List[str]
    properties: Dict[str, Any]
    media: Dict[str, List[MediaResource]]
