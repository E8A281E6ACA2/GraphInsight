/**
 * 任务中心页面
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Container,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Drawer,
  FormControlLabel,
  IconButton,
  LinearProgress,
  MenuItem,
  Switch,
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
import { Close, RestartAlt, StopCircle } from '@mui/icons-material';
import { useSearchParams } from 'react-router-dom';
import { jobsApi } from '../../services/adminService';
import type { JobItem, JobLogItem, JobStatus, JobType } from '../../types/admin';
import AdminLayout from '../../components/Admin/AdminLayout';
import AdminRefreshButton from '../../components/Admin/AdminRefreshButton';
import { LoadingState } from '../../components/Loading/AppleSpinner';
import { usePageVisible } from '../../hooks/usePageVisible';
import { getErrorMessage } from '../../utils/errorMessage';

type JobTypeFilter = JobType | '';
type JobStatusFilter = JobStatus | '';
const CLEAR_KB_CONFIRM_PHRASE = 'CLEAR';

const jobTypeOptions: Array<{ label: string; value: JobTypeFilter }> = [
  { label: '全部类型', value: '' },
  { label: '建图任务', value: 'build_graph' },
  { label: '清库任务', value: 'clear_kb' },
  { label: '重建索引', value: 'reindex' },
];

const jobStatusOptions: Array<{ label: string; value: JobStatusFilter }> = [
  { label: '全部状态', value: '' },
  { label: '待执行', value: 'pending' },
  { label: '执行中', value: 'running' },
  { label: '成功', value: 'succeeded' },
  { label: '失败', value: 'failed' },
  { label: '已取消', value: 'cancelled' },
];

const statusChipColor = (status: JobStatus): 'default' | 'primary' | 'success' | 'error' | 'warning' => {
  if (status === 'succeeded') return 'success';
  if (status === 'failed') return 'error';
  if (status === 'running') return 'warning';
  if (status === 'pending') return 'primary';
  return 'default';
};

const readNumericValue = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
};

const getJobProgress = (job: JobItem): number => {
  const direct = readNumericValue(job.progress);
  const resultProgress =
    job.result && typeof job.result === 'object'
      ? readNumericValue((job.result as Record<string, unknown>).progress)
      : null;
  const runtime =
    job.result && typeof job.result === 'object'
      ? (job.result as Record<string, unknown>).runtime
      : null;
  const runtimeProgress =
    runtime && typeof runtime === 'object'
      ? readNumericValue((runtime as Record<string, unknown>).progress)
      : null;
  const raw = direct ?? resultProgress ?? runtimeProgress;
  if (raw !== null) return Math.max(0, Math.min(100, raw <= 1 ? raw * 100 : raw));
  if (job.status === 'succeeded') return 100;
  if (job.status === 'failed' || job.status === 'cancelled') return 100;
  if (job.status === 'running') return 45;
  if (job.status === 'pending') return 8;
  return 0;
};

const getJobProgressLabel = (job: JobItem) => {
  if (job.status === 'pending') return '排队中';
  if (job.status === 'running') return '执行中';
  if (job.status === 'succeeded') return '已完成';
  if (job.status === 'failed') return '失败';
  if (job.status === 'cancelled') return '已取消';
  return job.status;
};

const formatDate = (value?: string) => {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleString('zh-CN');
};

const JobsPage: React.FC = () => {
  const pageVisible = usePageVisible();
  const [searchParams] = useSearchParams();
  const [jobs, setJobs] = useState<JobItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(20);
  const [jobType, setJobType] = useState<JobTypeFilter>('');
  const [jobStatus, setJobStatus] = useState<JobStatusFilter>('');
  const [tenantId, setTenantId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [kbId, setKbId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [operatingJobId, setOperatingJobId] = useState<number | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [selectedJob, setSelectedJob] = useState<JobItem | null>(null);
  const [jobLogs, setJobLogs] = useState<JobLogItem[]>([]);
  const [clearKbDialogOpen, setClearKbDialogOpen] = useState(false);
  const [clearKbConfirmText, setClearKbConfirmText] = useState('');
  const [clearKbPurgeGraph, setClearKbPurgeGraph] = useState(true);

  useEffect(() => {
    const nextType = searchParams.get('job_type') || '';
    const nextStatus = searchParams.get('status') || '';
    if (jobTypeOptions.some((item) => item.value === nextType)) {
      setJobType(nextType as JobTypeFilter);
      setPage(0);
    }
    if (jobStatusOptions.some((item) => item.value === nextStatus)) {
      setJobStatus(nextStatus as JobStatusFilter);
      setPage(0);
    }
  }, [searchParams]);

  const hasRunningJobs = useMemo(
    () => jobs.some((item) => item.status === 'pending' || item.status === 'running'),
    [jobs]
  );

  const loadJobs = useCallback(
    async (showSpinner = true) => {
      if (showSpinner) {
        setLoading(true);
      }
      setError('');
      try {
        const data = await jobsApi.getJobs({
          page: page + 1,
          page_size: rowsPerPage,
          job_type: jobType || undefined,
          status: jobStatus || undefined,
          tenant_id: tenantId.trim() || undefined,
          project_id: projectId.trim() || undefined,
          kb_id: kbId.trim() || undefined,
        });
        setJobs(Array.isArray(data.items) ? data.items : []);
        setTotal(Number(data.total || 0));
      } catch (err: unknown) {
        setError(getErrorMessage(err, '任务加载失败'));
      } finally {
        if (showSpinner) {
          setLoading(false);
        }
      }
    },
    [jobStatus, jobType, kbId, page, projectId, rowsPerPage, tenantId]
  );

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    if (!hasRunningJobs || !pageVisible) return undefined;
    const timer = window.setInterval(() => {
      void loadJobs(false);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [hasRunningJobs, loadJobs, pageVisible]);

  const handleCreateBuildGraph = async () => {
    try {
      setOperatingJobId(-1);
      setError('');
      await jobsApi.createBuildGraph({
        tenant_id: tenantId.trim() || undefined,
        project_id: projectId.trim() || undefined,
        kb_id: kbId.trim() || undefined,
        payload: { source: 'admin_jobs_page', force: false },
        max_retries: 3,
      });
      setMessage('建图任务已创建');
      await loadJobs(false);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '创建建图任务失败'));
    } finally {
      setOperatingJobId(null);
    }
  };

  const handleCreateClearKb = async () => {
    try {
      setOperatingJobId(-2);
      setError('');
      await jobsApi.createClearKb({
        tenant_id: tenantId.trim() || undefined,
        project_id: projectId.trim() || undefined,
        kb_id: kbId.trim() || undefined,
        payload: { purge_graph: clearKbPurgeGraph },
        max_retries: 1,
      });
      setMessage('清库任务已创建');
      setClearKbDialogOpen(false);
      setClearKbConfirmText('');
      await loadJobs(false);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '创建清库任务失败'));
    } finally {
      setOperatingJobId(null);
    }
  };

  const handleCreateReindex = async () => {
    try {
      setOperatingJobId(-3);
      setError('');
      await jobsApi.createReindex({
        tenant_id: tenantId.trim() || undefined,
        project_id: projectId.trim() || undefined,
        kb_id: kbId.trim() || undefined,
        payload: {},
        max_retries: 1,
      });
      setMessage('重建索引任务已创建');
      await loadJobs(false);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '创建重建索引任务失败'));
    } finally {
      setOperatingJobId(null);
    }
  };

  const handleRetry = async (jobId: number) => {
    try {
      setOperatingJobId(jobId);
      setError('');
      await jobsApi.retryJob(jobId);
      setMessage(`任务 #${jobId} 已提交重试`);
      await loadJobs(false);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '重试任务失败'));
    } finally {
      setOperatingJobId(null);
    }
  };

  const handleCancel = async (jobId: number) => {
    try {
      setOperatingJobId(jobId);
      setError('');
      await jobsApi.cancelJob(jobId);
      setMessage(`任务 #${jobId} 已取消`);
      await loadJobs(false);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '取消任务失败'));
    } finally {
      setOperatingJobId(null);
    }
  };

  const openDetail = async (job: JobItem) => {
    try {
      setError('');
      // M4-R1 审计：job detail/logs 必须携带 kb_id。优先用任务行归属，
      // 回退到当前 KB 过滤；两者都缺失时无法定位作用域，不发起请求。
      const scopedKbId = (job.kb_id || kbId).trim();
      if (!scopedKbId) {
        setError('缺少知识库归属，请先选择知识库后再查看任务详情');
        return;
      }
      const data = await jobsApi.getJobById(job.id, scopedKbId);
      const logs = await jobsApi.getJobLogs(job.id, scopedKbId, { page: 1, page_size: 100 });
      setSelectedJob(data);
      setJobLogs(logs.items || []);
      setDetailOpen(true);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '加载任务详情失败'));
    }
  };

  const actionBar = (
    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
      <Button
        variant="contained"
        onClick={handleCreateBuildGraph}
        disabled={loading || operatingJobId !== null}
      >
        新建建图任务
      </Button>
      <Button
        variant="outlined"
        color="warning"
        onClick={() => {
          setClearKbConfirmText('');
          setClearKbPurgeGraph(true);
          setClearKbDialogOpen(true);
        }}
        disabled={loading || operatingJobId !== null}
      >
        新建清库任务
      </Button>
      <Button
        variant="outlined"
        onClick={handleCreateReindex}
        disabled={loading || operatingJobId !== null}
      >
        新建重建索引
      </Button>
      <AdminRefreshButton onClick={() => void loadJobs()} loading={loading} />
    </Stack>
  );

  return (
    <AdminLayout title="任务中心" subtitle="异步任务提交、追踪与重试" actions={actionBar}>
      <Container maxWidth="lg" sx={{ px: 0 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        {message && (
          <Alert severity="success" sx={{ mb: 2 }} onClose={() => setMessage('')}>
            {message}
          </Alert>
        )}

        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
              <TextField
                select
                label="任务类型"
                value={jobType}
                onChange={(event) => {
                  setJobType(event.target.value as JobTypeFilter);
                  setPage(0);
                }}
                sx={{ minWidth: 180 }}
                size="small"
              >
                {jobTypeOptions.map((item) => (
                  <MenuItem key={item.value || 'all'} value={item.value}>
                    {item.label}
                  </MenuItem>
                ))}
              </TextField>

              <TextField
                select
                label="状态"
                value={jobStatus}
                onChange={(event) => {
                  setJobStatus(event.target.value as JobStatusFilter);
                  setPage(0);
                }}
                sx={{ minWidth: 160 }}
                size="small"
              >
                {jobStatusOptions.map((item) => (
                  <MenuItem key={item.value || 'all'} value={item.value}>
                    {item.label}
                  </MenuItem>
                ))}
              </TextField>

              <TextField
                label="租户 ID"
                value={tenantId}
                onChange={(event) => {
                  setTenantId(event.target.value);
                  setPage(0);
                }}
                size="small"
              />
              <TextField
                label="项目 ID"
                value={projectId}
                onChange={(event) => {
                  setProjectId(event.target.value);
                  setPage(0);
                }}
                size="small"
              />
              <TextField
                label="知识库 ID"
                value={kbId}
                onChange={(event) => {
                  setKbId(event.target.value);
                  setPage(0);
                }}
                size="small"
              />
            </Stack>
          </CardContent>
        </Card>

        <Alert severity="warning" sx={{ mb: 2 }}>
          当前“清库任务”为高风险全局操作：它会清理文档目录中的全部受支持文档，并可选择同时清空图谱。
          当前页的租户 / 项目 / 知识库筛选只用于查询任务列表，不会限制清库执行范围。
        </Alert>

        <Card>
          <CardContent>
            {loading && jobs.length === 0 ? (
              <LoadingState label="正在加载任务" minHeight={280} />
            ) : (
              <>
                <TableContainer>
                  <Table>
                    <TableHead>
                      <TableRow>
                        <TableCell>ID</TableCell>
                        <TableCell>类型</TableCell>
                        <TableCell>状态</TableCell>
                        <TableCell>进度</TableCell>
                        <TableCell>作用域</TableCell>
                        <TableCell>重试</TableCell>
                        <TableCell>创建时间</TableCell>
                        <TableCell>结束时间</TableCell>
                        <TableCell align="right">操作</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {jobs.length === 0 ? (
                        <TableRow>
                          <TableCell colSpan={9} align="center">
                            <Typography color="text.secondary">暂无任务</Typography>
                          </TableCell>
                        </TableRow>
                      ) : (
                        jobs.map((item) => {
                          const canRetry =
                            (item.status === 'failed' || item.status === 'cancelled') &&
                            item.retry_count < item.max_retries;
                          const canCancel = item.status === 'pending' || item.status === 'running';
                          const busy = operatingJobId === item.id;
                          const progress = getJobProgress(item);
                          return (
                            <TableRow key={item.id} hover>
                              <TableCell>#{item.id}</TableCell>
                              <TableCell>{item.job_type}</TableCell>
                              <TableCell>
                                <Chip label={item.status} color={statusChipColor(item.status)} size="small" />
                              </TableCell>
                              <TableCell sx={{ minWidth: 150 }}>
                                <Stack spacing={0.5}>
                                  <LinearProgress variant="determinate" value={progress} />
                                  <Typography variant="caption" color="text.secondary">
                                    {getJobProgressLabel(item)} · {Math.round(progress)}%
                                  </Typography>
                                </Stack>
                              </TableCell>
                              <TableCell>
                                <Typography variant="body2" noWrap sx={{ maxWidth: 220 }}>
                                  {item.tenant_id || '-'}/{item.project_id || '-'}/{item.kb_id || '-'}
                                </Typography>
                              </TableCell>
                              <TableCell>
                                {item.retry_count}/{item.max_retries}
                              </TableCell>
                              <TableCell>{formatDate(item.created_at)}</TableCell>
                              <TableCell>{formatDate(item.finished_at)}</TableCell>
                              <TableCell align="right">
                                <Stack direction="row" spacing={1} justifyContent="flex-end">
                                  <Button size="small" onClick={() => void openDetail(item)}>
                                    详情
                                  </Button>
                                  <Button
                                    size="small"
                                    startIcon={<RestartAlt />}
                                    disabled={!canRetry || busy || operatingJobId !== null}
                                    onClick={() => void handleRetry(item.id)}
                                  >
                                    重试
                                  </Button>
                                  <Button
                                    size="small"
                                    color="warning"
                                    startIcon={<StopCircle />}
                                    disabled={!canCancel || busy || operatingJobId !== null}
                                    onClick={() => void handleCancel(item.id)}
                                  >
                                    取消
                                  </Button>
                                </Stack>
                              </TableCell>
                            </TableRow>
                          );
                        })
                      )}
                    </TableBody>
                  </Table>
                </TableContainer>
                <TablePagination
                  component="div"
                  count={total}
                  page={page}
                  rowsPerPage={rowsPerPage}
                  onPageChange={(_, newPage) => setPage(newPage)}
                  onRowsPerPageChange={(event) => {
                    setRowsPerPage(parseInt(event.target.value, 10));
                    setPage(0);
                  }}
                  rowsPerPageOptions={[10, 20, 50]}
                  labelRowsPerPage="每页行数"
                  labelDisplayedRows={({ from, to, count }) => `${from}-${to} 共 ${count}`}
                />
              </>
            )}
          </CardContent>
        </Card>
      </Container>

      <Drawer
        anchor="right"
        open={detailOpen}
        onClose={() => {
          setDetailOpen(false);
          setSelectedJob(null);
          setJobLogs([]);
        }}
        PaperProps={{ sx: { width: { xs: '100%', md: 520 }, p: 2.5 } }}
      >
        <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2 }}>
          <Typography variant="h6">任务详情</Typography>
          <IconButton
            onClick={() => {
              setDetailOpen(false);
              setSelectedJob(null);
              setJobLogs([]);
            }}
          >
            <Close />
          </IconButton>
        </Stack>
        {!selectedJob ? (
          <Typography color="text.secondary">未选择任务</Typography>
        ) : (
          <Stack spacing={2}>
            <Typography variant="body2">任务 ID: #{selectedJob.id}</Typography>
            <Typography variant="body2">类型: {selectedJob.job_type}</Typography>
            <Typography variant="body2">状态: {selectedJob.status}</Typography>
            <Box>
              <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.5 }}>
                <Typography variant="body2">执行进度</Typography>
                <Typography variant="body2" color="text.secondary">
                  {getJobProgressLabel(selectedJob)} · {Math.round(getJobProgress(selectedJob))}%
                </Typography>
              </Stack>
              <LinearProgress variant="determinate" value={getJobProgress(selectedJob)} />
            </Box>
            <Typography variant="body2">Trace ID: {selectedJob.trace_id || '-'}</Typography>
            <Typography variant="body2">创建时间: {formatDate(selectedJob.created_at)}</Typography>
            <Typography variant="body2">开始时间: {formatDate(selectedJob.started_at)}</Typography>
            <Typography variant="body2">结束时间: {formatDate(selectedJob.finished_at)}</Typography>
            {selectedJob.error_message ? (
              <Alert severity="error">{selectedJob.error_message}</Alert>
            ) : null}
            <Box>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                Payload
              </Typography>
              <Box component="pre" sx={{ m: 0, p: 1.5, borderRadius: 1.5, bgcolor: 'rgba(10, 28, 44, 0.04)' }}>
                {JSON.stringify(selectedJob.payload || {}, null, 2)}
              </Box>
            </Box>
            <Box>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                Result
              </Typography>
              <Box component="pre" sx={{ m: 0, p: 1.5, borderRadius: 1.5, bgcolor: 'rgba(10, 28, 44, 0.04)' }}>
                {JSON.stringify(selectedJob.result || {}, null, 2)}
              </Box>
            </Box>
            <Box>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                Logs
              </Typography>
              {jobLogs.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  暂无任务日志
                </Typography>
              ) : (
                <Stack spacing={1}>
                  {jobLogs.map((item) => (
                    <Box
                      key={item.id}
                      sx={{
                        p: 1.25,
                        borderRadius: 1.5,
                        bgcolor: item.status === 'failed' ? 'rgba(183, 28, 28, 0.08)' : 'rgba(10, 28, 44, 0.04)',
                      }}
                    >
                      <Typography variant="caption" color="text.secondary">
                        #{item.id} · {item.action} · {new Date(item.created_at).toLocaleString('zh-CN')}
                      </Typography>
                      {item.error_message ? (
                        <Typography variant="body2" color="error.main">
                          {item.error_message}
                        </Typography>
                      ) : null}
                    </Box>
                  ))}
                </Stack>
              )}
            </Box>
          </Stack>
        )}
      </Drawer>

      <Dialog
        open={clearKbDialogOpen}
        onClose={() => {
          if (operatingJobId !== -2) {
            setClearKbDialogOpen(false);
          }
        }}
        fullWidth
        maxWidth="sm"
      >
        <DialogTitle>确认创建清库任务</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <Alert severity="warning">
              该任务会按全局范围删除文档文件。
              {clearKbPurgeGraph ? '当前还会同时清空 Neo4j 图谱数据。' : '当前不会清空图谱数据。'}
            </Alert>
            <Box>
              <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                当前筛选值
              </Typography>
              <Typography variant="body2" color="text.secondary">
                tenant_id={tenantId.trim() || '-'} / project_id={projectId.trim() || '-'} / kb_id={kbId.trim() || '-'}
              </Typography>
              <Typography variant="caption" color="warning.main">
                这些筛选值不会限制清库范围，仅用于列表过滤与记录上下文。
              </Typography>
            </Box>
            <FormControlLabel
              control={
                <Switch
                  checked={clearKbPurgeGraph}
                  onChange={(event) => setClearKbPurgeGraph(event.target.checked)}
                  color="warning"
                />
              }
              label="同时清空图谱数据（purge_graph）"
            />
            <TextField
              label={`请输入 ${CLEAR_KB_CONFIRM_PHRASE} 以确认`}
              value={clearKbConfirmText}
              onChange={(event) => setClearKbConfirmText(event.target.value.toUpperCase())}
              autoFocus
              fullWidth
              disabled={operatingJobId === -2}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button
            onClick={() => setClearKbDialogOpen(false)}
            disabled={operatingJobId === -2}
          >
            取消
          </Button>
          <Button
            color="warning"
            variant="contained"
            onClick={() => void handleCreateClearKb()}
            disabled={operatingJobId === -2 || clearKbConfirmText.trim() !== CLEAR_KB_CONFIRM_PHRASE}
          >
            确认创建清库任务
          </Button>
        </DialogActions>
      </Dialog>
    </AdminLayout>
  );
};

export default JobsPage;
