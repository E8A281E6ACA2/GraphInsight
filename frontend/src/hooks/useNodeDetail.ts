import { useState, useEffect } from 'react';
import { getNodeDetail } from '../services/graphService';
import type { NodeDetailResponse } from '../types/api';
import { getErrorMessage } from '../utils/errorMessage';

export function useNodeDetail(nodeId: string | null) {
  const [nodeDetail, setNodeDetail] = useState<NodeDetailResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!nodeId) {
      setNodeDetail(null);
      setError(null);
      return;
    }

    const fetchNodeDetail = async () => {
      setIsLoading(true);
      setError(null);

      try {
        const detail = await getNodeDetail(nodeId);
        setNodeDetail(detail);
      } catch (err: unknown) {
        setError(getErrorMessage(err, '获取节点详情失败'));
        console.error('Failed to fetch node detail:', err);
      } finally {
        setIsLoading(false);
      }
    };

    fetchNodeDetail();
  }, [nodeId]);

  return { nodeDetail, isLoading, error };
}
