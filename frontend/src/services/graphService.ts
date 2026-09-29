import axios from 'axios';
import { api } from './api';
import { requireActiveKbId } from './kbScope';
import type { QueryRequest, QueryResponse, ExpandRequest, NodeDetailResponse, GraphSchemaSummary } from '../types/api';
import type { GraphData } from '../store/graphStore';

interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
  timestamp?: string;
  trace_id?: string;
}

// 自定义错误类
export class GraphServiceError extends Error {
  public code: string;
  public details?: unknown;

  constructor(message: string, code: string, details?: unknown) {
    super(message);
    this.name = 'GraphServiceError';
    this.code = code;
    this.details = details;
  }
}

function getResponseMessage(data: unknown, fallback: string) {
  if (data && typeof data === 'object' && 'message' in data) {
    const message = (data as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) {
      return message;
    }
  }
  return fallback;
}

function unwrapApiData<T>(payload: T | ApiEnvelope<T>): T {
  if (
    payload &&
    typeof payload === 'object' &&
    'data' in payload &&
    'code' in payload &&
    'message' in payload
  ) {
    return (payload as ApiEnvelope<T>).data;
  }
  return payload as T;
}

// 发现当前图数据库结构，用于生成更贴近真实数据的默认查询
export async function getGraphSchema(): Promise<GraphSchemaSummary> {
  try {
    const response = await api.get<GraphSchemaSummary | ApiEnvelope<GraphSchemaSummary>>('/api/graph/schema');
    return unwrapApiData(response.data);
  } catch (error: unknown) {
    if (axios.isAxiosError(error) && error.response?.status === 503) {
      throw new GraphServiceError(
        'Database unavailable',
        'DATABASE_UNAVAILABLE',
        error.response.data
      );
    }
    throw new GraphServiceError(
      'Failed to discover graph schema',
      'SCHEMA_DISCOVERY_FAILED',
      error
    );
  }
}

// 执行 Cypher 查询
export async function executeQuery(cypher: string, parameters?: Record<string, unknown>): Promise<GraphData> {
  try {
    const request: QueryRequest = { cypher, parameters };
    const response = await api.post<QueryResponse | ApiEnvelope<QueryResponse>>('/api/query', request);
    const data = unwrapApiData(response.data);

    return {
      nodes: data.nodes,
      edges: data.edges,
      stats: data.stats,
    };
  } catch (error: unknown) {
    if (axios.isAxiosError(error) && error.response?.status === 400) {
      throw new GraphServiceError(
        getResponseMessage(error.response.data, 'Invalid Cypher query'),
        'INVALID_QUERY',
        error.response.data
      );
    } else if (axios.isAxiosError(error) && error.response?.status === 503) {
      throw new GraphServiceError(
        'Database unavailable',
        'DATABASE_UNAVAILABLE',
        error.response.data
      );
    } else if (axios.isAxiosError(error) && error.response?.status === 500) {
      throw new GraphServiceError(
        'Internal server error',
        'SERVER_ERROR',
        error.response.data
      );
    }
    throw new GraphServiceError(
      'Failed to execute query',
      'UNKNOWN_ERROR',
      error
    );
  }
}

// 获取节点详情
export async function getNodeDetail(nodeId: string): Promise<NodeDetailResponse> {
  try {
    const response = await api.get<NodeDetailResponse | ApiEnvelope<NodeDetailResponse>>(`/api/node/${nodeId}`);
    return unwrapApiData(response.data);
  } catch (error: unknown) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      throw new GraphServiceError(
        'Node not found',
        'NODE_NOT_FOUND',
        error.response.data
      );
    }
    throw new GraphServiceError(
      'Failed to fetch node details',
      'UNKNOWN_ERROR',
      error
    );
  }
}

// 展开节点（获取邻居节点）
export async function expandNode(
  nodeId: string,
  direction: 'in' | 'out' | 'both' = 'both',
  relationshipTypes?: string[],
  limit: number = 20
): Promise<GraphData> {
  try {
    // 展开在 body 显式携带 kb_id（与拦截器注入的 X-KB-ID 头一致）；未选择知识库时本地拦截。
    const kbId = requireActiveKbId();
    const request: ExpandRequest = {
      nodeId,
      direction,
      relationshipTypes,
      limit,
      kb_id: kbId,
    };
    const response = await api.post<QueryResponse | ApiEnvelope<QueryResponse>>('/api/expand', request);
    const data = unwrapApiData(response.data);

    return {
      nodes: data.nodes,
      edges: data.edges,
      stats: data.stats,
    };
  } catch (error: unknown) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      throw new GraphServiceError(
        'Node not found',
        'NODE_NOT_FOUND',
        error.response.data
      );
    }
    throw new GraphServiceError(
      'Failed to expand node',
      'UNKNOWN_ERROR',
      error
    );
  }
}
