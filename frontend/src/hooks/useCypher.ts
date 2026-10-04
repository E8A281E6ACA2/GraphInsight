import { useState } from 'react';
import { useGraphStore } from '../store/graphStore';
import { executeQuery, GraphServiceError } from '../services/graphService';

export function useCypher() {
  const [isExecuting, setIsExecuting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setGraphData = useGraphStore((state) => state.setGraphData);
  const setLastQueryStats = useGraphStore((state) => state.setLastQueryStats);
  const addQueryToHistory = useGraphStore((state) => state.addQueryToHistory);
  const setHighlightAll = useGraphStore((state) => state.setHighlightAll);
  const setSelectedCitation = useGraphStore((state) => state.setSelectedCitation);

  const execute = async (cypher: string) => {
    if (!cypher.trim()) {
      setError('请输入 Cypher 查询');
      return null;
    }

    setIsExecuting(true);
    setError(null);

    try {
      console.log('useCypher - Executing query:', cypher);
      const result = await executeQuery(cypher);
      console.log('useCypher - Query result:', result);
      console.log('useCypher - Nodes:', result.nodes.length, 'Edges:', result.edges.length);

      // 更新图数据
      setGraphData(result);
      console.log('💾 useCypher - Graph data updated in store');
      // 查询结果本身就是当前图谱，不应再作为全量搜索命中高亮。
      // 3D 模式下全量高亮会打开所有节点和边标签，导致画面不可读。
      setHighlightAll(false);
      setSelectedCitation(null);

      // 更新统计信息
      if (result.stats) {
        setLastQueryStats(result.stats);
      }

      // 添加到查询历史
      addQueryToHistory(cypher, result.nodes.length + result.edges.length);

      return result;
    } catch (err) {
      if (err instanceof GraphServiceError) {
        setError(`${err.code}: ${err.message}`);
      } else {
        setError('执行查询时发生未知错误');
      }
      console.error('Query execution error:', err);
      return null;
    } finally {
      setIsExecuting(false);
    }
  };

  const clearError = () => setError(null);

  return {
    execute,
    isExecuting,
    error,
    clearError,
  };
}
