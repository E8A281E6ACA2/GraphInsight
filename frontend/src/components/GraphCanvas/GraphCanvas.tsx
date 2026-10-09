import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { Box, Typography, Dialog, DialogTitle, DialogContent, IconButton } from '@mui/material';
import { Close as CloseIcon } from '@mui/icons-material';
import { useGraphStore, type GraphData } from '../../store/graphStore';
import { adaptGraphData } from '../../renderers/core/adapter';
import { createRenderer } from '../../renderers/canvas2d/renderer';
import type { RendererAPI, RendererActiveElement, RendererData } from '../../renderers/core/types';
import { GraphControls } from './GraphControls';
import { NodeTooltip } from './NodeTooltip';
import { Minimap } from './Minimap';
import { ContextMenu, type ContextMenuTarget } from './ContextMenu';
import { PerformanceWarningDialog } from './PerformanceWarningDialog';
import { NavigationPanel } from './NavigationPanel';
import { reportClientLog } from '../../services/clientLog';
import { expandNode } from '../../services/graphService';
import { captureWorkspaceContext, isWorkspaceContextCurrent } from '../../services/kbScope';

interface GraphCanvasProps {
  rendererRef?: React.RefObject<RendererAPI | null>;
  onGroupingUpdate?: () => void;
}

type TooltipNodeData = {
  id: string;
  label: string;
  type: string;
  properties: Record<string, unknown>;
};

