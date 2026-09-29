import { api } from './api';
import type { WorkspaceKnowledgeBase } from '../types/api';

interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
  timestamp?: string;
  trace_id?: string;
}

type KnowledgeBasesPayload =
  | { items?: WorkspaceKnowledgeBase[] }
  | ApiEnvelope<{ items?: WorkspaceKnowledgeBase[] }>;

function unwrapItems(payload: KnowledgeBasesPayload): WorkspaceKnowledgeBase[] {
  if (
    payload &&
    typeof payload === 'object' &&
    'data' in payload &&
    'code' in payload &&
    'message' in payload
  ) {
    const envelope = payload as ApiEnvelope<{ items?: WorkspaceKnowledgeBase[] }>;
    return envelope.data?.items ?? [];
  }
  return (payload as { items?: WorkspaceKnowledgeBase[] }).items ?? [];
}

// 拉取当前用户可访问的 active 知识库目录（业务面 GET /api/knowledge-bases）。
// 后端按 graph:read 授权集合返回；无授权时返回空 items（不是错误）。
export async function listWorkspaceKnowledgeBases(): Promise<WorkspaceKnowledgeBase[]> {
  const response = await api.get<KnowledgeBasesPayload>('/api/knowledge-bases');
  return unwrapItems(response.data);
}
