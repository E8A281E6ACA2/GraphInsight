import { useEffect, useRef, useState } from 'react';
import { useGraphStore } from '../store/graphStore';
import { executeQuery, GraphServiceError } from '../services/graphService';
import { captureWorkspaceContext, isWorkspaceContextCurrent } from '../services/kbScope';

export function useCypher() {
  const [isExecuting, setIsExecuting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const requestRef = useRef(0);
  const activeKbId = useGraphStore((state) => state.activeKbId);
  const setGraphData = useGraphStore((state) => state.setGraphData);
  const setLastQueryStats = useGraphStore((state) => state.setLastQueryStats);
  const addQueryToHistory = useGraphStore((state) => state.addQueryToHistory);
  const setHighlightAll = useGraphStore((state) => state.setHighlightAll);
  const setSelectedCitation = useGraphStore((state) => state.setSelectedCitation);

  const scopeVersion = useGraphStore((state) => state.scopeVersion);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  useEffect(() => {
    requestRef.current += 1;
    setError(null);
    setIsExecuting(false);
  }, [activeKbId, scopeVersion]);

  const execute = async (cypher: string) => {
    if (!mountedRef.current || !useGraphStore.getState().activeKbId) return null;
    if (!cypher.trim()) {
      setError('请输入 Cypher 查询');
      return null;
    }
    const requestId = ++requestRef.current;
    const context = captureWorkspaceContext();
    const current = () => mountedRef.current && requestRef.current === requestId && isWorkspaceContextCurrent(context);
    setIsExecuting(true);
    setError(null);
    try {
      const result = await executeQuery(cypher, undefined, context.kbId);
      if (!current()) return null;
      setGraphData(result);
      setHighlightAll(false);
      setSelectedCitation(null);
      if (result.stats) setLastQueryStats(result.stats);
      addQueryToHistory(cypher, result.nodes.length + result.edges.length);
      return result;
    } catch (err) {
      if (current()) setError(err instanceof GraphServiceError ? `${err.code}: ${err.message}` : '执行查询时发生未知错误');
      return null;
    } finally { if (current()) setIsExecuting(false); }
  };
  const clearError = () => setError(null);
  return { execute, isExecuting, error, clearError };
}
