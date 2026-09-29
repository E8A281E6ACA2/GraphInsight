/**
 * 问答链路追踪页面
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Container,
  Divider,
  Drawer,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TablePagination,
  TableRow,
  TextField,
  Typography,
} from '@mui/material';
import { Folder, Search, Settings, WorkHistory } from '@mui/icons-material';
import { useNavigate } from 'react-router-dom';
import AdminLayout from '../../components/Admin/AdminLayout';
import AdminRefreshButton from '../../components/Admin/AdminRefreshButton';
import AdminLoadingButton from '../../components/Admin/AdminLoadingButton';
import { LoadingState } from '../../components/Loading/AppleSpinner';
import { qaTracesApi } from '../../services/adminService';
import { listWorkspaceKnowledgeBases } from '../../services/kbService';
import type { WorkspaceKnowledgeBase } from '../../types/api';
import type {
  QACostSummary,
  QACostModelBreakdown,
  QATraceDetail,
  QATraceGenerationSnapshot,
  QATraceItem,
  QATraceStatus,
  QATraceType,
  RetrievalDiagnosticsResult,
} from '../../types/admin';
import { getErrorMessage } from '../../utils/errorMessage';

type QATraceTypeFilter = QATraceType | '';
type QATraceStatusFilter = QATraceStatus | '';

const formatDate = (value?: string) => {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN');
};

const qaTypeLabel = (value: string) => (value === 'deep_research' ? '深度调研' : '文档问答');

const formatPercent = (value: number) => `${(Number(value || 0) * 100).toFixed(1)}%`;

const formatCost = (value: number, currency: string) => `${currency || 'USD'} ${Number(value || 0).toFixed(6)}`;

const getTopCostModel = (models: QACostModelBreakdown[]) =>
  models.find((item) => {
    const model = String(item.model || '').trim().toLowerCase();
    return model !== '' && model !== 'unknown' && Number(item.total_tokens || 0) > 0;
  });

const formatReasoningProfile = (value?: string) => {
  if (value === 'fast') return 'fast';
  if (value === 'deep') return 'deep';
  if (value === 'balanced') return 'balanced';
  return '-';
};

const asRecord = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
};

const formatValue = (value: unknown, fallback = '-') => {
  if (value === null || typeof value === 'undefined' || value === '') return fallback;
  return String(value);
};

const recommendationLabel = (value: string) => {
  const labels: Record<string, string> = {
    enable_vector_store: '启用向量库',
    configure_embedding: '配置 Embedding',
    reindex_or_import_documents: '导入文档或重建索引',
    verify_graph_mentions: '检查图谱实体关联',
  };
  return labels[value] || value;
};

const sourceLabel = (value: string) => {
  const labels: Record<string, string> = {
    keyword: '全文',
    vector: '向量',
    graph: '图谱扩展',
    keyword_fallback: '全文回退',
  };
  return labels[value] || value;
};

const QATracesPage: React.FC = () => {
  const navigate = useNavigate();
  const [items, setItems] = useState<QATraceItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(20);
  const [qaType, setQaType] = useState<QATraceTypeFilter>('');
  const [status, setStatus] = useState<QATraceStatusFilter>('');
  const [keyword, setKeyword] = useState('');
  const [traceId, setTraceId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [detailOpen, setDetailOpen] = useState(false);
  const [detail, setDetail] = useState<QATraceDetail | null>(null);
  const [costSummary, setCostSummary] = useState<QACostSummary | null>(null);
  const [diagnostics, setDiagnostics] = useState<RetrievalDiagnosticsResult | null>(null);
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false);
  const [knowledgeBases, setKnowledgeBases] = useState<WorkspaceKnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState('');

  useEffect(() => {
    const loadKnowledgeBases = async () => {
      try {
        const kbs = await listWorkspaceKnowledgeBases();
        setKnowledgeBases(kbs);
      } catch (err: unknown) {
        setError(getErrorMessage(err, '知识库列表加载失败'));
      }
    };
    void loadKnowledgeBases();
  }, []);

  const loadTraces = useCallback(async () => {
    if (!selectedKbId) {
      setItems([]);
      setTotal(0);
      setCostSummary(null);
      return;
    }
    setLoading(true);
    setError('');
    try {
      const data = await qaTracesApi.getTraces({
        kb_id: selectedKbId,
        page: page + 1,
        page_size: rowsPerPage,
        qa_type: qaType || undefined,
        status: status || undefined,
        keyword: keyword.trim() || undefined,
        trace_id: traceId.trim() || undefined,
      });
      setItems(Array.isArray(data.items) ? data.items : []);
      setTotal(Number(data.total || 0));
      const costData = await qaTracesApi.getCostSummary({
        kb_id: selectedKbId,
        qa_type: qaType || undefined,
        status: status || undefined,
        window_hours: 24,
      });
      setCostSummary(costData);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '问答追踪加载失败'));
    } finally {
      setLoading(false);
    }
  }, [keyword, page, qaType, rowsPerPage, selectedKbId, status, traceId]);

  useEffect(() => {
    void loadTraces();
  }, [loadTraces]);

  const openDetail = async (item: QATraceItem) => {
    if (!selectedKbId) return;
    setError('');
    setDiagnostics(null);
    try {
      const data = await qaTracesApi.getTrace(item.id, item.kb_id || selectedKbId);
      setDetail(data);
      setDetailOpen(true);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '追踪详情加载失败'));
    }
  };

  const extractReasoningProfile = (detail: QATraceDetail | null) => {
    const snapshot = (detail?.generation_snapshot || {}) as QATraceGenerationSnapshot;
    return snapshot.reasoning_profile;
  };

  const runRetrievalDiagnostics = async () => {
    if (!detail?.question) return;
    const kbId = detail.kb_id || selectedKbId;
    if (!kbId) {
      setError('缺少知识库归属，无法运行检索诊断');
      return;
    }
    setDiagnosticsLoading(true);
    setError('');
    try {
      const data = await qaTracesApi.runRetrievalDiagnostics({
        question: detail.question,
        kb_id: kbId,
        top_k: detail.top_k || 5,
        modes: ['keyword', 'vector', 'hybrid', 'graph_hybrid'],
      });
      setDiagnostics(data);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '检索诊断失败'));
    } finally {
      setDiagnosticsLoading(false);
    }
  };

  const renderTraceSummary = (detail: QATraceDetail) => {
    const retrieval = asRecord(detail.retrieval_snapshot);
    const orchestrator = asRecord(retrieval.orchestrator);
    const sources = asRecord(orchestrator.sources);
    const generation = asRecord(detail.generation_snapshot);
    const usage = asRecord(generation.usage);
    const skipReasons = Object.entries(sources)
      .map(([source, sourceTrace]) => {
        const skipReason = formatValue(asRecord(sourceTrace).skip_reason, '');
        return skipReason ? `${sourceLabel(source)}: ${skipReason}` : '';
      })
      .filter(Boolean);

    return (
      <Card variant="outlined">
        <CardContent>
          <Stack spacing={2}>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <Chip size="small" label={`检索模式: ${formatValue(orchestrator.mode)}`} />
              <Chip size="small" label={`召回/引用: ${detail.retrieval_count} / ${detail.citation_count}`} />
              <Chip size="small" label={`生成: ${formatValue(generation.mode)}`} />
              <Chip size="small" label={`模型: ${formatValue(generation.model || detail.model)}`} />
              <Chip size="small" label={`档位: ${formatReasoningProfile(extractReasoningProfile(detail))}`} />
            </Stack>
            <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'repeat(3, 1fr)' }, gap: 1.5 }}>
              <Box>
                <Typography variant="caption" color="text.secondary">检索耗时</Typography>
                <Typography variant="body2">{formatValue(orchestrator.duration_ms)} ms</Typography>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">总 Token</Typography>
                <Typography variant="body2">{formatValue(usage.total_tokens)}</Typography>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">Trace ID</Typography>
                <Typography variant="body2" noWrap>{formatValue(detail.trace_id)}</Typography>
              </Box>
            </Box>
            {skipReasons.length > 0 && (
              <Alert severity="warning">
                {skipReasons.join('；')}
              </Alert>
            )}
          </Stack>
        </CardContent>
      </Card>
    );
  };

  const renderDiagnosticsSummary = () => {
    if (!diagnostics) return null;
    const modeSummary = diagnostics.summary?.modes || {};
    const recommendations = diagnostics.summary?.recommendations || [];

    return (
      <Card variant="outlined">
        <CardContent>
          <Stack spacing={2}>
            <Box>
              <Typography variant="subtitle2">实时检索诊断</Typography>
              <Typography variant="body2" color="text.secondary">
                最优模式: {formatValue(diagnostics.summary?.best_mode)}，最慢模式: {formatValue(diagnostics.summary?.slowest_mode)}
              </Typography>
            </Box>
            {recommendations.length > 0 && (
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                {recommendations.map((item) => (
                  <Chip key={item} color="warning" size="small" label={recommendationLabel(item)} />
                ))}
              </Stack>
            )}
            <TableContainer>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>模式</TableCell>
                    <TableCell>命中</TableCell>
                    <TableCell>耗时</TableCell>
                    <TableCell>来源</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {Object.entries(modeSummary).map(([mode, summary]) => (
                    <TableRow key={mode}>
                      <TableCell>{mode}</TableCell>
                      <TableCell>{summary.hit_count}</TableCell>
                      <TableCell>{summary.duration_ms} ms</TableCell>
                      <TableCell>
                        {Object.entries(summary.source_counts || {})
                          .map(([source, count]) => `${sourceLabel(source)} ${count}`)
                          .join(' / ') || '-'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </Stack>
        </CardContent>
      </Card>
    );
  };

  const actionBar = (
    <AdminRefreshButton onClick={loadTraces} loading={loading} />
  );

  return (
    <AdminLayout title="问答追踪" subtitle="问题、检索证据、模型响应与引用链路" actions={actionBar}>
      <Container maxWidth="lg" sx={{ px: 0 }}>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {costSummary && (
          <Card sx={{ mb: 2 }}>
            <CardContent>
              {(() => {
                const topModel = getTopCostModel(costSummary.models || []);
                return (
              <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} alignItems={{ md: 'center' }}>
                <Box sx={{ flex: 1 }}>
                  <Typography variant="subtitle2" color="text.secondary">24h 模型成本估算</Typography>
                  <Typography variant="h5">
                    {formatCost(costSummary.estimated_cost, costSummary.currency)}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    价格来源: {costSummary.pricing_source}，未配置价格时成本按 0 估算
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="subtitle2" color="text.secondary">调用</Typography>
                  <Typography variant="h6">{costSummary.total_calls}</Typography>
                </Box>
                <Box>
                  <Typography variant="subtitle2" color="text.secondary">Token</Typography>
                  <Typography variant="h6">{costSummary.total_tokens}</Typography>
                </Box>
                <Box>
                  <Typography variant="subtitle2" color="text.secondary">成功率</Typography>
                  <Typography variant="h6">{formatPercent(costSummary.success_rate)}</Typography>
                </Box>
                <Box>
                  <Typography variant="subtitle2" color="text.secondary">Top 模型</Typography>
                  <Typography variant="body2">
                    {topModel ? `${topModel.model} · ${topModel.total_tokens} tokens` : '暂无有效模型数据'}
                  </Typography>
                </Box>
              </Stack>
                );
              })()}
            </CardContent>
          </Card>
        )}

        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
              <TextField
                select
                label="知识库"
                value={selectedKbId}
                onChange={(e) => {
                  setSelectedKbId(e.target.value);
                  setPage(0);
                }}
                sx={{ minWidth: 220 }}
              >
                {knowledgeBases.length === 0 && (
                  <MenuItem value="" disabled>
                    无可访问的知识库
                  </MenuItem>
                )}
                {knowledgeBases.map((kb) => (
                  <MenuItem key={kb.kb_id} value={kb.kb_id}>
                    {kb.name || kb.kb_id}
                  </MenuItem>
                ))}
              </TextField>
              <TextField select label="类型" value={qaType} onChange={(e) => setQaType(e.target.value as QATraceTypeFilter)} sx={{ minWidth: 160 }}>
                <MenuItem value="">全部</MenuItem>
                <MenuItem value="docqa">文档问答</MenuItem>
                <MenuItem value="deep_research">深度调研</MenuItem>
              </TextField>
              <TextField select label="状态" value={status} onChange={(e) => setStatus(e.target.value as QATraceStatusFilter)} sx={{ minWidth: 140 }}>
                <MenuItem value="">全部</MenuItem>
                <MenuItem value="success">成功</MenuItem>
                <MenuItem value="failed">失败</MenuItem>
              </TextField>
              <TextField label="trace_id" value={traceId} onChange={(e) => setTraceId(e.target.value)} sx={{ minWidth: 220 }} />
              <TextField label="关键词" value={keyword} onChange={(e) => setKeyword(e.target.value)} sx={{ flex: 1 }} />
              <Button variant="contained" startIcon={<Search />} onClick={loadTraces}>
                查询
              </Button>
            </Stack>
          </CardContent>
        </Card>

        {!selectedKbId ? (
          <Alert severity="info" sx={{ mb: 2 }}>
            请先在上方选择知识库。问答追踪数据按知识库隔离，未选择知识库时不加载任何记录。
          </Alert>
        ) : (
          <Card>
            <TableContainer>
              <Table>
                <TableHead>
                  <TableRow>
                    <TableCell>ID</TableCell>
                    <TableCell>类型</TableCell>
                    <TableCell>状态</TableCell>
                    <TableCell>档位</TableCell>
                    <TableCell>问题</TableCell>
                    <TableCell>检索/引用</TableCell>
                    <TableCell>延迟</TableCell>
                    <TableCell>时间</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {loading ? (
                    <TableRow>
                      <TableCell colSpan={8} sx={{ p: 0 }}>
                        <LoadingState label="正在加载问答追踪" minHeight={240} />
                      </TableCell>
                    </TableRow>
                  ) : items.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={8} align="center">暂无问答追踪记录</TableCell>
                    </TableRow>
                  ) : (
                    items.map((item) => (
                      <TableRow key={item.id} hover sx={{ cursor: 'pointer' }} onClick={() => openDetail(item)}>
                        <TableCell>{item.id}</TableCell>
                        <TableCell>{qaTypeLabel(item.qa_type)}</TableCell>
                        <TableCell>
                          <Chip
                            size="small"
                            label={item.status === 'success' ? '成功' : '失败'}
                            color={item.status === 'success' ? 'success' : 'error'}
                          />
                        </TableCell>
                        <TableCell>
                          <Chip
                            size="small"
                            variant="outlined"
                            label={formatReasoningProfile(item.reasoning_profile)}
                          />
                        </TableCell>
                        <TableCell sx={{ maxWidth: 360 }}>
                          <Typography variant="body2" noWrap>{item.question}</Typography>
                          {item.trace_id && (
                            <Typography variant="caption" color="text.secondary" noWrap>
                              {item.trace_id}
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell>{item.retrieval_count} / {item.citation_count}</TableCell>
                        <TableCell>{item.latency_ms ?? 0} ms</TableCell>
                        <TableCell>{formatDate(item.created_at)}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </TableContainer>
            <TablePagination
              component="div"
              count={total}
              page={page}
              rowsPerPage={rowsPerPage}
              rowsPerPageOptions={[10, 20, 25, 50, 100]}
              onPageChange={(_, next) => setPage(next)}
              onRowsPerPageChange={(e) => {
                setRowsPerPage(parseInt(e.target.value, 10));
                setPage(0);
              }}
            />
          </Card>
        )}

        <Drawer anchor="right" open={detailOpen} onClose={() => setDetailOpen(false)} PaperProps={{ sx: { width: { xs: '100%', md: 720 }, p: 3 } }}>
          {detail && (
            <Stack spacing={2}>
              <Box>
                <Typography variant="h6">追踪详情 #{detail.id}</Typography>
                <Typography variant="body2" color="text.secondary">{detail.trace_id || '-'}</Typography>
              </Box>
              <Alert severity={detail.status === 'success' ? 'success' : 'error'}>
                {qaTypeLabel(detail.qa_type)} · {detail.status} · {detail.latency_ms ?? 0} ms
              </Alert>
              {renderTraceSummary(detail)}
              <Card variant="outlined">
                <CardContent>
                  <Stack spacing={1.5}>
                    <Typography variant="subtitle2">诊断动作</Typography>
                    <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                      <AdminLoadingButton
                        variant="contained"
                        size="small"
                        startIcon={<Search />}
                        loading={diagnosticsLoading}
                        label="跑检索诊断"
                        onClick={runRetrievalDiagnostics}
                      />
                      <Button size="small" variant="outlined" startIcon={<Settings />} onClick={() => navigate('/admin/config')}>
                        配置中心
                      </Button>
                      <Button size="small" variant="outlined" startIcon={<WorkHistory />} onClick={() => navigate('/admin/jobs?job_type=build_graph')}>
                        建图任务
                      </Button>
                      <Button size="small" variant="outlined" startIcon={<Folder />} onClick={() => navigate('/admin/knowledge-base')}>
                        知识库
                      </Button>
                    </Stack>
                  </Stack>
                </CardContent>
              </Card>
              {renderDiagnosticsSummary()}
              <Divider />
              <Box>
                <Typography variant="subtitle2">问题</Typography>
                <Typography variant="body2">{detail.question}</Typography>
              </Box>
              <Box>
                <Typography variant="subtitle2">回答摘要</Typography>
                <Typography variant="body2" whiteSpace="pre-wrap">{detail.answer_preview || '-'}</Typography>
              </Box>
              <Box>
                <Typography variant="subtitle2">检索快照</Typography>
                <Box component="pre" sx={{ whiteSpace: 'pre-wrap', fontSize: 12, bgcolor: 'rgba(15,31,45,0.04)', p: 2, borderRadius: 1, maxHeight: 260, overflow: 'auto' }}>
                  {JSON.stringify(detail.retrieval_snapshot || {}, null, 2)}
                </Box>
              </Box>
              <Box>
                <Typography variant="subtitle2">模型生成</Typography>
                <Stack direction="row" spacing={1} sx={{ mb: 1, mt: 1 }}>
                  <Chip
                    size="small"
                    variant="outlined"
                    label={`档位: ${formatReasoningProfile(extractReasoningProfile(detail))}`}
                  />
                </Stack>
                <Box component="pre" sx={{ whiteSpace: 'pre-wrap', fontSize: 12, bgcolor: 'rgba(15,31,45,0.04)', p: 2, borderRadius: 1, maxHeight: 220, overflow: 'auto' }}>
                  {JSON.stringify(detail.generation_snapshot || {}, null, 2)}
                </Box>
              </Box>
              <Box>
                <Typography variant="subtitle2">响应快照</Typography>
                <Box component="pre" sx={{ whiteSpace: 'pre-wrap', fontSize: 12, bgcolor: 'rgba(15,31,45,0.04)', p: 2, borderRadius: 1, maxHeight: 220, overflow: 'auto' }}>
                  {JSON.stringify(detail.response_snapshot || {}, null, 2)}
                </Box>
              </Box>
            </Stack>
          )}
        </Drawer>
      </Container>
    </AdminLayout>
  );
};

export default QATracesPage;
