import { api } from './api';

export interface DocumentItem {
  id: string;
  doc_id?: string;
  kb_id?: string;
  name: string;
  path?: string;
  relative_path?: string;
  ext: string;
  size: number;
  updated_at: number;
  status?: string;
  graph_status?: string;
  vector_status?: string;
  version?: number;
}

export interface UploadedDocumentItem {
  id: string;
  doc_id: string;
  name: string;
  path: string;
  ext: string;
  size: number;
}

export interface UploadDocumentsResult {
  uploaded: UploadedDocumentItem[];
  skipped: Array<{
    name?: string;
    reason: string;
  }>;
}

export async function listDocuments(kbId?: string) {
  const response = await api.get('/api/documents', kbId ? { headers: { 'X-KB-ID': kbId } } : undefined);
  return (response.data?.data?.items || []) as DocumentItem[];
}

export async function uploadDocuments(files: File[], kbId?: string) {
  const formData = new FormData();
  files.forEach((file) => formData.append('files', file));
  const response = await api.post('/api/documents/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
      ...(kbId ? { 'X-KB-ID': kbId } : {}),
    },
    timeout: 180000,
  });
  return response.data?.data as UploadDocumentsResult;
}

export interface DocumentVerificationSnapshot {
  before?: {
    active_documents: number;
    graph?: Record<string, number> | null;
  };
  after?: {
    active_documents: number;
    deleted_documents: number;
    graph?: Record<string, number> | null;
  };
  checks?: Record<string, boolean | number>;
}

export interface DeleteDocumentResult {
  doc_id: string;
  dry_run?: boolean;
  mode?: 'soft_delete' | 'hard_delete';
  file_deleted: boolean;
  file_action?: 'soft_deleted' | 'hard_deleted' | 'none';
  deleted_entry?: DeletedDocumentItem;
  candidate_file?: {
    exists: boolean;
    name?: string | null;
    path?: string | null;
  };
  verification?: DocumentVerificationSnapshot;
  verification_preview?: {
    before_active_documents: number;
    after_active_documents: number;
    after_graph_estimate?: Record<string, number> | null;
  };
  graph?: {
    documents: number;
    chunks: number;
    relations: number;
    orphan_entities: number;
  };
}

export interface DeleteDocumentOptions {
  purgeGraph?: boolean;
  softDelete?: boolean;
  dryRun?: boolean;
  verifyAfter?: boolean;
}

export interface DeletedDocumentItem {
  doc_id: string;
  name: string;
  ext: string;
  size: number;
  original_path: string;
  trash_path: string;
  deleted_at: number;
  expires_at: number;
  remaining_ms?: number | null;
  purge_graph: boolean;
  operator?: string;
}

export async function deleteDocument(docId: string, options: DeleteDocumentOptions | boolean = true, kbId?: string) {
  const normalized: DeleteDocumentOptions =
    typeof options === 'boolean'
      ? { purgeGraph: options }
      : options;
  const response = await api.delete(`/api/documents/${encodeURIComponent(docId)}`, {
    headers: kbId ? { 'X-KB-ID': kbId } : undefined,
    params: {
      purge_graph: normalized.purgeGraph ?? true,
      soft_delete: normalized.softDelete ?? true,
      dry_run: normalized.dryRun ?? false,
      verify_after: normalized.verifyAfter ?? true,
    },
    timeout: 180000,
  });
  return response.data?.data as DeleteDocumentResult;
}

export interface ClearDocumentsResult {
  dry_run?: boolean;
  mode?: 'soft_delete' | 'hard_delete';
  removed_files: number;
  soft_deleted_files?: number;
  failed_files?: number;
  errors_preview?: string[];
  candidate_files?: number;
  candidate_names_preview?: string[];
  verification?: DocumentVerificationSnapshot;
  graph?: {
    documents: number;
    chunks: number;
    relations: number;
    orphan_entities: number;
  };
}

export interface ClearDocumentsOptions {
  purgeGraph?: boolean;
  softDelete?: boolean;
  dryRun?: boolean;
  verifyAfter?: boolean;
}

export async function clearDocuments(options: ClearDocumentsOptions | boolean = true) {
  const normalized: ClearDocumentsOptions =
    typeof options === 'boolean'
      ? { purgeGraph: options }
      : options;
  const response = await api.delete('/api/documents', {
    params: {
      purge_graph: normalized.purgeGraph ?? true,
      soft_delete: normalized.softDelete ?? true,
      dry_run: normalized.dryRun ?? false,
      verify_after: normalized.verifyAfter ?? true,
    },
    timeout: 180000,
  });
  return response.data?.data as ClearDocumentsResult;
}

export async function listDeletedDocuments() {
  const response = await api.get('/api/documents/deleted');
  return (response.data?.data?.items || []) as DeletedDocumentItem[];
}

export async function restoreDocument(docId: string) {
  const response = await api.post(`/api/documents/${encodeURIComponent(docId)}/restore`);
  return response.data?.data as {
    doc_id: string;
    original_doc_id: string;
    restored_name: string;
    restored_path: string;
    graph_restored: boolean;
    note?: string;
    verification?: DocumentVerificationSnapshot;
  };
}
