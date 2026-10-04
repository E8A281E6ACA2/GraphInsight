/**
 * 操作日志页面 v2.0
 * 使用标准化 API
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Box,
  Container,
  Card,
  CardContent,
  Typography,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TablePagination,
  Chip,
  Alert,
  TextField,
  MenuItem,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
} from '@mui/material';
import { FilterList, Download, DeleteSweep } from '@mui/icons-material';
import { logApi } from '../../services/adminService';
import type { LogItem } from '../../types/admin';
import AdminLayout from '../../components/Admin/AdminLayout';
import AdminRefreshButton from '../../components/Admin/AdminRefreshButton';
import { LoadingState } from '../../components/Loading/AppleSpinner';
import { getErrorMessage } from '../../utils/errorMessage';

type LogStatusFilter = 'success' | 'failed' | '';

const LogsPage: React.FC = () => {
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(20);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [action, setAction] = useState<string>('');
  const [status, setStatus] = useState<LogStatusFilter>('');
  const [traceId, setTraceId] = useState('');
  const [cleanDialogOpen, setCleanDialogOpen] = useState(false);
  const [cleanDays, setCleanDays] = useState(90);
  const [cleanPreview, setCleanPreview] = useState<{ deleted_count: number; days?: number; dry_run?: boolean; cutoff_at?: string } | null>(null);
  const [cleaning, setCleaning] = useState(false);
  const [message, setMessage] = useState('');

  const loadLogs = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await logApi.getLogs({
        page: page + 1,
        page_size: rowsPerPage,
        action: action || undefined,
        status: status || undefined,
        trace_id: traceId.trim() || undefined,
      });
      setLogs(Array.isArray(response.logs) ? response.logs : []);
      setTotal(Number(response.total || 0));
    } catch (err: unknown) {
      console.error('加载日志失败:', err);
      setError(getErrorMessage(err, '加载日志失败'));
    } finally {
      setLoading(false);
    }
  }, [action, page, rowsPerPage, status, traceId]);

  const handleExportCsv = async () => {
    try {
      setLoading(true);
      const blob = await logApi.exportLogsCsv({
        action: action || undefined,
        status: status || undefined,
        trace_id: traceId.trim() || undefined,
      });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      const stamp = new Date().toISOString().replace(/[:.]/g, '-');
      link.href = url;
      link.download = `admin_logs_${stamp}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '导出失败'));
    } finally {
      setLoading(false);
    }
  };

  const openCleanDialog = async () => {
    setCleanDialogOpen(true);
    setCleanPreview(null);
    setError('');
    setMessage('');
    try {
      setCleaning(true);
      const preview = await logApi.cleanup(cleanDays, true);
      setCleanPreview(preview);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '日志清理预览失败'));
    } finally {
      setCleaning(false);
    }
  };

  const refreshCleanPreview = async (days = cleanDays) => {
    setCleanPreview(null);
    setError('');
    try {
      setCleaning(true);
      const preview = await logApi.cleanup(days, true);
      setCleanPreview(preview);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '日志清理预览失败'));
    } finally {
      setCleaning(false);
    }
  };

  const handleCleanLogs = async () => {
    setCleaning(true);
    setError('');
    setMessage('');
    try {
      const result = await logApi.cleanup(cleanDays, false);
      setMessage(`已清理 ${result.deleted_count} 条 ${cleanDays} 天前的日志`);
      setCleanDialogOpen(false);
      setCleanPreview(null);
      await loadLogs();
    } catch (err: unknown) {
      setError(getErrorMessage(err, '日志清理失败'));
    } finally {
      setCleaning(false);
    }
  };

  useEffect(() => {
    loadLogs();
  }, [loadLogs]);

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleString('zh-CN');
  };

  const actionBar = (
    <AdminRefreshButton onClick={loadLogs} loading={loading} />
  );

  return (
    <AdminLayout title="日志审计" subtitle="关键操作与风险留痕" actions={actionBar}>
      <Container maxWidth="lg" sx={{ px: 0 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 3 }}>
            {error}
          </Alert>
        )}
        {message && (
          <Alert severity="success" sx={{ mb: 3 }} onClose={() => setMessage('')}>
            {message}
          </Alert>
        )}

        <Card sx={{ mb: 3 }}>
          <CardContent>
            <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', flexWrap: 'wrap' }}>
              <FilterList />
              <TextField
                select
                label="操作类型"
                value={action}
                onChange={(e) => {
                  setAction(e.target.value);
                  setPage(0);
                }}
                sx={{ minWidth: 150 }}
                size="small"
              >
                <MenuItem value="">全部</MenuItem>
                <MenuItem value="login">登录</MenuItem>
                <MenuItem value="logout">登出</MenuItem>
                <MenuItem value="config_update">配置更新</MenuItem>
                <MenuItem value="query">查询</MenuItem>
              </TextField>

              <TextField
                select
                label="状态"
                value={status}
                onChange={(e) => {
                  setStatus(e.target.value as LogStatusFilter);
                  setPage(0);
                }}
                sx={{ minWidth: 120 }}
                size="small"
              >
                <MenuItem value="">全部</MenuItem>
                <MenuItem value="success">成功</MenuItem>
                <MenuItem value="failed">失败</MenuItem>
              </TextField>
              <TextField
                label="Trace ID"
                value={traceId}
                onChange={(e) => {
                  setTraceId(e.target.value);
                  setPage(0);
                }}
                size="small"
                sx={{ minWidth: 260 }}
                placeholder="按 trace_id 精确查询"
              />

              <AdminRefreshButton onClick={loadLogs} loading={loading} />
              <Button
                variant="outlined"
                startIcon={<Download />}
                onClick={handleExportCsv}
                disabled={loading}
              >
                导出 CSV
              </Button>
              <Button
                variant="outlined"
                color="error"
                startIcon={<DeleteSweep />}
                onClick={() => void openCleanDialog()}
                disabled={loading || cleaning}
              >
                清理日志
              </Button>
            </Box>
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            {loading && logs.length === 0 ? (
              <LoadingState label="正在加载日志" minHeight={280} />
            ) : (
              <>
                <TableContainer>
                  <Table>
                    <TableHead>
                      <TableRow>
                        <TableCell>时间</TableCell>
                        <TableCell>用户</TableCell>
                        <TableCell>操作</TableCell>
                        <TableCell>资源</TableCell>
                        <TableCell>状态</TableCell>
                        <TableCell>详情</TableCell>
                        <TableCell>IP 地址</TableCell>
                        <TableCell>Trace ID</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {logs.length === 0 ? (
                        <TableRow>
                          <TableCell colSpan={8} align="center">
                            <Typography color="text.secondary">暂无日志</Typography>
                          </TableCell>
                        </TableRow>
                      ) : (
                        logs.map((log) => (
                          <TableRow key={log.id}>
                            <TableCell>{formatDate(log.created_at)}</TableCell>
                            <TableCell>{log.username || log.user_id || '-'}</TableCell>
                            <TableCell>
                              <Chip label={log.action} size="small" />
                            </TableCell>
                            <TableCell>{log.resource || '-'}</TableCell>
                            <TableCell>
                              <Chip
                                label={log.status}
                                color={log.status === 'success' ? 'success' : 'error'}
                                size="small"
                              />
                            </TableCell>
                            <TableCell>
                              <Typography variant="body2" noWrap sx={{ maxWidth: 200 }}>
                                {log.details || log.error_message || '-'}
                              </Typography>
                            </TableCell>
                            <TableCell>{log.ip_address || '-'}</TableCell>
                            <TableCell>
                              <Typography variant="body2" noWrap sx={{ maxWidth: 220 }}>
                                {log.trace_id || '-'}
                              </Typography>
                            </TableCell>
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
                  onPageChange={(_, newPage) => setPage(newPage)}
                  rowsPerPage={rowsPerPage}
                  rowsPerPageOptions={[10, 20, 25, 50, 100]}
                  onRowsPerPageChange={(e) => {
                    setRowsPerPage(parseInt(e.target.value, 10));
                    setPage(0);
                  }}
                  labelRowsPerPage="每页行数"
                  labelDisplayedRows={({ from, to, count }) => `${from}-${to} 共 ${count}`}
                />
              </>
            )}
          </CardContent>
        </Card>
      </Container>
      <Dialog open={cleanDialogOpen} onClose={() => !cleaning && setCleanDialogOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>清理旧日志</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="保留天数"
              type="number"
              value={cleanDays}
              onChange={(event) => {
                const next = Math.max(1, Math.min(365, Number(event.target.value || 90)));
                setCleanDays(next);
              }}
              onBlur={() => void refreshCleanPreview()}
              inputProps={{ min: 1, max: 365 }}
              helperText="只清理早于该天数的审计日志，清理操作本身会继续留痕。"
            />
            {cleanPreview ? (
              <Alert severity={cleanPreview.deleted_count > 0 ? 'warning' : 'info'}>
                将清理 {cleanPreview.deleted_count} 条日志
                {cleanPreview.cutoff_at ? `，截止时间 ${new Date(cleanPreview.cutoff_at).toLocaleString('zh-CN')}` : ''}
              </Alert>
            ) : (
              <Alert severity="info">正在预览可清理日志...</Alert>
            )}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCleanDialogOpen(false)} disabled={cleaning}>
            取消
          </Button>
          <Button onClick={() => void refreshCleanPreview()} disabled={cleaning}>
            重新预览
          </Button>
          <Button
            color="error"
            variant="contained"
            onClick={() => void handleCleanLogs()}
            disabled={cleaning || !cleanPreview || cleanPreview.deleted_count <= 0}
          >
            确认清理
          </Button>
        </DialogActions>
      </Dialog>
    </AdminLayout>
  );
};

export default LogsPage;
