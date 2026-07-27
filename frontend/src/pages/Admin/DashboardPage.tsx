/**
 * 管理系统仪表盘 v3.0
 * 使用标准化 API + 图表展示
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Box,
  Container,
  Card,
  CardContent,
  Typography,
  Chip,
  Alert,
  Button,
  LinearProgress,
  Stack,
} from '@mui/material';
import {
  Dashboard as DashboardIcon,
  Storage as StorageIcon,
  Speed as SpeedIcon,
  Security as SecurityIcon,
  Computer as ComputerIcon,
  Memory as MemoryIcon,
  Folder as FolderIcon,
  WorkHistory as WorkHistoryIcon,
  ManageSearch as ManageSearchIcon,
  MonitorHeart as MonitorHeartIcon,
} from '@mui/icons-material';
import { useNavigate } from 'react-router-dom';
import { monitorApi, logApi } from '../../services/adminService';
import type { SystemStats, HealthStatus, LogStats } from '../../types/admin';
import SystemResourceChart from '../../components/Admin/Charts/SystemResourceChart';
import LogStatsChart from '../../components/Admin/Charts/LogStatsChart';
import { useSystemMetrics } from '../../hooks/useSystemMetrics';
import { usePageVisible } from '../../hooks/usePageVisible';
import AdminLayout from '../../components/Admin/AdminLayout';
import { LoadingState } from '../../components/Loading/AppleSpinner';
import { getErrorMessage } from '../../utils/errorMessage';

const DashboardPage: React.FC = () => {
  const navigate = useNavigate();
  const pageVisible = usePageVisible();
  const [systemStats, setSystemStats] = useState<SystemStats | null>(null);
  const [healthStatus, setHealthStatus] = useState<HealthStatus | null>(null);
  const [logStats, setLogStats] = useState<LogStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  
  // 使用历史数据管理 Hook
  const { metricsHistory, addDataPoint } = useSystemMetrics({
    maxDataPoints: 20,
    updateInterval: 30000,
  });

  const fetchStats = useCallback(async (showSpinner = true) => {
    try {
      if (showSpinner) {
        setLoading(true);
      }
      setError('');

      const [systemData, healthData, logData] = await Promise.allSettled([
        monitorApi.getStats(),
        monitorApi.getHealth(),
        logApi.getStats(),
      ]);
      const failures: string[] = [];
      let hasSuccess = false;

      if (systemData.status === 'fulfilled') {
        setSystemStats(systemData.value);
        addDataPoint(systemData.value);
        hasSuccess = true;
      } else {
        failures.push(`系统指标：${getErrorMessage(systemData.reason, '请求失败')}`);
      }

      if (healthData.status === 'fulfilled') {
        setHealthStatus(healthData.value);
        hasSuccess = true;
      } else {
        failures.push(`健康状态：${getErrorMessage(healthData.reason, '请求失败')}`);
      }

      if (logData.status === 'fulfilled') {
        setLogStats(logData.value);
        hasSuccess = true;
      } else {
        failures.push(`日志统计：${getErrorMessage(logData.reason, '请求失败')}`);
      }

      if (failures.length > 0 && !hasSuccess) {
        setError(failures.join('；'));
      }
    } catch (err: unknown) {
      console.error('Dashboard load failed:', err);
    } finally {
      if (showSpinner) {
        setLoading(false);
      }
    }
  }, [addDataPoint]);

  useEffect(() => {
    void fetchStats();
    if (!pageVisible) {
      return undefined;
    }
    const interval = window.setInterval(() => {
      void fetchStats(false);
    }, 30000);
    return () => window.clearInterval(interval);
  }, [fetchStats, pageVisible]);

  const getStatusColor = (status: string): 'success' | 'error' | 'warning' | 'default' => {
    switch (status.toLowerCase()) {
      case 'connected':
      case 'healthy':
        return 'success';
      case 'disconnected':
      case 'unhealthy':
        return 'error';
      case 'degraded':
        return 'warning';
      default:
        return 'default';
    }
  };

  const formatUptime = (seconds: number) => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${hours}小时 ${minutes}分钟`;
  };

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const operationEntrypoints = [
    {
      title: '治理知识库',
      description: '查看文档、回收站和高风险清理结果',
      icon: <FolderIcon />,
      path: '/admin/knowledge-base',
    },
    {
      title: '跟踪任务',
      description: '处理建图、清库、重建索引和失败重试',
      icon: <WorkHistoryIcon />,
      path: '/admin/jobs',
    },
    {
      title: '诊断问答',
      description: '检查引用、检索、模型响应和成本',
      icon: <ManageSearchIcon />,
      path: '/admin/qa-traces',
    },
    {
      title: '查看监控',
      description: '定位健康、SLO、告警和服务状态',
      icon: <MonitorHeartIcon />,
      path: '/admin/monitor',
    },
  ];

  return (
    <AdminLayout title="运营总览" subtitle="知识库、问答、任务与系统状态的控制台入口">
      <Container maxWidth="lg" sx={{ pb: 4 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 3 }}>
            {error}
          </Alert>
        )}

        {loading && !systemStats && !healthStatus ? (
          <LoadingState label="正在加载数据" minHeight={360} />
        ) : null}
        
        {/* 始终显示内容区域,即使在加载中 */}
        <Stack spacing={3} sx={{ display: (loading && !systemStats && !healthStatus) ? 'none' : 'flex' }}>
            <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(4, 1fr)' }, gap: 2 }}>
              {operationEntrypoints.map((item) => (
                <Card key={item.path} sx={{ height: '100%' }}>
                  <CardContent sx={{ height: '100%', display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                    <Box sx={{ color: 'primary.main', display: 'flex' }}>{item.icon}</Box>
                    <Box sx={{ flex: 1 }}>
                      <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                        {item.title}
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        {item.description}
                      </Typography>
                    </Box>
                    <Button variant="outlined" size="small" onClick={() => navigate(item.path)}>
                      打开
                    </Button>
                  </CardContent>
                </Card>
              ))}
            </Box>

            {/* 系统健康状态 */}
            <Card>
              <CardContent>
                <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                  <DashboardIcon sx={{ mr: 1 }} />
                  <Typography variant="h6">系统健康状态</Typography>
                </Box>
                {healthStatus ? (
                  <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                    <Chip
                      label={`整体: ${healthStatus.status === 'healthy' ? '健康' : healthStatus.status === 'degraded' ? '降级' : '异常'}`}
                      color={getStatusColor(healthStatus.status)}
                    />
                    <Chip
                      label={`PostgreSQL: ${healthStatus.database.connected ? '已连接' : '断开'}`}
                      color={getStatusColor(healthStatus.database.connected ? 'connected' : 'disconnected')}
                      size="small"
                    />
                    <Chip
                      label={`Neo4j: ${healthStatus.neo4j.connected ? '已连接' : '断开'}`}
                      color={getStatusColor(healthStatus.neo4j.connected ? 'connected' : 'disconnected')}
                      size="small"
                    />
                    {healthStatus.milvus && (
                      <Chip
                        label={`Milvus: ${healthStatus.milvus.connected ? '已连接' : '断开'}${healthStatus.milvus.enabled ? '' : '（检索禁用）'}`}
                        color={getStatusColor(healthStatus.milvus.connected ? 'connected' : 'disconnected')}
                        size="small"
                      />
                    )}
                    {healthStatus.ai_service && (
                      <Chip
                        label={`AI服务: ${healthStatus.ai_service.connected ? '已配置' : '未配置'}`}
                        color={getStatusColor(healthStatus.ai_service.connected ? 'connected' : 'disconnected')}
                        size="small"
                      />
                    )}
                  </Box>
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    正在加载健康状态...
                  </Typography>
                )}
              </CardContent>
            </Card>

            {/* 系统资源 */}
            {systemStats && (
              <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, 1fr)' }, gap: 3 }}>
                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <ComputerIcon sx={{ mr: 1 }} />
                      <Typography variant="h6">CPU</Typography>
                    </Box>
                    <Typography variant="h4" color="primary">
                      {systemStats.cpu_percent.toFixed(1)}%
                    </Typography>
                    <LinearProgress
                      variant="determinate"
                      value={systemStats.cpu_percent}
                      sx={{ mt: 1 }}
                    />
                  </CardContent>
                </Card>

                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <MemoryIcon sx={{ mr: 1 }} />
                      <Typography variant="h6">内存</Typography>
                    </Box>
                    <Typography variant="h4" color="primary">
                      {systemStats.memory_percent.toFixed(1)}%
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      {formatBytes(systemStats.memory_used_mb * 1024 * 1024)} / {formatBytes(systemStats.memory_total_mb * 1024 * 1024)}
                    </Typography>
                    <LinearProgress
                      variant="determinate"
                      value={systemStats.memory_percent}
                      sx={{ mt: 1 }}
                    />
                  </CardContent>
                </Card>

                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <StorageIcon sx={{ mr: 1 }} />
                      <Typography variant="h6">磁盘</Typography>
                    </Box>
                    <Typography variant="h4" color="primary">
                      {systemStats.disk_percent.toFixed(1)}%
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      {systemStats.disk_used_gb.toFixed(1)}GB / {systemStats.disk_total_gb.toFixed(1)}GB
                    </Typography>
                    <LinearProgress
                      variant="determinate"
                      value={systemStats.disk_percent}
                      sx={{ mt: 1 }}
                    />
                  </CardContent>
                </Card>
              </Box>
            )}

            {/* 服务状态卡片 */}
            <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, 1fr)' }, gap: 3 }}>
              {/* Neo4j 数据库 */}
              {healthStatus?.neo4j && (
                <Card 
                  sx={{ cursor: 'pointer', '&:hover': { boxShadow: 4 } }}
                  onClick={() => navigate('/admin/config?tab=0')}
                >
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <StorageIcon sx={{ mr: 1, color: healthStatus.neo4j.connected ? 'success.main' : 'error.main' }} />
                      <Typography variant="h6">Neo4j 图数据库</Typography>
                    </Box>
                    <Chip 
                      label={healthStatus.neo4j.connected ? '已连接' : '断开'} 
                      color={healthStatus.neo4j.connected ? 'success' : 'error'}
                      size="small"
                      sx={{ mb: 1 }}
                    />
                    <Typography variant="body2" color="text.secondary" gutterBottom>
                      {healthStatus.neo4j.uri}
                    </Typography>
                    {healthStatus.neo4j.connected && (
                      <>
                        {healthStatus.neo4j.nodes_count !== undefined && healthStatus.neo4j.nodes_count !== null && (
                          <Typography variant="body2" color="text.secondary" gutterBottom>
                            节点: {healthStatus.neo4j.nodes_count.toLocaleString()}
                          </Typography>
                        )}
                        {healthStatus.neo4j.relationships_count !== undefined && healthStatus.neo4j.relationships_count !== null && (
                          <Typography variant="body2" color="text.secondary">
                            关系: {healthStatus.neo4j.relationships_count.toLocaleString()}
                          </Typography>
                        )}
                      </>
                    )}
                    {!healthStatus.neo4j.connected && healthStatus.neo4j.error && (
                      <Typography variant="caption" color="error">
                        {healthStatus.neo4j.error}
                      </Typography>
                    )}
                    <Typography variant="caption" color="primary" sx={{ display: 'block', mt: 1 }}>
                      点击配置 →
                    </Typography>
                  </CardContent>
                </Card>
              )}

              {/* AI 服务 */}
              {healthStatus?.ai_service && (
                <Card 
                  sx={{ cursor: 'pointer', '&:hover': { boxShadow: 4 } }}
                  onClick={() => navigate('/admin/config?tab=1')}
                >
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <SpeedIcon sx={{ mr: 1, color: healthStatus.ai_service.connected ? 'success.main' : 'warning.main' }} />
                      <Typography variant="h6">AI 服务</Typography>
                    </Box>
                    <Chip 
                      label={healthStatus.ai_service.connected ? '已配置' : '未配置'} 
                      color={healthStatus.ai_service.connected ? 'success' : 'warning'}
                      size="small"
                      sx={{ mb: 1 }}
                    />
                    <Typography variant="body2" color="text.secondary" gutterBottom>
                      服务: {healthStatus.ai_service.service_name}
                    </Typography>
                    {healthStatus.ai_service.model && (
                      <Typography variant="body2" color="text.secondary" gutterBottom>
                        模型: {healthStatus.ai_service.model}
                      </Typography>
                    )}
                    {!healthStatus.ai_service.connected && healthStatus.ai_service.error && (
                      <Typography variant="caption" color="warning.main">
                        {healthStatus.ai_service.error}
                      </Typography>
                    )}
                    <Typography variant="caption" color="primary" sx={{ display: 'block', mt: 1 }}>
                      点击配置 →
                    </Typography>
                  </CardContent>
                </Card>
              )}

              {/* 日志统计 */}
              {logStats && (
                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <SecurityIcon sx={{ mr: 1 }} />
                      <Typography variant="h6">操作日志</Typography>
                    </Box>
                    <Typography variant="h4" color="primary" gutterBottom>
                      {logStats.total_logs?.toLocaleString() ?? 0}
                    </Typography>
                    <Typography variant="body2" color="text.secondary" gutterBottom>
                      成功率: {((logStats.success_rate ?? 0) * 100).toFixed(1)}%
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      成功: {logStats.success_count ?? 0} / 失败: {logStats.failed_count ?? 0}
                    </Typography>
                  </CardContent>
                </Card>
              )}
            </Box>

            {/* 系统资源趋势图表 */}
            {metricsHistory.length > 0 && (
              <SystemResourceChart data={metricsHistory} />
            )}

            {/* 日志统计图表 */}
            {logStats && (
              <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(2, 1fr)' }, gap: 3 }}>
                <LogStatsChart 
                  successCount={logStats.success_count}
                  failedCount={logStats.failed_count}
                />
                
                {/* 系统信息卡片 */}
                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                      <SpeedIcon sx={{ mr: 1 }} />
                      <Typography variant="h6">系统信息</Typography>
                    </Box>
                    <Stack spacing={1}>
                      {systemStats && (
                        <Typography variant="body2" color="text.secondary">
                          运行时间: {formatUptime(systemStats.uptime_seconds)}
                        </Typography>
                      )}
                      <Typography variant="body2" color="text.secondary">
                        最后更新: {new Date().toLocaleString()}
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        数据点数: {metricsHistory.length} / 20
                      </Typography>
                    </Stack>
                  </CardContent>
                </Card>
              </Box>
            )}
          </Stack>
      </Container>
    </AdminLayout>
  );
};

export default DashboardPage;
