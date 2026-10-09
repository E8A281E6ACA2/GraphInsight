import { useState, useEffect, useRef } from 'react';
import { getNodeDetail } from '../services/graphService';
import type { NodeDetailResponse } from '../types/api';
import { getErrorMessage } from '../utils/errorMessage';
import { captureWorkspaceContext, isWorkspaceContextCurrent } from '../services/kbScope';
import { useGraphStore } from '../store/graphStore';

export function useNodeDetail(nodeId: string | null) {
  const [nodeDetail, setNodeDetail] = useState<NodeDetailResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const requestRef = useRef(0);
  const activeKbId = useGraphStore((state) => state.activeKbId);
  const scopeVersion = useGraphStore((state) => state.scopeVersion);
  const nodeIdRef = useRef(nodeId);
  nodeIdRef.current = nodeId;
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    const requestId = ++requestRef.current;
    setNodeDetail(null);
    setError(null);
    setIsLoading(false);
    if (!nodeId || !activeKbId) return;
    const context = captureWorkspaceContext();
    const current = () => mountedRef.current && nodeIdRef.current === nodeId && requestRef.current === requestId && isWorkspaceContextCurrent(context);
    const fetchNodeDetail = async () => {
      setIsLoading(true); setError(null);
      try {
        const detail = await getNodeDetail(nodeId, context.kbId);
        if (current()) setNodeDetail(detail);
      } catch (err: unknown) {
        if (current()) setError(getErrorMessage(err, '获取节点详情失败'));
      } finally { if (current()) setIsLoading(false); }
    };
    void fetchNodeDetail();
  }, [nodeId, activeKbId, scopeVersion]);
  return { nodeDetail, isLoading, error };
}
