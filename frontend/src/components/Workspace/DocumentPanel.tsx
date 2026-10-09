import { Box, Button, Chip, Divider, Typography } from '@mui/material';
import {
  Description as DescriptionIcon,
  LocalLibrary as LibraryIcon,
  DeleteOutline as DeleteOutlineIcon,
  AutoGraph as AutoGraphIcon,
  ManageSearch as ManageSearchIcon,
  TravelExplore as TravelExploreIcon,
} from '@mui/icons-material';
import { useGraphStore } from '../../store/graphStore';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link as RouterLink } from 'react-router-dom';
import {
  listDocuments,
  deleteDocument,
  type DocumentItem,
  type DocumentVerificationSnapshot,
} from '../../services/documents';
import { getErrorMessage } from '../../utils/errorMessage';
import { captureWorkspaceContext, isWorkspaceContextCurrent, type WorkspaceContext } from '../../services/kbScope';
import { AppleSpinner } from '../Loading/AppleSpinner';
import LoadingButton from '../Loading/LoadingButton';

function buildGraphSummary(graph?: { documents: number; chunks: number; relations: number; orphan_entities: number }) {
  if (!graph) return '';
  return `图谱清理：文档 ${graph.documents} · 片段 ${graph.chunks} · 关系 ${graph.relations}`;
}

function buildVerificationSummary(verification?: DocumentVerificationSnapshot) {
  if (!verification?.after) return '';
  const graph = verification.after.graph;
  if (!graph) {
    return `校验：文档剩余 ${verification.after.active_documents}`;
  }
  return `校验：文档剩余 ${verification.after.active_documents} · 图文档 ${graph.documents ?? 0} · 图关系 ${graph.relations ?? 0}`;
}

