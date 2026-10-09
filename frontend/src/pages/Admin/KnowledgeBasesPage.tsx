import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent, DialogTitle, Stack, TextField, Typography } from '@mui/material';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useGraphStore } from '../../store/graphStore';
import AdminLayout from '../../components/Admin/AdminLayout';
import AdminLoadingButton from '../../components/Admin/AdminLoadingButton';
import AdminRefreshButton from '../../components/Admin/AdminRefreshButton';
import { knowledgeBasesApi } from '../../services/adminService';
import type { AdminKnowledgeBase } from '../../types/admin';
import { getErrorMessage } from '../../utils/errorMessage';

const emptyForm = { name: '', slug: '', description: '' };
const statusLabel = (status: string) => status === 'active' ? '启用' : status === 'archived' ? '已归档' : status === 'deleting' ? '清理中' : status;

export default function KnowledgeBasesPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialScope = { tenant_id: searchParams.get('tenant_id') ?? '', project_id: searchParams.get('project_id') ?? '' };
  const [items, setItems] = useState<AdminKnowledgeBase[]>([]);
  const [status, setStatus] = useState('active');
  const [query, setQuery] = useState('');
  const [scopeInput, setScopeInput] = useState(initialScope);
  const [parentScope, setParentScope] = useState<typeof scopeInput | null>(() => [initialScope.tenant_id, initialScope.project_id].every((value) => /^[a-z0-9][a-z0-9_-]{1,99}$/.test(value)) ? initialScope : null);
  const [formScope, setFormScope] = useState<typeof scopeInput | null>(null);
  const contextGeneration = useRef(0);
  const mounted = useRef(false);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [totalPages, setTotalPages] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [formMode, setFormMode] = useState<'closed' | 'create' | 'edit'>('closed');
  const [editing, setEditing] = useState<AdminKnowledgeBase | null>(null);
  const [form, setForm] = useState(emptyForm);
  const loadSequence = useRef(0);
  const [saving, setSaving] = useState(false);
  const [archiving, setArchiving] = useState<string | null>(null);

  const load = useCallback(async (requestedStatus = status, requestedScope = parentScope) => {
    if (!requestedScope?.tenant_id.trim() || !requestedScope.project_id.trim()) return;
    const sequence = ++loadSequence.current;
    const generation = contextGeneration.current;
    const isCurrent = () => mounted.current && sequence === loadSequence.current && generation === contextGeneration.current;
    setLoading(true); setError(null);
    try {
      const data = await knowledgeBasesApi.list({ ...requestedScope, status: requestedStatus, page, page_size: pageSize });
      if (!isCurrent()) return;
      setTotalPages(data.total_pages);
      const lastPage = Math.max(1, data.total_pages);
      if (page > lastPage) {
        loadSequence.current += 1;
        setItems([]);
        setPage(lastPage);
        return;
      }
      setItems(data.items);
    }
    catch (err) { if (isCurrent()) setError(getErrorMessage(err, '知识库目录加载失败')); }
    finally { if (isCurrent()) setLoading(false); }
  }, [page, pageSize, parentScope, status]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; contextGeneration.current += 1; loadSequence.current += 1; }; }, []);
  useEffect(() => { if (parentScope) void load(status, parentScope); }, [load, parentScope, status]);

  const filtered = useMemo(() => items.filter((item) => `${item.name} ${item.description ?? ''} ${item.project_id} ${item.tenant_id}`.toLowerCase().includes(query.trim().toLowerCase())), [items, query]);
  const refreshCurrent = useRef(load);
  refreshCurrent.current = load;
  const invalidateContext = () => { contextGeneration.current += 1; loadSequence.current += 1; setItems([]); setError(null); setFormMode('closed'); };
  const applyScope = () => {
    const next = { tenant_id: scopeInput.tenant_id.trim().toLowerCase(), project_id: scopeInput.project_id.trim().toLowerCase() };
    if (![next.tenant_id, next.project_id].every((value) => /^[a-z0-9][a-z0-9_-]{1,99}$/.test(value))) { setError('请输入合法的 Tenant ID 和 Project ID（2–100个小写字母、数字、下划线或连字符）'); return; }
    invalidateContext(); setPage(1); setParentScope(next); setSearchParams(next, { replace: true });
  };
  const openCreate = () => { if (!parentScope) return; setFormScope({ ...parentScope }); setEditing(null); setForm({ ...emptyForm }); setFormMode('create'); };
  const openEdit = (item: AdminKnowledgeBase) => { if (item.status !== 'active') return; setEditing(item); setForm({ name: item.name, slug: item.slug ?? '', description: item.description ?? '' }); setFormMode('edit'); };
  const save = async () => {
    if (!form.name.trim() || saving || (!editing && !formScope)) return;
    const generation = contextGeneration.current;
    setSaving(true); setError(null);
    try {
      if (editing) await knowledgeBasesApi.update(editing.id, form);
      else await knowledgeBasesApi.create({ ...formScope!, name: form.name.trim(), slug: form.slug.trim() || undefined, description: form.description.trim() || undefined });
      if (!mounted.current) return;
      if (generation === contextGeneration.current) { setEditing(null); setForm(emptyForm); setFormMode('closed'); }
      await refreshCurrent.current();
    }
    catch (err) { if (mounted.current && generation === contextGeneration.current) setError(getErrorMessage(err, '知识库保存失败')); }
    finally { if (mounted.current) setSaving(false); }
  };
  const restore = async (item: AdminKnowledgeBase) => {
    if (archiving) return;
    setArchiving(item.id); setError(null);
    const generation = contextGeneration.current;
    try { await knowledgeBasesApi.update(item.id, { status: 'active' }); if (mounted.current) await refreshCurrent.current(); }
    catch (err) { if (mounted.current && generation === contextGeneration.current) setError(getErrorMessage(err, '知识库恢复失败')); }
    finally { setArchiving(null); }
  };
  const archive = async (item: AdminKnowledgeBase) => {
    if (archiving || !window.confirm(`确认归档知识库“${item.name}”吗？归档不是物理删除。`)) return;
    setArchiving(item.id); setError(null);
    const generation = contextGeneration.current;
    try {
      await knowledgeBasesApi.archive(item.id);
      if (useGraphStore.getState().activeKbId === item.id) useGraphStore.getState().setActiveKbId(null);
      if (mounted.current) await refreshCurrent.current();
    }
    catch (err) { if (mounted.current && generation === contextGeneration.current) setError(getErrorMessage(err, '知识库归档失败')); }
    finally { setArchiving(null); }
  };

  return <AdminLayout title="知识库目录" subtitle="管理知识库资源、状态与进入详情">
    <Stack spacing={2}>
      {error && <Alert severity="error">{error}</Alert>}
      <Card><CardContent><Stack direction={{ xs: 'column', md: 'row' }} spacing={2} alignItems={{ md: 'center' }}>
        <TextField label="Tenant ID" value={scopeInput.tenant_id} onChange={(e) => setScopeInput({ ...scopeInput, tenant_id: e.target.value })} size="small" />
        <TextField label="Project ID" value={scopeInput.project_id} onChange={(e) => setScopeInput({ ...scopeInput, project_id: e.target.value })} size="small" />
        <Button onClick={applyScope}>应用作用域</Button>
        <TextField label="搜索当前页" value={query} onChange={(e) => setQuery(e.target.value)} size="small" sx={{ minWidth: 260 }} />
        <TextField select label="状态" value={status} onChange={(e) => { invalidateContext(); setPage(1); setStatus(e.target.value); }} size="small" SelectProps={{ native: true }}><option value="active">启用</option><option value="archived">已归档</option><option value="">全部</option></TextField>
        <Box sx={{ flex: 1 }} />{totalPages > 1 && <Stack direction="row" spacing={1} alignItems="center"><Button size="small" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</Button><Typography variant="caption">{page}/{totalPages}</Typography><Button size="small" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>下一页</Button></Stack>}<AdminRefreshButton onClick={() => void load(status, parentScope ?? undefined)} disabled={!parentScope} /><Button variant="contained" onClick={openCreate} disabled={!parentScope}>创建知识库</Button>
      </Stack></CardContent></Card>
      {!parentScope ? <Alert severity="info">请选择管理父作用域，输入值不代表授权。</Alert> : loading ? <Typography color="text.secondary">加载知识库目录...</Typography> : filtered.length === 0 ? <Alert severity="info">暂无匹配知识库</Alert> : filtered.map((item) => <Card key={item.id}><CardContent><Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ md: 'center' }}>
        <Box sx={{ flex: 1 }}><Typography variant="h6">{item.name}</Typography><Typography variant="body2" color="text.secondary">{item.description || '暂无描述'} · {item.tenant_id}/{item.project_id}</Typography><Typography variant="caption" color="text.secondary">更新时间：{item.updated_at ?? '暂无'}</Typography></Box>
        <Chip label={statusLabel(item.status)} color={item.status === 'active' ? 'success' : 'default'} /><Button onClick={() => navigate(`/admin/knowledge-bases/${item.id}?${new URLSearchParams(parentScope ?? {}).toString()}`)}>详情</Button><Button onClick={() => openEdit(item)} disabled={item.status !== 'active'} title={item.status === 'archived' ? '请先恢复知识库后再编辑' : undefined}>编辑</Button>{item.status === 'active' && <AdminLoadingButton loading={archiving === item.id} label="归档" loadingLabel="归档中..." color="warning" onClick={() => void archive(item)} />}{item.status === 'archived' && <AdminLoadingButton loading={archiving === item.id} label="恢复" loadingLabel="恢复中..." onClick={() => void restore(item)} />}
      </Stack></CardContent></Card>)}
    </Stack>
    <Dialog open={formMode !== 'closed'} onClose={() => { if (!saving) { setEditing(null); setForm(emptyForm); setFormMode('closed'); } }} fullWidth maxWidth="sm"><DialogTitle>{editing ? '编辑知识库' : '创建知识库'}</DialogTitle><DialogContent><Stack spacing={2} sx={{ pt: 1 }}>{error && <Alert severity="error">{error}</Alert>}<Typography variant="caption">父作用域：{editing ? `${editing.tenant_id}/${editing.project_id}` : `${formScope?.tenant_id ?? ''}/${formScope?.project_id ?? ''}`}</Typography><TextField required label="名称" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus /><TextField label="Slug" value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} /><TextField label="描述" multiline minRows={3} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Stack></DialogContent><DialogActions><Button onClick={() => { setEditing(null); setForm(emptyForm); setFormMode('closed'); }} disabled={saving}>取消</Button><AdminLoadingButton loading={saving} label="保存" loadingLabel="保存中..." onClick={() => void save()} disabled={!form.name.trim()} /></DialogActions></Dialog>
  </AdminLayout>;
}
