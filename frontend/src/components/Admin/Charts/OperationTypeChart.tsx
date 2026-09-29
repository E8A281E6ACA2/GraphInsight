/**
 * 操作类型统计图表
 * 显示不同操作类型的分布
 */
import React from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  Cell,
} from 'recharts';
import { Card, CardContent, Typography, Box } from '@mui/material';
import { BarChart as BarChartIcon } from '@mui/icons-material';

interface OperationData {
  operation: string;
  count: number;
}

interface OperationTypeChartProps {
  data: OperationData[];
  title?: string;
}

const COLORS = ['#0088FE', '#00C49F', '#FFBB28', '#FF8042', '#8884D8', '#82CA9D'];

const OperationTypeChart: React.FC<OperationTypeChartProps> = ({ 
  data,
  title = '操作类型分布'
}) => {
  return (
    <Card>
      <CardContent>
        <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
          <BarChartIcon sx={{ mr: 1 }} />
          <Typography variant="h6">{title}</Typography>
        </Box>
        <ResponsiveContainer width="100%" height={300}>
          <BarChart data={data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis 
              dataKey="operation" 
              tick={{ fontSize: 12 }}
              angle={-45}
              textAnchor="end"
              height={80}
            />
            <YAxis label={{ value: '次数', angle: -90, position: 'insideLeft' }} />
            <Tooltip />
            <Legend />
            <Bar dataKey="count" name="操作次数" fill="#8884d8">
              {data.map((_entry, index) => (
                <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
};

export default OperationTypeChart;