export function DocumentPanel() {
  const selectedCitation = useGraphStore((state) => state.selectedCitation);
  const setSelectedCitation = useGraphStore((state) => state.setSelectedCitation);
  const recentUploadedDocIds = useGraphStore((state) => state.recentUploadedDocIds);
  const documentRefreshKey = useGraphStore((state) => state.documentRefreshKey);
  const activeKbId = useGraphStore((state) => state.activeKbId);
  const citationRef = useRef<HTMLDivElement | null>(null);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [documentsLoaded, setDocumentsLoaded] = useState(false);
  const [uploadSummary, setUploadSummary] = useState<string | null>(null);

  useEffect(() => {
    if (selectedCitation && citationRef.current) {
      citationRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [selectedCitation]);

  const requestSequence = useRef(0);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; requestSequence.current += 1; };
  }, []);

  const refreshDocuments = useCallback(async (context: WorkspaceContext) => {
    if (!mounted.current || !isWorkspaceContextCurrent(context)) return;
    const sequence = ++requestSequence.current;
    const valid = () => mounted.current && sequence === requestSequence.current && isWorkspaceContextCurrent(context);
    setLoading(true);
    setError(null);
    try {
      const items = await listDocuments(context.kbId);
      if (!valid()) return;
      setDocuments(items);
      setDocumentsLoaded(true);
    } catch (err) {
      if (!valid()) return;
      setDocuments([]);
      setDocumentsLoaded(false);
      setError(getErrorMessage(err, '文档列表加载失败'));
    } finally {
      if (valid()) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setDocuments([]);
    setDocumentsLoaded(false);
    setError(null);
    setUploadSummary(null);
    setDeletingId(null);
    setLoading(false);
    requestSequence.current += 1;
    if (activeKbId) void refreshDocuments(captureWorkspaceContext());
  }, [activeKbId, documentRefreshKey, refreshDocuments]);

  const handleDeleteOne = useCallback(
    async (item: DocumentItem) => {
      if (!useGraphStore.getState().activeKbId) return;
      const context = captureWorkspaceContext();
      const valid = () => mounted.current && isWorkspaceContextCurrent(context);
      setDeletingId(item.id);
      setError(null);
      try {
        const preview = await deleteDocument(item.id, {
          purgeGraph: true,
          softDelete: true,
          dryRun: true,
          verifyAfter: false,
        }, context.kbId);
        if (!valid()) return;
        const previewText = preview.candidate_file?.exists
          ? `将软删除文档「${item.name}」并清理图谱。`
          : `文件不存在，将仅清理图谱数据。`;
        const graphText = buildGraphSummary(preview.graph);
        const confirmed = window.confirm([previewText, graphText].filter(Boolean).join('\n'));
        if (!confirmed || !valid()) return;

        const result = await deleteDocument(item.id, {
          purgeGraph: true,
          softDelete: true,
          dryRun: false,
          verifyAfter: true,
        }, context.kbId);
        if (!valid()) return;
        const graphSummary = buildGraphSummary(result?.graph);
        const verificationSummary = buildVerificationSummary(result?.verification);
        setUploadSummary(
          [result?.file_action === 'soft_deleted' ? `已移入回收站 ${item.name}` : `已删除 ${item.name}`, graphSummary, verificationSummary]
            .filter(Boolean)
            .join('。')
        );
        if (selectedCitation?.title === item.name) {
          setSelectedCitation(null);
        }
        await refreshDocuments(context);
      } catch (err) {
        if (valid()) setError(getErrorMessage(err, '文档删除失败'));
      } finally {
        if (valid()) setDeletingId(null);
      }
    },
    [refreshDocuments, selectedCitation?.title, setSelectedCitation]
  );

  return (
    <Box
      sx={(theme) => ({
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        minWidth: 0,
        bgcolor: theme.palette.background.default,
        backgroundImage:
          theme.palette.mode === 'dark'
            ? 'radial-gradient(circle at top left, rgba(59, 130, 246, 0.08), transparent 55%), radial-gradient(circle at 80% 20%, rgba(16, 185, 129, 0.08), transparent 50%)'
            : 'radial-gradient(circle at top left, rgba(14, 165, 233, 0.08), transparent 55%), radial-gradient(circle at 80% 20%, rgba(34, 197, 94, 0.08), transparent 50%)',
      })}
    >
      <Box sx={{ px: 3, pt: 2.5, pb: 1.5 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 2 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h6" fontWeight={700} sx={{ mb: 0.5 }}>
              引用证据
            </Typography>
          </Box>
          <Chip size="small" variant="outlined" label={`${documents.length} 个文档`} />
        </Box>
      </Box>

      <Divider />

      <Box sx={{ px: 3, py: 2, overflowY: 'auto', overflowX: 'hidden', flex: 1, minWidth: 0 }}>
        <Box
          sx={(theme) => ({
            mb: 2,
            p: 1.5,
            borderRadius: 2,
            border: `1px solid ${theme.palette.divider}`,
            bgcolor: theme.palette.mode === 'dark' ? 'rgba(15, 23, 42, 0.45)' : 'rgba(248, 250, 252, 0.8)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: 2,
          })}
        >
          <Box sx={{ minWidth: 0, flex: '1 1 320px' }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
              <Chip icon={<TravelExploreIcon />} size="small" color="primary" variant="outlined" label="全库问答" />
              <Chip
                icon={<AutoGraphIcon />}
                size="small"
                color={recentUploadedDocIds.length > 0 ? 'success' : 'default'}
                variant="outlined"
                label={recentUploadedDocIds.length > 0 ? `最近上传 ${recentUploadedDocIds.length}` : '最近上传 0'}
              />
            </Box>
            {uploadSummary && (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                {uploadSummary}
              </Typography>
            )}
            {error && (
              <Typography variant="caption" color="error" sx={{ display: 'block', mt: 0.5 }}>
                {error}
              </Typography>
            )}
          </Box>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <Button
              component={RouterLink}
              to="/admin/knowledge-base"
              variant="outlined"
              size="small"
              startIcon={<ManageSearchIcon fontSize="small" />}
            >
              后台治理
            </Button>
          </Box>
        </Box>

        {selectedCitation && (
          <Box
            ref={citationRef}
            sx={(theme) => ({
              mb: 2.5,
              p: 2,
              borderRadius: 2,
              border: `1px solid ${theme.palette.primary.main}`,
              bgcolor: theme.palette.mode === 'dark' ? 'rgba(59, 130, 246, 0.12)' : 'rgba(14, 165, 233, 0.08)',
              boxShadow: theme.shadows[1],
            })}
          >
            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
              引用片段
            </Typography>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              {selectedCitation.title}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75 }}>
              {selectedCitation.snippet}
            </Typography>
            {selectedCitation.location && (
              <Chip
                size="small"
                label={selectedCitation.location}
                sx={{ mt: 1 }}
                variant="outlined"
              />
            )}
          </Box>
        )}

        <Box sx={{ display: 'grid', gap: 1.5 }}>
          {loading && (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
              <AppleSpinner size={28} label="正在加载文档" />
            </Box>
          )}
          {!loading && documentsLoaded && documents.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              提问后可在这里查看引用片段。当前知识库还没有文档。
            </Typography>
          )}
          {!loading && documents.map((item) => {
            const isActive = selectedCitation?.title === item.name;
            const sizeKb = Math.max(1, Math.round(item.size / 1024));
            return (
            <Box
              key={item.id}
              sx={(theme) => ({
                p: 2,
                borderRadius: 2,
                border: `1px solid ${isActive ? theme.palette.primary.main : theme.palette.divider}`,
                bgcolor: isActive
                  ? theme.palette.mode === 'dark'
                    ? 'rgba(59, 130, 246, 0.12)'
                    : 'rgba(14, 165, 233, 0.08)'
                  : theme.palette.background.paper,
                display: 'flex',
                alignItems: 'center',
                gap: 1.5,
                minWidth: 0,
              })}
            >
              <Box
                sx={(theme) => ({
                  width: 42,
                  height: 42,
                  borderRadius: 1.5,
                  bgcolor: theme.palette.mode === 'dark' ? 'rgba(148, 163, 184, 0.18)' : 'rgba(148, 163, 184, 0.2)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                })}
              >
                <DescriptionIcon fontSize="small" />
              </Box>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography
                  variant="subtitle2"
                  sx={{
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={item.name}
                >
                  {item.name}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {item.ext.toUpperCase().replace('.', '')} · {sizeKb} KB
                </Typography>
              </Box>
              <Chip
                icon={<LibraryIcon />}
                label="已入库"
                size="small"
                color="success"
                variant="outlined"
              />
              <LoadingButton
                size="small"
                color="error"
                variant="text"
                startIcon={<DeleteOutlineIcon fontSize="small" />}
                loading={deletingId === item.id}
                disabled={deletingId === item.id}
                onClick={() => {
                  handleDeleteOne(item);
                }}
                label="删除"
                loadingLabel="删除中..."
              />
            </Box>
            );
          })}
        </Box>

        {!loading && documentsLoaded && documents.length > 0 && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="caption" color="text.secondary">
              批量清理和恢复在后台治理处理。
            </Typography>
          </Box>
        )}
      </Box>
    </Box>
  );
}
