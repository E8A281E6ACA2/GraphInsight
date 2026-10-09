import { api } from './api';

export interface GraphBuildResponse {
  job_id: number;
  status: string;
  message?: string;
  job?: Record<string, unknown>;
}

export async function triggerGraphBuild(payload?: {
  source?: string;
  force?: boolean;
  note?: string;
  docIds?: string[];
  complexExtraction?: boolean;
  reasoningProfile?: 'fast' | 'balanced' | 'deep';
}, kbId?: string) {
  const response = await api.post(
    '/api/graph/build',
    {
      source: payload?.source ?? 'documents',
      force: payload?.force ?? false,
      note: payload?.note ?? null,
      doc_ids: payload?.docIds ?? [],
      complex_extraction: payload?.complexExtraction ?? false,
      reasoning_profile: payload?.reasoningProfile,
    },
    { timeout: 180000, headers: kbId ? { 'X-KB-ID': kbId } : undefined }
  );
  return response.data?.data as GraphBuildResponse;
}
