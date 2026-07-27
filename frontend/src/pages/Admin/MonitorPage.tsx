/**
 * 系统监控页面 v3.0
 * 使用标准化 API + 实时图表
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Box,
  Container,
  Card,
  CardContent,
  Typography,
  Chip,
  Button,
  Alert,
  LinearProgress,
  Stack,
} from '@mui/material';
import { 
  NotificationsActive,
  Computer,
  Memory,
  Storage,
  Speed,
} from '@mui/icons-material';
import { monitorApi } from '../../services/adminService';
import type { AlertCheckResult, HealthStatus, SloSnapshot, SystemStats, PerformanceMetricsData, QAQualityMetrics } from '../../types/admin';
import AdminRefreshButton from '../../components/Admin/AdminRefreshButton';
import SystemResourceChart from '../../components/Admin/Charts/SystemResourceChart';
import { useSystemMetrics } from '../../hooks/useSystemMetrics';
import { usePageVisible } from '../../hooks/usePageVisible';
import AdminLayout from '../../components/Admin/AdminLayout';
import { LoadingState } from '../../components/Loading/AppleSpinner';
import { getErrorMessage } from '../../utils/errorMessage';

const API_WINDOW_SECONDS = 900;
const JOB_WINDOW_MINUTES = 60;

const MonitorPage: React.FC = () => {
  const pageVisible = usePageVisible();
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [performance, setPerformance] = useState<PerformanceMetricsData | null>(null);
  const [qaQuality, setQAQuality] = useState<QAQualityMetrics | null>(null);
  const [sloSnapshot, setSloSnapshot] = useState<SloSnapshot | null>(null);
  const [alertResult, setAlertResult] = useState<AlertCheckResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [checkingAlerts, setCheckingAlerts] = useState(false);
  const [error, setError] = useState('');
  
  // 使用历史数据管理 Hook
  const { metricsHistory, addDataPoint } = useSystemMetrics({
    maxDataPoints: 30,
    updateInterval: 30000,
  });

  const loadData = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [healthData, statsData, perfData, qaData, sloData] = await Promise.allSettled([
        monitorApi.getHealth(),
        monitorApi.getStats(),
        monitorApi.getPerformance({ window_seconds: API_WINDOW_SECONDS }),
        monitorApi.getQAQuality({ window_seconds: API_WINDOW_SECONDS }),
        monitorApi.getSloSnapshot({
          api_window_seconds: API_WINDOW_SECONDS,
          job_window_minutes: JOB_WINDOW_MINUTES,
        }),
      ]);
      const failures: string[] = [];
      let hasSuccess = false;
      
      if (healthData.status === 'fulfilled') {
        setHealth(healthData.value);
        hasSuccess = true;
      } else {
        const reason = getErrorMessage(healthData.reason, '健康检查接口失败');
        failures.push(`健康检查：${reason}`);
      }
      
      if (statsData.status === 'fulfilled') {
        setStats(statsData.value);
        // 添加到历史数据
        addDataPoint(statsData.value);
        hasSuccess = true;
      } else {
        const reason = getErrorMessage(statsData.reason, '系统指标接口失败');
        failures.push(`系统指标：${reason}`);
      }

      if (perfData.status === 'fulfilled') {
        setPerformance(perfData.value);
        hasSuccess = true;
      } else {
        const reason = getErrorMessage(perfData.reason, '性能指标接口失败');
        failures.push(`性能指标：${reason}`);
      }

      if (qaData.status === 'fulfilled') {
        setQAQuality(qaData.value);
        hasSuccess = true;
      } else {
        const reason = getErrorMessage(qaData.reason, '问答质量接口失败');
        failures.push(`问答质量：${reason}`);
      }

      if (sloData.status === 'fulfilled') {
        setSloSnapshot(sloData.value);
        hasSuccess = true;
      } else {
        const reason = getErrorMessage(sloData.reason, 'SLO 接口失败');
        failures.push(`SLO：${reason}`);
      }

      if (failures.length > 0 && !hasSuccess) {
        setError(failures.join('；'));
      }
    } catch (err: unknown) {
      console.error('加载数据失败:', err);
      setError(getErrorMessage(err, '加载数据失败'));
    } finally {
      setLoading(false);
    }
  }, [addDataPoint]);

  useEffect(() => {
    loadData();
    // 每 30 秒自动刷新
    if (!pageVisible) {
      return undefined;
    }
    const interval = window.setInterval(loadData, 30000);
    return () => window.clearInterval(interval);
  }, [loadData, pageVisible]);

  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case 'healthy':
      case 'connected':
        return 'success';
      case 'unhealthy':
      case 'disconnected':
        return 'error';
      case 'degraded':
        return 'warning';
      default:
        return 'default';
    }
  };

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const formatUptime = (seconds: number) => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${hours}小时 ${minutes}分钟`;
  };

  const formatPercent = (value: number) => `${(value * 100).toFixed(2)}%`;

  const handleCheckAlerts = async () => {
    setCheckingAlerts(true);
    setError('');
    try {
      const data = await monitorApi.checkAlerts({
        send_webhook: true,
        api_window_seconds: API_WINDOW_SECONDS,
        job_window_minutes: JOB_WINDOW_MINUTES,
      });
      setAlertResult(data);
      setSloSnapshot(data.snapshot);
      setPerformance(data.snapshot.api);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '告警检查失败'));
    } finally {
      setCheckingAlerts(false);
    }
  };

  const actionBar = (
    <Stack direction="row" spacing={1}>
      <AdminRefreshButton onClick={loadData} loading={loading} />
      <Button
        variant="contained"
        startIcon={<NotificationsActive />}
        onClick={handleCheckAlerts}
        disabled={checkingAlerts}
      >
        检查告警
      </Button>
    </Stack>
  );

  return (
    <AdminLayout title="系统监控" subtitle="服务状态与资源指标实时看板" actions={actionBar}>
      <Container maxWidth="lg" sx={{ px: 0 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 3 }}>
            {error}
          </Alert>
        )}

        {loading && !health && !stats && (
          <LoadingState label="正在加载监控数据" minHeight={360} />
        )}

        <Stack spacing={3}>
          {!loading && !health && !stats && !error && (
            <Alert severity="info">
              暂无监控数据，请检查后端连接状态与权限配置。
            </Alert>
          )}

          {/* 系统健康状态 */}
          {health && (
            <Box>
              <Card>
                <CardContent>
                  <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                    <Typography variant="h6">
                      系统健康状态
                    </Typography>
                    <Chip
                      label={health.status}
                      color={getStatusColor(health.status)}
                    />
                  </Box>
                  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, 1fr)' }, gap: 2 }}>
                      <Card variant="outlined">
                        <CardContent>
                          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <Typography variant="h6">数据库</Typography>
                            <Chip
                              label={health.database.connected ? '已连接' : '断开'}
                              color={getStatusColor(health.database.connected ? 'connected' : 'disconnected')}
                              size="small"
                            />
                          </Box>
                          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                            {health.database.message}
                          </Typography>
                        </CardContent>
                      </Card>
                      <Card variant="outlined">
                        <CardContent>
                          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <Typography variant="h6">Neo4j</Typography>
                            <Chip
                              label={health.neo4j.connected ? '已连接' : '断开'}
                              color={getStatusColor(health.neo4j.connected ? 'connected' : 'disconnected')}
                              size="small"
                            />
                          </Box>
                          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                            {health.neo4j.message}
                          </Typography>
                          {health.neo4j.nodes_count !== undefined && (
                            <Typography variant="body2" color="text.secondary">
                              节点: {health.neo4j.nodes_count.toLocaleString()}
                            </Typography>
                          )}
                          {health.neo4j.relationships_count !== undefined && (
                            <Typography variant="body2" color="text.secondary">
                              关系: {health.neo4j.relationships_count.toLocaleString()}
                            </Typography>
                          )}
                        </CardContent>
                      </Card>
                      {health.milvus && (
                        <Card variant="outlined">
                          <CardContent>
                            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                              <Typography variant="h6">Milvus</Typography>
                              <Chip
                                label={health.milvus.connected ? '已连接' : '断开'}
                                color={getStatusColor(health.milvus.connected ? 'connected' : 'disconnected')}
                                size="small"
                              />
                            </Box>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                              {health.milvus.base_url || health.milvus.message}
                            </Typography>
                            <Typography variant="body2" color="text.secondary">
                              向量检索：{health.milvus.enabled ? '已启用' : '已禁用'}
                            </Typography>
                          </CardContent>
                        </Card>
                      )}
                      {health.python_backend && (
                        <Card variant="outlined">
                          <CardContent>
                            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                              <Typography variant="h6">内部能力服务</Typography>
                              <Chip
                                label={health.python_backend.connected ? '已连接' : '断开'}
                                color={getStatusColor(health.python_backend.connected ? 'connected' : 'disconnected')}
                                size="small"
                              />
                            </Box>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                              {health.python_backend.base_url || health.python_backend.message}
                            </Typography>
                          </CardContent>
                        </Card>
                      )}

                    {health.ai_service && (
                        <Card variant="outlined">
                          <CardContent>
                            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                              <Typography variant="h6">AI 服务</Typography>
                              <Chip
                                label={health.ai_service.connected ? '已连接' : '未连接'}
                                color={getStatusColor(health.ai_service.connected ? 'connected' : 'disconnected')}
                                size="small"
                              />
                            </Box>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                              {health.ai_service.service_name} · {health.ai_service.model || '未指定模型'}
                            </Typography>
                          </CardContent>
                        </Card>
                    )}
                  </Box>
                </CardContent>
              </Card>
            </Box>
          )}

          {/* 系统资源 */}
          {stats && (
            <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, 1fr)' }, gap: 3 }}>
                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <Computer sx={{ mr: 1 }} />
                      <Typography variant="h6">CPU</Typography>
                    </Box>
                    <Typography variant="h4" color="primary">
                      {stats.cpu_percent.toFixed(1)}%
                    </Typography>
                    <LinearProgress
                      variant="determinate"
                      value={stats.cpu_percent}
                      sx={{ mt: 1 }}
                    />
                  </CardContent>
                </Card>

                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <Memory sx={{ mr: 1 }} />
                      <Typography variant="h6">内存</Typography>
                    </Box>
                    <Typography variant="h4" color="primary">
                      {stats.memory_percent.toFixed(1)}%
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      {formatBytes(stats.memory_used_mb * 1024 * 1024)} / {formatBytes(stats.memory_total_mb * 1024 * 1024)}
                    </Typography>
                    <LinearProgress
                      variant="determinate"
                      value={stats.memory_percent}
                      sx={{ mt: 1 }}
                    />
                  </CardContent>
                </Card>

                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <Storage sx={{ mr: 1 }} />
                      <Typography variant="h6">磁盘</Typography>
                    </Box>
                    <Typography variant="h4" color="primary">
                      {stats.disk_percent.toFixed(1)}%
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      {stats.disk_used_gb.toFixed(1)}GB / {stats.disk_total_gb.toFixed(1)}GB
                    </Typography>
                    <LinearProgress
                      variant="determinate"
                      value={stats.disk_percent}
                      sx={{ mt: 1 }}
                    />
                  </CardContent>
                </Card>
            </Box>
          )}

          {/* 实时资源趋势图 */}
          {metricsHistory.length > 0 && (
            <SystemResourceChart 
              data={metricsHistory}
              title="实时资源使用率趋势"
            />
          )}

          {performance && (
            <Card>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 2 }}>
                  API 性能指标（最近 {Math.round(performance.window_seconds / 60)} 分钟）
                </Typography>
                <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(4, 1fr)' }, gap: 2 }}>
                  <Card variant="outlined">
                    <CardContent>
                      <Typography variant="body2" color="text.secondary">错误率</Typography>
                      <Typography variant="h6">{formatPercent(performance.error_rate)}</Typography>
                    </CardContent>
                  </Card>
                  <Card variant="outlined">
                    <CardContent>
                      <Typography variant="body2" color="text.secondary">P95 延迟</Typography>
                      <Typography variant="h6">{performance.p95_response_time_ms.toFixed(1)} ms</Typography>
                    </CardContent>
                  </Card>
                  <Card variant="outlined">
                    <CardContent>
                      <Typography variant="body2" color="text.secondary">RPS</Typography>
                      <Typography variant="h6">{performance.requests_per_second.toFixed(3)}</Typography>
                    </CardContent>
                  </Card>
                  <Card variant="outlined">
                    <CardContent>
                      <Typography variant="body2" color="text.secondary">请求数</Typography>
                      <Typography variant="h6">{performance.total_requests}</Typography>
                    </CardContent>
                  </Card>
                </Box>
                {performance.top_paths.length > 0 && (
                  <Box sx={{ mt: 2 }}>
                    <Typography variant="subtitle2" sx={{ mb: 1 }}>高频路径</Typography>
                    <Stack spacing={1}>
                      {performance.top_paths.slice(0, 5).map((item) => (
                        <Box key={item.path} sx={{ display: 'flex', justifyContent: 'space-between' }}>
                          <Typography variant="body2" noWrap sx={{ maxWidth: '70%' }}>{item.path}</Typography>
                          <Typography variant="body2" color="text.secondary">
                            {item.total} 次 / 错误率 {formatPercent(item.error_rate)}
                          </Typography>
                        </Box>
                      ))}
                    </Stack>
                  </Box>
                )}
              </CardContent>
            </Card>
          )}

          {qaQuality && (
            <Card>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 2 }}>
                  问答质量指标（最近 {Math.round(qaQuality.window_seconds / 60)} 分钟）
                </Typography>
                <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(4, 1fr)' }, gap: 2 }}>
                  <Card variant="outlined">
                    <CardContent>
                      <Typography variant="body2" color="text.secondary">成功率</Typography>
                      <Typography variant="h6">{formatPercent(qaQuality.success_rate)}</Typography>
                    </CardContent>
                  </Card>
                  <Card variant="outlined">
                    <CardContent>
                      <Typography variant="body2" color="text.secondary">引用率</Typography>
                      <Typography variant="h6">{formatPercent(qaQuality.citation_rate)}</Typography>
                    </CardContent>
                  </Card>
                  <Card variant="outlined">
                    <CardContent>
                      <Typography variant="body2" color="text.secondary">P95 延迟</Typography>
                      <Typography variant="h6">{qaQuality.p95_latency_ms.toFixed(1)} ms</Typography>
                    </CardContent>
                  </Card>
                  <Card variant="outlined">
                    <CardContent>
                      <Typography variant="body2" color="text.secondary">请求数</Typography>
                      <Typography variant="h6">{qaQuality.total_requests}</Typography>
                    </CardContent>
                  </Card>
                </Box>
                {qaQuality.by_type.length > 0 && (
                  <Box sx={{ mt: 2 }}>
                    <Typography variant="subtitle2" sx={{ mb: 1 }}>链路拆分</Typography>
                    <Stack spacing={1}>
                      {qaQuality.by_type.map((item) => (
                        <Box key={item.qa_type} sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
                          <Typography variant="body2">
                            {item.qa_type === 'deep_research' ? '深度调研' : '文档问答'}
                          </Typography>
                          <Typography variant="body2" color="text.secondary">
                            {item.total} 次 / 引用率 {formatPercent(item.citation_rate)} / P95 {item.p95_latency_ms.toFixed(1)} ms
                          </Typography>
                        </Box>
                      ))}
                    </Stack>
                  </Box>
                )}
              </CardContent>
            </Card>
          )}

          {sloSnapshot && (
            <Card>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 2 }}>
                  SLO 快照
                </Typography>
                <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
                  <Chip
                    color={sloSnapshot.jobs.success_rate >= 0.99 ? 'success' : 'warning'}
                    label={`任务成功率: ${formatPercent(sloSnapshot.jobs.success_rate)} (目标 >=99%)`}
                  />
                  <Chip
                    color={sloSnapshot.jobs.timeout_rate <= 0.1 ? 'success' : 'warning'}
                    label={`任务超时率: ${formatPercent(sloSnapshot.jobs.timeout_rate)} (目标 <=10%)`}
                  />
                  <Chip
                    color={sloSnapshot.api.error_rate <= 0.01 ? 'success' : 'warning'}
                    label={`API 错误率: ${formatPercent(sloSnapshot.api.error_rate)} (目标 <=1%)`}
                  />
                </Stack>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
                  任务窗口 {sloSnapshot.jobs.window_minutes} 分钟，统计总任务 {sloSnapshot.jobs.total_jobs}，P95 耗时 {sloSnapshot.jobs.p95_duration_ms.toFixed(1)} ms
                </Typography>
              </CardContent>
            </Card>
          )}

          {alertResult && (
            <Card>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 2 }}>
                  最近一次告警检查
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  告警数: {alertResult.alert_count} · Webhook: {alertResult.webhook_configured ? (alertResult.sent ? '已发送' : '未发送') : '未配置'}
                </Typography>
                {alertResult.delivery_error && (
                  <Alert severity="warning" sx={{ mt: 2 }}>
                    webhook 发送失败：{alertResult.delivery_error}
                  </Alert>
                )}
                {alertResult.alerts.length > 0 ? (
                  <Stack spacing={1} sx={{ mt: 2 }}>
                    {alertResult.alerts.map((item, index) => (
                      <Alert key={`${item.type}-${index}`} severity={item.severity}>
                        {item.message}
                      </Alert>
                    ))}
                  </Stack>
                ) : (
                  <Alert severity="success" sx={{ mt: 2 }}>
                    当前窗口内无告警。
                  </Alert>
                )}
              </CardContent>
            </Card>
          )}

          {stats && (
            <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <Speed sx={{ mr: 1 }} />
                      <Typography variant="h6">系统信息</Typography>
                    </Box>
                    <Typography variant="body2" color="text.secondary">
                      运行时间: {formatUptime(stats.uptime_seconds)}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      最后更新: {new Date().toLocaleString()}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      监控数据点: {metricsHistory.length} / 30
                    </Typography>
                    {performance && (
                      <Typography variant="body2" color="text.secondary">
                        API P95: {performance.p95_response_time_ms.toFixed(1)}ms / 错误率: {formatPercent(performance.error_rate)}
                      </Typography>
                    )}
                  </CardContent>
                </Card>
          )}
        </Stack>
      </Container>
    </AdminLayout>
  );
};

export default MonitorPage;
