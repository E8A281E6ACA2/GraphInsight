import { useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, Card, CardContent, Chip, Stack, Typography } from '@mui/material';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import AdminLayout from '../../components/Admin/AdminLayout';
import { knowledgeBasesApi, jobsApi } from '../../services/adminService';
import { listDocuments } from '../../services/documents';
import { listWorkspaceKnowledgeBases } from '../../services/kbService';
import type { AdminKnowledgeBase, JobItem } from '../../types/admin';
import type { DocumentItem } from '../../services/documents';
import { useGraphStore } from '../../store/graphStore';
import { getErrorMessage } from '../../utils/errorMessage';

type Region<T> = { loading: boolean; data: T | null; error: string | null };
const pending = <T,>(): Region<T> => ({ loading: true, data: null, error: null });
const documentLabels: Record<string, string> = { uploaded: '已上传', parsing: '处理中', indexed: '可用', failed: '失败' };
const indexLabels: Record<string, string> = { pending: '待处理', indexed: '已索引', stale: '过期', failed: '失败' };

export default function KnowledgeBaseDetailPage() {
  const { kbId = '' } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [result, setResult] = useState({ id: '', kb: pending<AdminKnowledgeBase>(), documents: pending<DocumentItem[]>(), jobs: pending<JobItem[]>(), workspace: pending<boolean>() });
  const [enterError, setEnterError] = useState<string | null>(null);
  const [entering, setEntering] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    const requestGeneration = ++generation.current;
    let current = true;
    const initial = { id: kbId, kb: pending<AdminKnowledgeBase>(), documents: pending<DocumentItem[]>(), jobs: pending<JobItem[]>(), workspace: pending<boolean>() };
    const run = async () => {
      if (!current) return;
      setResult(initial);
      setEnterError(null);
      setEntering(false);
      const update = <K extends 'kb' | 'documents' | 'jobs' | 'workspace'>(key: K, value: typeof initial[K]) => { if (current) setResult((previous) => previous.id === kbId ? { ...previous, [key]: value } : previous); };
      void knowledgeBasesApi.get(kbId).then((data) => update('kb', { loading: false, data, error: null })).catch((error) => update('kb', { loading: false, data: null, error: getErrorMessage(error, '基本信息加载失败') }));
      void listDocuments(kbId).then((data) => update('documents', { loading: false, data, error: null })).catch((error) => update('documents', { loading: false, data: null, error: getErrorMessage(error, '文档列表加载失败') }));
      void jobsApi.getJobs({ kb_id: kbId, page: 1, page_size: 10 }).then((data) => update('jobs', { loading: false, data: data.items, error: null })).catch((error) => update('jobs', { loading: false, data: null, error: getErrorMessage(error, '任务列表加载失败') }));
      void listWorkspaceKnowledgeBases().then((data) => update('workspace', { loading: false, data: data.some((item) => item.kb_id === kbId && item.status === 'active'), error: null })).catch((error) => update('workspace', { loading: false, data: null, error: getErrorMessage(error, '工作台授权目录读取失败') }));
    };
    void Promise.resolve().then(run);
    return () => { current = false; if (generation.current === requestGeneration) generation.current += 1; };
  }, [kbId]);
  const view = result.id === kbId ? result : { id: kbId, kb: pending<AdminKnowledgeBase>(), documents: pending<DocumentItem[]>(), jobs: pending<JobItem[]>(), workspace: pending<boolean>() };
  const kb = view.kb.data;
  const enterWorkspace = async () => {
    if (!kb || kb.id !== kbId || kb.status !== 'active' || !view.workspace.data || entering) return;
    setEntering(true); setEnterError(null);
    const requestedId = kbId;
    const requestedGeneration = generation.current;
    try {
      const allowed = await listWorkspaceKnowledgeBases();
      if (generation.current !== requestedGeneration || window.location.pathname !== `/admin/knowledge-bases/${encodeURIComponent(requestedId)}`) return;
      if (!allowed.some((item) => item.kb_id === requestedId && item.status === 'active')) { setEnterError('当前知识库不在工作台授权目录中'); return; }
      useGraphStore.getState().setActiveKbId(requestedId);
      navigate('/workspace');
    } catch (error) { if (generation.current === requestedGeneration && window.location.pathname === `/admin/knowledge-bases/${encodeURIComponent(requestedId)}`) setEnterError(getErrorMessage(error, '进入工作台失败')); }
    finally { if (generation.current === requestedGeneration && window.location.pathname === `/admin/knowledge-bases/${encodeURIComponent(requestedId)}`) setEntering(false); }
  };
  const returnQuery = searchParams.toString();
  return <AdminLayout title="知识库详情" subtitle="只读资源与独立区域状态；管理权限不等于工作台使用权限">
    <Stack spacing={2}>
      <Button onClick={() => navigate(`/admin/knowledge-bases${returnQuery ? `?${returnQuery}` : ''}`)}>返回目录</Button>
      <Card data-testid="kb-basic"><CardContent>{view.kb.loading ? <Typography>加载详情...</Typography> : view.kb.error ? <Alert severity="error">{view.kb.error}</Alert> : kb ? <Stack spacing={1}>
        <Stack direction="row" spacing={1}><Typography variant="h5">{kb.name}</Typography><Chip label={kb.status === 'active' ? '启用' : kb.status === 'archived' ? '已归档' : '未知状态'} /></Stack>
        <Typography>{kb.description || '暂无描述'}</Typography><Typography>作用域：{kb.tenant_id} / {kb.project_id}</Typography>
        <Button variant="contained" disabled={kb.status !== 'active' || kb.id !== kbId || !view.workspace.data || entering} onClick={() => void enterWorkspace()}>进入工作台</Button>
        {view.workspace.loading ? <Typography>核对工作台授权目录...</Typography> : view.workspace.error ? <Alert severity="warning">{view.workspace.error}</Alert> : !view.workspace.data ? <Alert severity="info">当前知识库不在工作台授权 active 目录中</Alert> : null}
        {enterError && <Alert severity="error">{enterError}</Alert>}
      </Stack> : <Typography>暂无基本信息</Typography>}</CardContent></Card>
      <Card><CardContent><Typography variant="h6">基础统计</Typography><Typography>暂无 KB 级统计</Typography><Typography variant="caption">文档列表仅展示本次响应数据量，不代表全库总量。</Typography></CardContent></Card>
      <Card data-testid="kb-documents"><CardContent><Typography variant="h6">文档列表</Typography>{view.documents.loading ? <Typography>加载文档...</Typography> : view.documents.error ? <Alert severity="error">{view.documents.error}</Alert> : !view.documents.data?.length ? <Typography>暂无文档</Typography> : view.documents.data.map((doc) => <Box key={doc.id} sx={{ py: 1 }}><Typography>{doc.name}</Typography><Typography variant="caption">文档：{documentLabels[doc.status ?? ''] ?? '未知'} · 图谱：{indexLabels[doc.graph_status ?? ''] ?? '未知'} · 向量：{indexLabels[doc.vector_status ?? ''] ?? '未知'} · 版本：{doc.version ?? '未知'}</Typography></Box>)}</CardContent></Card>
      <Card data-testid="kb-jobs"><CardContent><Typography variant="h6">最近任务</Typography>{view.jobs.loading ? <Typography>加载任务...</Typography> : view.jobs.error ? <Alert severity="error">{view.jobs.error}</Alert> : !view.jobs.data?.length ? <Typography>暂无该 KB 任务</Typography> : view.jobs.data.map((job) => <Box key={job.id} sx={{ py: 1 }}><Typography>{job.job_type}</Typography><Typography>{job.status}</Typography></Box>)}</CardContent></Card>
    </Stack>
  </AdminLayout>;
}
