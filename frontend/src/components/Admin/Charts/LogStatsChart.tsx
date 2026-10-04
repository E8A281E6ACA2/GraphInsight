/**
 * 日志统计图表组件
 * 显示成功/失败日志的分布
 */
import React from 'react';
import {
  PieChart,
  Pie,
  Cell,
  ResponsiveContainer,
  Legend,
  Tooltip,
} from 'recharts';
import { Card, CardContent, Typography, Box } from '@mui/material';
import { PieChart as PieChartIcon } from '@mui/icons-material';

interface LogStatsChartProps {
  successCount: number;
  failedCount: number;
  title?: string;
}

const COLORS = {
  success: '#4caf50',
  failed: '#f44336',
};

const LogStatsChart: React.FC<LogStatsChartProps> = ({ 
  successCount, 
  failedCount,
  title = '日志统计分布'
}) => {
  const data = [
    { name: '成功', value: successCount, color: COLORS.success },
    { name: '失败', value: failedCount, color: COLORS.failed },
  ];

  const total = successCount + failedCount;
  const successRate = total > 0 ? ((successCount / total) * 100).toFixed(1) : '0.0';

  return (
    <Card>
      <CardContent>
        <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
          <PieChartIcon sx={{ mr: 1 }} />
          <Typography variant="h6">{title}</Typography>
        </Box>
        <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
          <ResponsiveContainer width="100%" height={250}>
            <PieChart>
              <Pie
                data={data}
                cx="50%"
                cy="50%"
                labelLine={false}
                label={({ name, percent }) => `${name}: ${((percent ?? 0) * 100).toFixed(0)}%`}
                outerRadius={80}
                fill="#8884d8"
                dataKey="value"
              >
                {data.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.color} />
                ))}
              </Pie>
              <Tooltip />
              <Legend />
            </PieChart>
          </ResponsiveContainer>
          <Box sx={{ mt: 2, textAlign: 'center' }}>
            <Typography variant="body2" color="text.secondary">
              总计: {total.toLocaleString()} 条日志
            </Typography>
            <Typography variant="body2" color="text.secondary">
              成功率: {successRate}%
            </Typography>
          </Box>
        </Box>
      </CardContent>
    </Card>
  );
};

export default LogStatsChart;