export function GraphCanvas({ rendererRef: externalRendererRef, onGroupingUpdate }: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const threeContainerRef = useRef<HTMLDivElement>(null);
  const internalRendererRef = useRef<RendererAPI | null>(null);
  const rendererRef = externalRendererRef || internalRendererRef;
  const activeElementRef = useRef<RendererActiveElement | null>(null);
  const rendererDataRef = useRef<RendererData | null>(null);

  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });

  // 提示框状态
  const [tooltipVisible, setTooltipVisible] = useState(false);
  const [tooltipPosition, setTooltipPosition] = useState({ x: 0, y: 0 });
  const [tooltipData, setTooltipData] = useState<TooltipNodeData | null>(null);

  // 视频播放状态
  const [videoDialogOpen, setVideoDialogOpen] = useState(false);
  const [currentVideo, setCurrentVideo] = useState<{ url: string; title: string } | null>(null);

  // 右键菜单状态
  const [contextMenuPosition, setContextMenuPosition] = useState<{ top: number; left: number } | null>(null);
  const [contextMenuTarget, setContextMenuTarget] = useState<ContextMenuTarget>(null);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [viewMode, setViewMode] = useState<'2d' | '3d'>('2d');
  const [rendererError, setRendererError] = useState<string | null>(null);
  const SAFE_FIT_PADDING = 120;

  useEffect(() => {
    if (viewMode === '3d' && navigationOpen) {
      setNavigationOpen(false);
    }
  }, [navigationOpen, viewMode]);

  // 性能警告状态
  const [showPerformanceWarning, setShowPerformanceWarning] = useState(false);
  const [pendingGraphData, setPendingGraphData] = useState<GraphData | null>(null);
  const [userConfirmedLargeGraph, setUserConfirmedLargeGraph] = useState(false);
  const PERFORMANCE_THRESHOLD = 500;

  const graphData = useGraphStore((state) => state.graphData);
  const setGraphData = useGraphStore((state) => state.setGraphData);
  const setAutoPaths = useGraphStore((state) => state.setAutoPaths);
  const highlightAll = useGraphStore((state) => state.highlightAll);
  const selectedNodeId = useGraphStore((state) => state.selectedNodeId);
  const setSelectedNodeId = useGraphStore((state) => state.setSelectedNodeId);
  const activeFilters = useGraphStore((state) => state.activeFilters);
  const nodeTypeStyles = useGraphStore((state) => state.nodeTypeStyles);
  const groupingState = useGraphStore((state) => state.groupingState);
  const toggleGroupCollapse = useGraphStore((state) => state.toggleGroupCollapse);
  const isDarkMode = useGraphStore((state) => state.isDarkMode);
  const selectedCitation = useGraphStore((state) => state.selectedCitation);
  const activeWorkspaceTab = useGraphStore((state) => state.activeWorkspaceTab);
  const rendererKey = useMemo(
    () => (viewMode === '3d' ? (isDarkMode ? '3d-dark' : '3d-light') : '2d'),
    [viewMode, isDarkMode]
  );
  const hasData = Boolean(graphData && (graphData.nodes.length > 0 || graphData.edges.length > 0));
  const hasQueryResult = graphData !== null;
  const isEmptyResult = hasQueryResult && !hasData;

  const groupingStateRef = useRef(groupingState);
  const graphDataRef = useRef(graphData);
  const onGroupingUpdateRef = useRef(onGroupingUpdate);
  const isExpandingRef = useRef(false);
  const pendingExpandFocusRef = useRef<{ requestId: number; nodeId: string; focusIds: string[] } | null>(null);
  const expandFocusRequestRef = useRef(0);

  useEffect(() => {
    groupingStateRef.current = groupingState;
  }, [groupingState]);

  useEffect(() => {
    graphDataRef.current = graphData;
  }, [graphData]);

  useEffect(() => {
    onGroupingUpdateRef.current = onGroupingUpdate;
  }, [onGroupingUpdate]);

  // 展开节点状态
  const [isExpanding, setIsExpanding] = useState(false);

  useEffect(() => {
    isExpandingRef.current = isExpanding;
  }, [isExpanding]);

  // 过滤隐藏状态
  const [hiddenNodeIds, setHiddenNodeIds] = useState<Set<string>>(new Set());
  const [hiddenEdgeIds, setHiddenEdgeIds] = useState<Set<string>>(new Set());
  const scopeVersion = useGraphStore((state) => state.scopeVersion);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; pendingExpandFocusRef.current = null; };
  }, []);
  useEffect(() => {
    isExpandingRef.current = false;
    setIsExpanding(false);
    pendingExpandFocusRef.current = null;
    activeElementRef.current = null;
    setHiddenNodeIds(new Set());
    setHiddenEdgeIds(new Set());
    setTooltipVisible(false);
    setTooltipData(null);
    setContextMenuPosition(null);
    setContextMenuTarget(null);
    setVideoDialogOpen(false);
    setCurrentVideo(null);
    setShowPerformanceWarning(false);
    setPendingGraphData(null);
    setUserConfirmedLargeGraph(false);
  }, [scopeVersion]);
  const hiddenTypeNodeIds = useMemo(() => {
    if (!graphData || activeFilters.hiddenNodeTypes.length === 0) {
      return new Set<string>();
    }
    const hiddenTypes = new Set(activeFilters.hiddenNodeTypes);
    return new Set(
      graphData.nodes
        .filter((node) => node.labels.some((label) => hiddenTypes.has(label)))
        .map((node) => node.id)
    );
  }, [activeFilters.hiddenNodeTypes, graphData]);

  const handleVideoPlay = useCallback((videoUrl: string, title: string) => {
    setCurrentVideo({ url: videoUrl, title });
    setVideoDialogOpen(true);
  }, []);

  const handleExpandNode = useCallback(async (nodeId: string) => {
    if (isExpandingRef.current) return;

    const context = captureWorkspaceContext();
    const requestVersion = context.version;
    setIsExpanding(true);

    try {
      const data = await expandNode(nodeId, 'both', undefined, 30, context.kbId);
      if (!mounted.current || requestVersion !== useGraphStore.getState().scopeVersion || !isWorkspaceContextCurrent(context)) return;
      const currentData: GraphData = graphDataRef.current || {
        nodes: [],
        edges: [],
        stats: { nodeCount: 0, edgeCount: 0, executionTime: 0 },
      };

      const existingNodeIds = new Set(currentData.nodes.map((node) => node.id));
      const newNodes = data.nodes.filter((node) => !existingNodeIds.has(node.id));

      const existingEdgeIds = new Set(currentData.edges.map((edge) => edge.id));
      const newEdges = data.edges.filter((edge) => !existingEdgeIds.has(edge.id));

      const mergedData = {
        nodes: [...currentData.nodes, ...newNodes],
        edges: [...currentData.edges, ...newEdges],
        stats: {
          nodeCount: currentData.nodes.length + newNodes.length,
          edgeCount: currentData.edges.length + newEdges.length,
          executionTime: data.stats?.executionTime || 0,
        },
      };

      setGraphData(mergedData);
      setSelectedNodeId(nodeId);
      activeElementRef.current = { type: 'node', id: nodeId };

      const apiNodeIds = data.nodes.map((node) => node.id);
      const existingNeighborIds = rendererRef.current?.getNeighbors(nodeId) || [];
      const focusIds = Array.from(new Set([nodeId, ...apiNodeIds, ...existingNeighborIds]));
      const requestId = expandFocusRequestRef.current + 1;
      expandFocusRequestRef.current = requestId;
      pendingExpandFocusRef.current = { requestId, nodeId, focusIds };

      [180, 520, 1000].forEach((delay, index, delays) => {
        window.setTimeout(() => {
          const pendingFocus = pendingExpandFocusRef.current;
          if (!pendingFocus || pendingFocus.requestId !== requestId) return;
          rendererRef.current?.setActiveElement({ type: 'node', id: pendingFocus.nodeId });
          rendererRef.current?.fitTo(pendingFocus.focusIds, 90);
          if (index === delays.length - 1) {
            pendingExpandFocusRef.current = null;
          }
        }, delay);
      });
    } catch (error) {
      if (mounted.current && isWorkspaceContextCurrent(context)) console.error('Failed to expand node:', error);
      if (isWorkspaceContextCurrent(context)) pendingExpandFocusRef.current = null;
    } finally {
      if (mounted.current && isWorkspaceContextCurrent(context)) setIsExpanding(false);
    }
  }, [rendererRef, setGraphData, setSelectedNodeId]);

  const handleVideoClose = () => {
    setVideoDialogOpen(false);
    setCurrentVideo(null);
  };

  const handlePerformanceContinue = () => {
    setShowPerformanceWarning(false);
    setPendingGraphData(null);
    setUserConfirmedLargeGraph(true);
  };

  const handlePerformanceCancel = () => {
    setShowPerformanceWarning(false);
    setPendingGraphData(null);
    setUserConfirmedLargeGraph(false);
    const emptyData = { nodes: [], edges: [], stats: { nodeCount: 0, edgeCount: 0, executionTime: 0 } };
    setGraphData(emptyData);
  };

  useEffect(() => {
    if (graphData && graphData.nodes.length > 0) {
      setUserConfirmedLargeGraph(false);
    }
  }, [graphData]);

  const rendererData = useMemo(() => {
    if (graphData && graphData.nodes.length > PERFORMANCE_THRESHOLD && !userConfirmedLargeGraph && !pendingGraphData) {
      setPendingGraphData(graphData);
      setShowPerformanceWarning(true);
      return null;
    }

    return adaptGraphData(graphData, nodeTypeStyles);
  }, [graphData, nodeTypeStyles, pendingGraphData, userConfirmedLargeGraph]);

  useEffect(() => {
    rendererDataRef.current = rendererData;
  }, [rendererData]);

  const collapsedNodeIds = useMemo(() => {
    const ids = new Set<string>();
    groupingState.groups.forEach((group) => {
      if (group.collapsed) {
        group.nodeIds.forEach((id) => ids.add(id));
      }
    });
    return ids;
  }, [groupingState.groups]);

  const handleHideNode = useCallback((id: string) => {
    setHiddenNodeIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);

  const handleShowNode = useCallback((id: string) => {
    setHiddenNodeIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const handleHideEdge = useCallback((id: string) => {
    setHiddenEdgeIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);

  const handleShowEdge = useCallback((id: string) => {
    setHiddenEdgeIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const handleRendererClick = useCallback((payload: { type: 'node' | 'edge' | 'background'; id?: string; x: number; y: number }) => {
    if (!rendererRef.current) return;

    if (payload.type === 'node' && payload.id) {
      setSelectedNodeId(payload.id);
      activeElementRef.current = { type: 'node', id: payload.id };
      rendererRef.current.setActiveElement(activeElementRef.current);
      return;
    }

    if (payload.type === 'edge' && payload.id) {
      setSelectedNodeId(null);
      activeElementRef.current = { type: 'edge', id: payload.id };
      rendererRef.current.setActiveElement(activeElementRef.current);
      return;
    }

    setSelectedNodeId(null);
    activeElementRef.current = null;
    rendererRef.current.setActiveElement(null);
    setContextMenuPosition(null);
    setContextMenuTarget(null);
  }, [rendererRef, setSelectedNodeId]);

  const handleRendererDoubleClick = useCallback((payload: { type: 'node' | 'background'; id?: string }) => {
    if (payload.type !== 'node' || !payload.id || !rendererRef.current) return;

    const node = rendererRef.current.getNodeById(payload.id);
    if (!node) return;

    if (node.type === 'group') {
      toggleGroupCollapse(node.id);
      onGroupingUpdateRef.current?.();
      return;
    }

    const videoUrl = node.video || node.originalVideoUrl;

    if ((node.mediaType === 'video' || node.isVideo) && videoUrl) {
      handleVideoPlay(videoUrl, node.label);
      return;
    }

    if (videoUrl) {
      handleVideoPlay(videoUrl, node.label);
      return;
    }

    handleExpandNode(node.id);
  }, [handleExpandNode, handleVideoPlay, toggleGroupCollapse, rendererRef]);

  const handleRendererContextMenu = useCallback((payload: { type: 'node' | 'edge' | 'background'; id?: string; x: number; y: number }) => {
    if (payload.type === 'background') {
      setContextMenuPosition(null);
      setContextMenuTarget(null);
      return;
    }

    if (payload.id) {
      setContextMenuPosition({ top: payload.y, left: payload.x });
      setContextMenuTarget({ type: payload.type, id: payload.id });
    }
  }, []);

  const handleRendererHover = useCallback((payload: { type: 'node' | 'edge' | 'background'; id?: string; x: number; y: number }) => {
    if (!rendererRef.current) return;

    if (payload.type !== 'node' || !payload.id) {
      setTooltipVisible(false);
      setTooltipData(null);
      return;
    }

    const node = rendererRef.current.getNodeById(payload.id);
    if (!node) return;

    if (node.type === 'group') {
      const group = groupingStateRef.current.groups.find((g) => g.id === node.id);
      setTooltipData({
        id: node.id,
        label: node.label || '分组',
        type: '分组',
        properties: {
          节点数量: group?.nodeIds.length || 0,
          状态: group?.collapsed ? '已折叠' : '已展开',
        },
      });
    } else {
      setTooltipData({
        id: node.id,
        label: node.label,
        type: node.type,
        properties: node.properties,
      });
    }

    setTooltipPosition({ x: payload.x, y: payload.y });
    setTooltipVisible(true);
  }, [rendererRef]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const container3d = threeContainerRef.current;
    const is3D = rendererKey !== '2d';
    if (!is3D && !canvas) return;
    if (is3D && !container3d) return;
    if (is3D && !hasData) return;

    setRendererError(null);
    const styleName = rendererKey === '3d-dark' ? 'kgCosmic' : 'kgVivid';
    const maxAttempts = is3D ? 3 : 1;
    const retryDelayMs = 400;
    let renderer: RendererAPI | null = null;
    let disposed = false;
    let retryTimer: number | null = null;
    let attempt = 0;

    const clearRetryTimer = () => {
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const logInitFailure = (error: unknown, attemptNo: number) => {
      const errText = error instanceof Error ? error.message : String(error);
      if (is3D) {
        const isFinal = attemptNo >= maxAttempts;
        reportClientLog({
          level: isFinal ? 'error' : 'warn',
          message: isFinal
            ? `3D 渲染器初始化失败（已重试 ${Math.max(0, maxAttempts - 1)} 次）`
            : `3D 渲染器初始化失败，准备重试（${attemptNo}/${maxAttempts}）`,
          source: 'renderer3d',
          event: 'init',
          context: {
            error: errText,
            attempt: attemptNo,
            maxAttempts,
            styleName,
          },
        });
      } else {
        reportClientLog({
          level: 'error',
          message: '2D 渲染器初始化失败',
          source: 'renderer2d',
          event: 'init',
          context: {
            error: errText,
          },
        });
      }
    };

    const initRenderer = async () => {
      if (disposed) return;
      if (is3D) {
        const rect = container3d!.getBoundingClientRect();
        const containerReady = rect.width >= 48 && rect.height >= 48 && container3d!.getClientRects().length > 0;
        if (!containerReady) {
          retryTimer = window.setTimeout(() => {
            void initRenderer();
          }, 120);
          return;
        }
      }

      attempt += 1;
      try {
        if (!is3D) {
          renderer = createRenderer(
            canvas!,
            {
              onClick: handleRendererClick,
              onDoubleClick: handleRendererDoubleClick,
              onContextMenu: handleRendererContextMenu,
              onHover: handleRendererHover,
            },
            { minZoom: 0.25, maxZoom: 3, initialZoom: 0.5 }
          );
        } else {
          const { createRenderer3D } = await import('../../renderers/force3d/renderer');
          if (disposed) return;
          renderer = createRenderer3D(
            container3d!,
            {
              onClick: handleRendererClick,
              onDoubleClick: handleRendererDoubleClick,
              onContextMenu: handleRendererContextMenu,
              onHover: handleRendererHover,
            },
            {
              minZoom: 0.25,
              maxZoom: 3,
              initialZoom: 0.5,
              styleName,
            }
          );
        }
      } catch (error) {
        console.error('Failed to initialize renderer:', error);
        logInitFailure(error, attempt);
        if (is3D && attempt < maxAttempts) {
          retryTimer = window.setTimeout(() => {
            void initRenderer();
          }, retryDelayMs);
          return;
        }
        if (!disposed) {
          setRendererError(
            is3D
              ? '3D 渲染器初始化失败，已自动重试，请查看日志面板。'
              : '渲染器初始化失败。'
          );
        }
        return;
      }

      if (disposed) {
        renderer?.destroy();
        return;
      }

      rendererRef.current = renderer;
      if (rendererDataRef.current) {
        renderer.updateData(rendererDataRef.current);
      }
      if (is3D && attempt > 1) {
        reportClientLog({
          level: 'info',
          message: `3D 渲染器初始化成功（重试 ${attempt - 1} 次）`,
          source: 'renderer3d',
          event: 'init',
          context: {
            attempt,
            styleName,
          },
        });
      }
    };

    void initRenderer();

    return () => {
      disposed = true;
      clearRetryTimer();
      renderer?.destroy();
      if (rendererRef.current === renderer) {
        rendererRef.current = null;
      }
    };
  }, [
    handleRendererClick,
    handleRendererContextMenu,
    handleRendererDoubleClick,
    handleRendererHover,
    hasData,
    rendererRef,
    rendererKey,
  ]);

  useEffect(() => {
    if (!rendererRef.current || !rendererData) return;
    rendererRef.current.updateData(rendererData);
  }, [rendererData, rendererRef, viewMode, rendererKey]);

  useEffect(() => {
    if (!rendererRef.current) return;

    const combinedHiddenNodes = new Set<string>([
      ...hiddenNodeIds,
      ...collapsedNodeIds,
      ...hiddenTypeNodeIds,
    ]);
    rendererRef.current.setFilter({
      nodeTypes: activeFilters.nodeTypes,
      edgeTypes: activeFilters.relationshipTypes,
      hiddenNodeIds: combinedHiddenNodes,
      hiddenEdgeIds,
    });
  }, [activeFilters, collapsedNodeIds, hiddenEdgeIds, hiddenNodeIds, hiddenTypeNodeIds, rendererRef, viewMode, rendererKey]);

  useEffect(() => {
    if (!rendererRef.current) return;

    if (selectedNodeId) {
      activeElementRef.current = { type: 'node', id: selectedNodeId };
      rendererRef.current.setActiveElement(activeElementRef.current);
      return;
    }

    if (activeElementRef.current?.type === 'node') {
      activeElementRef.current = null;
      rendererRef.current.setActiveElement(null);
    }
  }, [rendererRef, selectedNodeId, viewMode, rendererKey]);

  useEffect(() => {
    if (!rendererRef.current || !rendererData || rendererData.nodes.length === 0) return;
    if (pendingExpandFocusRef.current) return;

    const timer = window.setTimeout(() => {
      rendererRef.current?.fitTo(undefined, SAFE_FIT_PADDING);
    }, 200);

    return () => window.clearTimeout(timer);
  }, [graphData?.stats?.executionTime, rendererData, rendererRef, viewMode, rendererKey]);

  useEffect(() => {
    if (!rendererRef.current) return;
    if (highlightAll && rendererDataRef.current) {
      const nodeIds = rendererDataRef.current.nodes.map((node) => node.id);
      const edgeIds = rendererDataRef.current.edges.map((edge) => edge.id);
      rendererRef.current.setSearchHighlight({ nodeIds, edgeIds });
      if (activeWorkspaceTab === 'graph' && !pendingExpandFocusRef.current) {
        rendererRef.current.fitTo(nodeIds, SAFE_FIT_PADDING);
      }
      rendererRef.current.setPathHighlight({ nodeIds: [], edgeIds: [] });
      return;
    }
    if (!selectedCitation || !rendererDataRef.current) {
      rendererRef.current.clearSearchHighlight();
      rendererRef.current.setPathHighlight({ nodeIds: [], edgeIds: [] });
      setAutoPaths([]);
      return;
    }

    const citationEntityNames = new Set(
      (selectedCitation.entityNames || [])
        .map((name) => (name || '').trim().toLowerCase())
        .filter(Boolean)
    );
    const citationKeywords = new Set(
      (selectedCitation.keywords || [])
        .map((item) => (item || '').trim().toLowerCase())
        .filter(Boolean)
    );
    const docId = selectedCitation.id.split('-')[0];
    const matches = rendererDataRef.current.nodes
      .filter((node) => {
        const props = node.properties || {};
        if (props.chunk_id && props.chunk_id === selectedCitation.id) return true;
        if (props.doc_id && props.doc_id === docId) return true;
        if (props.name && selectedCitation.title && props.name === selectedCitation.title) return true;
        return false;
      })
      .map((node) => node.id);

    const entityMatches = new Set<string>();
    const keywordNodeMatches = new Set<string>();
    rendererDataRef.current.nodes.forEach((node) => {
      const props = node.properties || {};
      const nodeType = String(node.type || '').toLowerCase();
      const candidateLabel = String(props.name || props.title || node.label || '').trim().toLowerCase();
      if (citationKeywords.size && candidateLabel) {
        for (const keyword of citationKeywords) {
          if (candidateLabel.includes(keyword) || keyword.includes(candidateLabel)) {
            keywordNodeMatches.add(node.id);
            break;
          }
        }
      }
      if (nodeType !== 'entity') return;
      const candidateName = String(props.name || node.label || '').trim().toLowerCase();
      if (candidateName && citationEntityNames.has(candidateName)) {
        entityMatches.add(node.id);
        return;
      }
      if (citationKeywords.size) {
        for (const keyword of citationKeywords) {
          if (candidateName && (candidateName.includes(keyword) || keyword.includes(candidateName))) {
            entityMatches.add(node.id);
            break;
          }
        }
      }
    });

    if (rendererRef.current && matches.length > 0) {
      matches.forEach((id: string) => {
        const neighbors = rendererRef.current?.getNeighbors(id) || [];
        neighbors.forEach((neighborId: string) => {
          const node = rendererRef.current?.getNodeById(neighborId);
          if (node?.type === 'Entity') {
            entityMatches.add(neighborId);
          }
        });
      });
    }

    const highlightIds = Array.from(new Set([...matches, ...entityMatches, ...keywordNodeMatches]));
    const highlightIdSet = new Set(highlightIds);
    const highlightEdgeIds = rendererDataRef.current.edges
      .filter((edge) => {
        const source = String(edge.source || '');
        const target = String(edge.target || '');
        return highlightIdSet.has(source) && highlightIdSet.has(target);
      })
      .map((edge) => edge.id);

    if (highlightIds.length > 0) {
      rendererRef.current.setSearchHighlight({ nodeIds: highlightIds, edgeIds: highlightEdgeIds });
      if (activeWorkspaceTab === 'graph' && !pendingExpandFocusRef.current) {
        rendererRef.current.fitTo(highlightIds, SAFE_FIT_PADDING);
      }
    } else {
      rendererRef.current.clearSearchHighlight();
    }

    const adjacency = new Map<string, Array<{ id: string; edgeId: string }>>();
    rendererDataRef.current.edges.forEach((edge) => {
      const source = String(edge.source || '');
      const target = String(edge.target || '');
      if (!source || !target) return;
      if (!adjacency.has(source)) adjacency.set(source, []);
      if (!adjacency.has(target)) adjacency.set(target, []);
      adjacency.get(source)?.push({ id: target, edgeId: edge.id });
      adjacency.get(target)?.push({ id: source, edgeId: edge.id });
    });

    const findShortestPath = (start: string, goal: string, maxLen = 6) => {
      if (start === goal) return null;
      const queue: Array<{ node: string; path: string[]; edges: string[] }> = [
        { node: start, path: [start], edges: [] },
      ];
      const visited = new Set<string>([start]);
      while (queue.length) {
        const current = queue.shift()!;
        if (current.path.length > maxLen) continue;
        const neighbors = adjacency.get(current.node) || [];
        for (const next of neighbors) {
          if (visited.has(next.id)) continue;
          const nextPath = [...current.path, next.id];
          const nextEdges = [...current.edges, next.edgeId];
          if (next.id === goal) {
            return { nodes: nextPath, edges: nextEdges };
          }
          visited.add(next.id);
          queue.push({ node: next.id, path: nextPath, edges: nextEdges });
        }
      }
      return null;
    };

    const candidateNodes = Array.from(new Set([...entityMatches, ...keywordNodeMatches])).slice(0, 8);
    const autoPaths: Array<{ id: string; nodes: string[]; edges: string[]; length: number; weight: number }> = [];
    for (let i = 0; i < candidateNodes.length; i += 1) {
      for (let j = i + 1; j < candidateNodes.length; j += 1) {
        const pathFound = findShortestPath(candidateNodes[i], candidateNodes[j]);
        if (pathFound) {
          autoPaths.push({
            id: `auto_path_${candidateNodes[i]}_${candidateNodes[j]}`,
            nodes: pathFound.nodes,
            edges: pathFound.edges,
            length: pathFound.nodes.length,
            weight: pathFound.edges.length,
          });
        }
        if (autoPaths.length >= 3) break;
      }
      if (autoPaths.length >= 3) break;
    }

    setAutoPaths(autoPaths);
    if (autoPaths.length > 0) {
      const primary = autoPaths[0];
      rendererRef.current.setPathHighlight({ nodeIds: primary.nodes, edgeIds: primary.edges });
      if (activeWorkspaceTab === 'graph' && !pendingExpandFocusRef.current) {
        rendererRef.current.fitTo(primary.nodes, 90);
      }
    } else {
      rendererRef.current.setPathHighlight({ nodeIds: [], edgeIds: [] });
    }
  }, [selectedCitation, activeWorkspaceTab, rendererData, rendererRef, viewMode, rendererKey, setAutoPaths, highlightAll]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;

    const updateSize = () => {
      const rect = element.getBoundingClientRect();
      setCanvasSize({ width: rect.width, height: rect.height });
    };

    updateSize();

    const observer = new ResizeObserver(updateSize);
    observer.observe(element);

    return () => observer.disconnect();
  }, []);

  return (
    <Box
      ref={containerRef}
      data-testid="graph-canvas-root"
      sx={{
        position: 'relative',
        width: '100%',
        height: '100%',
        bgcolor: 'background.default',
      }}
    >
      <canvas
        ref={canvasRef}
        style={{
          width: '100%',
          height: '100%',
          display: hasData && viewMode === '2d' ? 'block' : 'none',
        }}
      />
      <Box
        ref={threeContainerRef}
        data-testid="graph-canvas-3d"
        sx={{
          position: 'absolute',
          inset: 0,
          width: '100%',
          height: '100%',
          overflow: 'hidden',
          display: hasData && viewMode === '3d' ? 'block' : 'none',
        }}
      />

      {!hasData && (
        <Box
          sx={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'text.secondary',
          }}
        >
          <Typography variant="h5" gutterBottom>
            图谱画布
          </Typography>
          <Typography variant="body2">
            {isEmptyResult
              ? '查询无结果，数据库可能为空或查询条件不匹配。'
              : '执行 Cypher 查询以显示图谱'}
          </Typography>
          {isEmptyResult && (
            <Typography variant="caption" sx={{ mt: 0.5 }}>
              可尝试重新执行自动发现查询，或放宽当前过滤条件。
            </Typography>
          )}
        </Box>
      )}

      {rendererError && (
        <Box
          sx={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'error.main',
            bgcolor: 'rgba(0, 0, 0, 0.04)',
            backdropFilter: 'blur(2px)',
            zIndex: 10,
            textAlign: 'center',
            px: 2,
          }}
        >
          <Typography variant="h6" sx={{ mb: 1 }}>
            渲染失败
          </Typography>
          <Typography variant="body2">
            {rendererError}
          </Typography>
        </Box>
      )}

      <GraphControls
        rendererRef={rendererRef}
        onToggleNavigation={viewMode === '2d' ? () => setNavigationOpen((prev) => !prev) : undefined}
        navigationOpen={navigationOpen}
        viewMode={viewMode}
        onToggleViewMode={() => setViewMode((prev) => (prev === '2d' ? '3d' : '2d'))}
      />
      {viewMode === '2d' && <Minimap rendererRef={rendererRef} viewportSize={canvasSize} />}
      {viewMode === '2d' && (
        <NavigationPanel
          rendererRef={rendererRef}
          isOpen={navigationOpen}
          onClose={() => setNavigationOpen(false)}
        />
      )}
      <NodeTooltip
        visible={tooltipVisible}
        x={tooltipPosition.x}
        y={tooltipPosition.y}
        nodeData={tooltipData}
      />

      <ContextMenu
        rendererRef={rendererRef}
        anchorPosition={contextMenuPosition}
        onClose={() => {
          setContextMenuPosition(null);
          setContextMenuTarget(null);
        }}
        target={contextMenuTarget}
        hiddenNodeIds={hiddenNodeIds}
        hiddenEdgeIds={hiddenEdgeIds}
        onHideNode={handleHideNode}
        onShowNode={handleShowNode}
        onHideEdge={handleHideEdge}
        onShowEdge={handleShowEdge}
        onExpandNode={handleExpandNode}
        viewportSize={canvasSize}
      />


      <Dialog
        open={videoDialogOpen}
        onClose={handleVideoClose}
        maxWidth="md"
        fullWidth
        PaperProps={{
          sx: {
            bgcolor: 'background.paper',
            minHeight: '400px',
            zIndex: 9999,
          },
        }}
        sx={{
          zIndex: 9999,
        }}
      >
        <DialogTitle
          sx={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          {currentVideo?.title || '视频播放'}
          <IconButton onClick={handleVideoClose} size="small">
            <CloseIcon />
          </IconButton>
        </DialogTitle>
        <DialogContent>
          {currentVideo && (
            <video
              controls
              autoPlay
              style={{
                width: '100%',
                height: 'auto',
                maxHeight: '400px',
              }}
            >
              <source src={currentVideo.url} type="video/mp4" />
              您的浏览器不支持视频播放。
            </video>
          )}
        </DialogContent>
      </Dialog>

      <PerformanceWarningDialog
        open={showPerformanceWarning}
        nodeCount={pendingGraphData?.nodes.length || 0}
        onContinue={handlePerformanceContinue}
        onCancel={handlePerformanceCancel}
      />

      {isExpanding && (
        <Box
          sx={{
            position: 'absolute',
            top: 16,
            left: '50%',
            transform: 'translateX(-50%)',
            bgcolor: 'primary.main',
            color: 'white',
            px: 2,
            py: 1,
            borderRadius: 2,
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            boxShadow: 2,
            zIndex: 1000,
          }}
        >
          <Typography variant="body2">正在展开节点...</Typography>
        </Box>
      )}
    </Box>
  );
}
