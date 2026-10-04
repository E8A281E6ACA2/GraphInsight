/**
 * 系统资源使用率图表组件
 * 显示 CPU、内存、磁盘的历史趋势
 */
import React from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts';
import { Card, CardContent, Typography, Box } from '@mui/material';
import { TrendingUp } from '@mui/icons-material';

interface DataPoint {
  timestamp: string;
  cpu: number;
  memory: number;
  disk: number;
}

interface SystemResourceChartProps {
  data: DataPoint[];
  title?: string;
}

const SystemResourceChart: React.FC<SystemResourceChartProps> = ({ 
  data, 
  title = '系统资源使用率趋势' 
}) => {
  return (
    <Card sx={{ overflow: 'visible' }}>
      <CardContent sx={{ display: 'grid', gap: 2, overflow: 'visible' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', minHeight: 36 }}>
          <TrendingUp sx={{ mr: 1 }} />
          <Typography variant="h6">{title}</Typography>
        </Box>
        <Box sx={{ width: '100%', height: { xs: 320, md: 360 }, minHeight: 320, overflow: 'hidden' }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 16, right: 18, bottom: 18, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="timestamp"
                tick={{ fontSize: 12 }}
                angle={-45}
                textAnchor="end"
                height={64}
              />
              <YAxis
                label={{ value: '使用率 (%)', angle: -90, position: 'insideLeft' }}
                domain={[0, 100]}
              />
              <Tooltip
                formatter={(value: number) => `${value.toFixed(1)}%`}
                labelStyle={{ color: '#000' }}
              />
              <Legend />
              <Line
                type="monotone"
                dataKey="cpu"
                stroke="#8884d8"
                name="CPU"
                strokeWidth={2}
                dot={false}
              />
              <Line
                type="monotone"
                dataKey="memory"
                stroke="#82ca9d"
                name="内存"
                strokeWidth={2}
                dot={false}
              />
              <Line
                type="monotone"
                dataKey="disk"
                stroke="#ffc658"
                name="磁盘"
                strokeWidth={2}
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </Box>
      </CardContent>
    </Card>
  );
};

export default SystemResourceChart;
