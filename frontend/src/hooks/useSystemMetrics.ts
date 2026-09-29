/**
 * 系统指标历史数据管理 Hook
 * 用于收集和存储系统资源使用率的历史数据
 */
import { useState, useCallback } from 'react';
import type { SystemStats } from '../types/admin';

interface MetricDataPoint {
  timestamp: string;
  cpu: number;
  memory: number;
  disk: number;
}

interface UseSystemMetricsOptions {
  maxDataPoints?: number;
  updateInterval?: number; // 保留用于未来扩展
}

export const useSystemMetrics = (options: UseSystemMetricsOptions = {}) => {
  const { maxDataPoints = 20 } = options;
  const [metricsHistory, setMetricsHistory] = useState<MetricDataPoint[]>([]);

  // 添加新的数据点
  const addDataPoint = useCallback((stats: SystemStats) => {
    const newPoint: MetricDataPoint = {
      timestamp: new Date().toLocaleTimeString('zh-CN', { 
        hour: '2-digit', 
        minute: '2-digit',
        second: '2-digit'
      }),
      cpu: stats.cpu_percent,
      memory: stats.memory_percent,
      disk: stats.disk_percent,
    };

    setMetricsHistory(prev => {
      const updated = [...prev, newPoint];
      // 保持最大数据点数量
      if (updated.length > maxDataPoints) {
        return updated.slice(updated.length - maxDataPoints);
      }
      return updated;
    });
  }, [maxDataPoints]);

  // 清空历史数据
  const clearHistory = useCallback(() => {
    setMetricsHistory([]);
  }, []);

  return {
    metricsHistory,
    addDataPoint,
    clearHistory,
  };
};
