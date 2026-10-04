/**
 * 数据分析页面
 * 显示操作统计和趋势分析
 */
import React, { useEffect, useState } from 'react';
import {
  Box,
  Container,
  Card,
  CardContent,
  Typography,
  Alert,
  Stack,
} from '@mui/material';
import { 
  Analytics as AnalyticsIcon,
} from '@mui/icons-material';
import { logApi } from '../../services/adminService';
import OperationTypeChart from '../../components/Admin/Charts/OperationTypeChart';
import LogStatsChart from '../../components/Admin/Charts/LogStatsChart';
import type { LogStats } from '../../types/admin';
import AdminLayout from '../../components/Admin/AdminLayout';
import AdminRefreshButton from '../../components/Admin/AdminRefreshButton';
import { LoadingState } from '../../components/Loading/AppleSpinner';
import { getErrorMessage } from '../../utils/errorMessage';

type OperationStat = {
  operation: string;
  count: number;
};

const AnalyticsPage: React.FC = () => {
  const [logStats, setLogStats] = useState<LogStats | null>(null);
  const [operationStats, setOperationStats] = useState<OperationStat[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    setLoading(true);
    setError('');
    try {
      const stats = await logApi.getStats();
      setLogStats(stats);
      
      // 模拟操作类型统计（实际应该从后端获取）
      const mockOperationStats: OperationStat[] = [
        { operation: '查询', count: 150 },
        { operation: '配置更新', count: 45 },
        { operation: '登录', count: 89 },
        { operation: '导出', count: 23 },
        { operation: '测试连接', count: 67 },
      ];
      setOperationStats(mockOperationStats);
      
    } catch (err: unknown) {
      console.error('加载数据失败:', err);
      setError(getErrorMessage(err, '加载数据失败'));
    } finally {
      setLoading(false);
    }
  };

  const actionBar = (
    <AdminRefreshButton onClick={loadData} loading={loading} />
  );

  return (
    <AdminLayout title="数据分析" subtitle="行为趋势与风险洞察" actions={actionBar}>
      <Container maxWidth="lg" sx={{ px: 0 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 3 }}>
            {error}
          </Alert>
        )}

        {loading && !logStats ? (
          <LoadingState label="正在加载分析数据" minHeight={360} />
        ) : (
          <Stack spacing={3}>
            {/* 页面标题 */}
            <Card>
              <CardContent>
                <Box sx={{ display: 'flex', alignItems: 'center' }}>
                  <AnalyticsIcon sx={{ mr: 1, fontSize: 32 }} />
                  <Box>
                    <Typography variant="h5">系统数据分析</Typography>
                    <Typography variant="body2" color="text.secondary">
                      查看系统操作统计和趋势分析
                    </Typography>
                  </Box>
                </Box>
              </CardContent>
            </Card>

            {/* 图表展示 */}
            <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(2, 1fr)' }, gap: 3 }}>
              {logStats && (
                <LogStatsChart 
                  successCount={logStats.success_count}
                  failedCount={logStats.failed_count}
                />
              )}
              
              {operationStats.length > 0 && (
                <OperationTypeChart data={operationStats} />
              )}
            </Box>

            {/* 统计摘要 */}
            {logStats && (
              <Card>
                <CardContent>
                  <Typography variant="h6" gutterBottom>
                    统计摘要
                  </Typography>
                  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, 1fr)' }, gap: 2 }}>
                    <Box>
                      <Typography variant="body2" color="text.secondary">
                        总操作数
                      </Typography>
                      <Typography variant="h4" color="primary">
                        {logStats.total_logs.toLocaleString()}
                      </Typography>
                    </Box>
                    <Box>
                      <Typography variant="body2" color="text.secondary">
                        成功率
                      </Typography>
                      <Typography variant="h4" color="success.main">
                        {(logStats.success_rate * 100).toFixed(1)}%
                      </Typography>
                    </Box>
                    <Box>
                      <Typography variant="body2" color="text.secondary">
                        失败次数
                      </Typography>
                      <Typography variant="h4" color="error.main">
                        {logStats.failed_count.toLocaleString()}
                      </Typography>
                    </Box>
                  </Box>
                </CardContent>
              </Card>
            )}
          </Stack>
        )}
      </Container>
    </AdminLayout>
  );
};

export default AnalyticsPage;
