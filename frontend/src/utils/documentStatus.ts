export type StatusSeverity = 'default' | 'info' | 'warning' | 'success' | 'error';

export type StatusView = {
 key: string;
 label: string;
 severity: StatusSeverity;
 unknown: boolean;
};

export type DocumentStatusInput = {
 status?: unknown;
 graphStatus?: unknown;
 vectorStatus?: unknown;
};

const DOCUMENT_STATUS_VIEWS: Record<string, StatusView> = {
 uploaded: { key: 'uploaded', label: '已上传，等待处理', severity: 'info', unknown: false },
 parsing: { key: 'parsing', label: '解析中', severity: 'warning', unknown: false },
 indexed: { key: 'indexed', label: '文档处理完成', severity: 'success', unknown: false },
 failed: { key: 'failed', label: '处理失败', severity: 'error', unknown: false },
 archived: { key: 'archived', label: '已归档', severity: 'default', unknown: false },
};

const PROJECTION_STATUS_VIEWS: Record<string, StatusView> = {
 pending: { key: 'pending', label: '待索引', severity: 'warning', unknown: false },
 stale: { key: 'stale', label: '需要重建', severity: 'warning', unknown: false },
 indexed: { key: 'indexed', label: '已索引', severity: 'success', unknown: false },
 skipped: { key: 'skipped', label: '未启用', severity: 'default', unknown: false },
 failed: { key: 'failed', label: '索引失败', severity: 'error', unknown: false },
};

const UNKNOWN_DOCUMENT_STATUS: StatusView = {
 key: 'unknown',
 label: '处理状态未知',
 severity: 'warning',
 unknown: true,
};

const UNKNOWN_PROJECTION_STATUS: StatusView = {
 key: 'unknown',
 label: '状态未知',
 severity: 'warning',
 unknown: true,
};

function normalizeStatus(value: unknown) {
 return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

export function getDocumentStatusView(value: unknown): StatusView {
 return DOCUMENT_STATUS_VIEWS[normalizeStatus(value)] ?? UNKNOWN_DOCUMENT_STATUS;
}

export function getProjectionStatusView(value: unknown): StatusView {
 return PROJECTION_STATUS_VIEWS[normalizeStatus(value)] ?? UNKNOWN_PROJECTION_STATUS;
}

export function getDocumentOverallStatus(input: DocumentStatusInput): StatusView {
 const document = getDocumentStatusView(input.status);
 const graph = getProjectionStatusView(input.graphStatus);
 const vector = getProjectionStatusView(input.vectorStatus);
 const views = [document, graph, vector];

 if (views.some((view) => view.key === 'failed')) {
 return { key: 'failed', label: '处理失败', severity: 'error', unknown: false };
 }
 if (views.some((view) => view.key === 'stale')) {
 return { key: 'stale', label: '需要重建', severity: 'warning', unknown: false };
 }
 if (document.key === 'parsing' || views.some((view) => view.key === 'pending')) {
 return { key: 'processing', label: '处理中', severity: 'warning', unknown: false };
 }
 if (views.some((view) => view.unknown)) {
 return { key: 'unknown', label: '状态未知', severity: 'warning', unknown: true };
 }
 if (views.some((view) => view.key === 'skipped')) {
 return { key: 'partial', label: '部分能力未启用', severity: 'default', unknown: false };
 }
 if (document.key === 'indexed' && graph.key === 'indexed' && vector.key === 'indexed') {
 return { key: 'completed', label: '已完成', severity: 'success', unknown: false };
 }
 return { key: 'unknown', label: '状态未知', severity: 'warning', unknown: true };
}
