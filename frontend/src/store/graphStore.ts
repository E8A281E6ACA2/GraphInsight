import { create } from 'zustand';
import { persist } from 'zustand/middleware';

// 类型定义
export interface Node {
  id: string;
  labels: string[];
  properties: Record<string, unknown>;
}

export interface Edge {
  id: string;
  source: string;
  target: string;
  type: string;
  properties: Record<string, unknown>;
}

export interface GraphData {
  nodes: Node[];
  edges: Edge[];
  stats?: {
    nodeCount: number;
    edgeCount: number;
    executionTime: number;
  };
}

export interface GraphPathInfo {
  id: string;
  nodes: string[];
  edges: string[];
  length: number;
  weight: number;
  label?: string;
}

export interface QueryHistoryItem {
  id: string;
  cypher: string;
  timestamp: number;
  resultCount: number;
}

export interface CitationRef {
  id: string;
  title: string;
  snippet: string;
  location?: string;
  entityNames?: string[];
  keywords?: string[];
  retrievalScore?: number;
  confidence?: number;
  confidenceLevel?: string;
}

export interface FilterState {
  nodeTypes: string[];
  relationshipTypes: string[];
  hiddenNodeTypes: string[];
}

// 节点分组相关类型
export interface NodeGroup {
  id: string;
  name: string;
  nodeIds: string[];
  color: string;
  collapsed: boolean;
  position?: { x: number; y: number };
  style?: {
    backgroundColor: string;
    borderColor: string;
    borderWidth: number;
    opacity: number;
  };
}

export interface GroupingState {
  groups: NodeGroup[];
  autoGroupByType: boolean;
  showGroupLabels: boolean;
}

interface GraphStore {
  // 当前工作区激活的知识库（M4-R1 步骤 3：所有业务调用按此作用域发起）
  activeKbId: string | null;
  setActiveKbId: (id: string | null) => void;

  // 图数据
  graphData: GraphData | null;
  setGraphData: (data: GraphData) => void;

  // 选中的节点
  selectedNodeId: string | null;
  setSelectedNodeId: (id: string | null) => void;

  // 查询历史
  queryHistory: QueryHistoryItem[];
  addQueryToHistory: (cypher: string, resultCount: number) => void;
  clearQueryHistory: () => void;

  // 过滤器
  activeFilters: FilterState;
  setNodeTypeFilter: (types: string[]) => void;
  setRelationshipTypeFilter: (types: string[]) => void;
  toggleHiddenNodeType: (nodeType: string) => void;
  isNodeTypeHidden: (nodeType: string) => boolean;
  clearFilters: () => void;

  // 主题
  isDarkMode: boolean;
  toggleTheme: () => void;

  // 工作区视图
  activeWorkspaceTab: 'document' | 'graph';
  setWorkspaceTab: (tab: 'document' | 'graph') => void;
  selectedCitation: CitationRef | null;
  setSelectedCitation: (citation: CitationRef | null) => void;
  highlightAll: boolean;
  setHighlightAll: (value: boolean) => void;
  recentUploadedDocIds: string[];
  setRecentUploadedDocIds: (docIds: string[]) => void;
  documentRefreshKey: number;
  requestDocumentRefresh: () => void;

  // 自动推理链
  autoPaths: GraphPathInfo[];
  setAutoPaths: (paths: GraphPathInfo[]) => void;

  // 查询统计
  lastQueryStats: {
    nodeCount: number;
    edgeCount: number;
    executionTime: number;
  } | null;
  setLastQueryStats: (stats: { nodeCount: number; edgeCount: number; executionTime: number }) => void;



  // 节点样式配置 - 按标签类型配置
  nodeTypeStyles: Record<string, {
    color: string;
    size: number;
    borderWidth: number;
    showLabels: boolean;
    labelSize: number;
    showImages: boolean;
    caption: string[]; // 显示的属性
  }>;
  setNodeTypeStyle: (nodeType: string, style: {
    color: string;
    size: number;
    borderWidth: number;
    showLabels: boolean;
    labelSize: number;
    showImages: boolean;
    caption: string[];
  }) => void;

  // 节点分组功能
  groupingState: GroupingState;
  setAutoGroupByType: (enabled: boolean) => void;

  // 布局偏好
  preferredLayout: string;
  setPreferredLayout: (layout: string) => void;
  setShowGroupLabels: (show: boolean) => void;
  createGroup: (name: string, nodeIds: string[], color?: string) => void;
  updateGroup: (groupId: string, updates: Partial<NodeGroup>) => void;
  deleteGroup: (groupId: string) => void;
  addNodesToGroup: (groupId: string, nodeIds: string[]) => void;
  removeNodesFromGroup: (groupId: string, nodeIds: string[]) => void;
  toggleGroupCollapse: (groupId: string) => void;
  clearAllGroups: () => void;
}

export const useGraphStore = create<GraphStore>()(
  persist(
    (set, get) => ({
      // 初始状态
      activeKbId: null,
      graphData: null,
      selectedNodeId: null,
      queryHistory: [],
      activeFilters: {
        nodeTypes: [],
        relationshipTypes: [],
        hiddenNodeTypes: [],
      },
      isDarkMode: false,
      activeWorkspaceTab: 'document',
      selectedCitation: null,
      highlightAll: false,
      recentUploadedDocIds: [],
      documentRefreshKey: 0,
      autoPaths: [],
      lastQueryStats: null,
      nodeTypeStyles: {},
      groupingState: {
        groups: [],
        autoGroupByType: false,
        showGroupLabels: true,
      },
      preferredLayout: 'cose',

      // Actions
      // 切换知识库时重置视图：清空图数据/选中节点/选中引用，避免跨库脏数据。
      setActiveKbId: (id) =>
        set({
          activeKbId: id,
          graphData: null,
          selectedNodeId: null,
          selectedCitation: null,
        }),

      setGraphData: (data) => set({ graphData: data }),

      setSelectedNodeId: (id) => set({ selectedNodeId: id }),

      addQueryToHistory: (cypher, resultCount) =>
        set((state) => {
          const newItem: QueryHistoryItem = {
            id: Date.now().toString(),
            cypher,
            timestamp: Date.now(),
            resultCount,
          };
          // 保持最多 20 条历史记录
          const newHistory = [newItem, ...state.queryHistory].slice(0, 20);
          return { queryHistory: newHistory };
        }),

      clearQueryHistory: () => set({ queryHistory: [] }),

      setNodeTypeFilter: (types) =>
        set((state) => ({
          activeFilters: { ...state.activeFilters, nodeTypes: types },
        })),

      setRelationshipTypeFilter: (types) =>
        set((state) => ({
          activeFilters: { ...state.activeFilters, relationshipTypes: types },
        })),

      toggleHiddenNodeType: (nodeType) =>
        set((state) => {
          const current = state.activeFilters.hiddenNodeTypes;
          const exists = current.includes(nodeType);
          return {
            activeFilters: {
              ...state.activeFilters,
              hiddenNodeTypes: exists
                ? current.filter((item) => item !== nodeType)
                : [...current, nodeType],
            },
          };
        }),

      isNodeTypeHidden: (nodeType) => get().activeFilters.hiddenNodeTypes.includes(nodeType),

      clearFilters: () =>
        set({
          activeFilters: {
            nodeTypes: [],
            relationshipTypes: [],
            hiddenNodeTypes: [],
          },
        }),

      toggleTheme: () => set((state) => ({ isDarkMode: !state.isDarkMode })),

      setWorkspaceTab: (tab) => set({ activeWorkspaceTab: tab }),
      setSelectedCitation: (citation) => set({ selectedCitation: citation }),
      setHighlightAll: (value) => set({ highlightAll: value }),
      setRecentUploadedDocIds: (docIds) => set({ recentUploadedDocIds: docIds }),
      requestDocumentRefresh: () =>
        set((state) => ({ documentRefreshKey: state.documentRefreshKey + 1 })),

      setAutoPaths: (paths) => set({ autoPaths: paths }),

      setLastQueryStats: (stats) => set({ lastQueryStats: stats }),

      setNodeTypeStyle: (nodeType, style) =>
        set((state) => {
          console.log('🎨 GraphStore - Setting node type style:', nodeType, style);
          const newStyles = {
            ...state.nodeTypeStyles,
            [nodeType]: style,
          };
          console.log('🎨 GraphStore - Updated nodeTypeStyles:', newStyles);
          return {
            nodeTypeStyles: newStyles,
          };
        }),

      // 节点分组功能
      setAutoGroupByType: (enabled) =>
        set((state) => ({
          groupingState: {
            ...state.groupingState,
            autoGroupByType: enabled,
          },
        })),

      setShowGroupLabels: (show) =>
        set((state) => ({
          groupingState: {
            ...state.groupingState,
            showGroupLabels: show,
          },
        })),

      createGroup: (name, nodeIds, color = '#1976d2') =>
        set((state) => {
          const newGroup: NodeGroup = {
            id: `group_${Date.now()}`,
            name,
            nodeIds: [...nodeIds],
            color,
            collapsed: false,
            style: {
              backgroundColor: color + '20',
              borderColor: color,
              borderWidth: 2,
              opacity: 0.8,
            },
          };
          return {
            groupingState: {
              ...state.groupingState,
              groups: [...state.groupingState.groups, newGroup],
            },
          };
        }),

      updateGroup: (groupId, updates) =>
        set((state) => ({
          groupingState: {
            ...state.groupingState,
            groups: state.groupingState.groups.map((group) =>
              group.id === groupId ? { ...group, ...updates } : group
            ),
          },
        })),

      deleteGroup: (groupId) =>
        set((state) => ({
          groupingState: {
            ...state.groupingState,
            groups: state.groupingState.groups.filter((group) => group.id !== groupId),
          },
        })),

      addNodesToGroup: (groupId, nodeIds) =>
        set((state) => ({
          groupingState: {
            ...state.groupingState,
            groups: state.groupingState.groups.map((group) =>
              group.id === groupId
                ? {
                    ...group,
                    nodeIds: [...new Set([...group.nodeIds, ...nodeIds])],
                  }
                : group
            ),
          },
        })),

      removeNodesFromGroup: (groupId, nodeIds) =>
        set((state) => ({
          groupingState: {
            ...state.groupingState,
            groups: state.groupingState.groups.map((group) =>
              group.id === groupId
                ? {
                    ...group,
                    nodeIds: group.nodeIds.filter((id) => !nodeIds.includes(id)),
                  }
                : group
            ),
          },
        })),

      toggleGroupCollapse: (groupId) =>
        set((state) => ({
          groupingState: {
            ...state.groupingState,
            groups: state.groupingState.groups.map((group) =>
              group.id === groupId ? { ...group, collapsed: !group.collapsed } : group
            ),
          },
        })),

      clearAllGroups: () =>
        set((state) => ({
          groupingState: {
            ...state.groupingState,
            groups: [],
          },
        })),

      setPreferredLayout: (layout) => set({ preferredLayout: layout }),
    }),
    {
      name: 'graph-insight-storage', // localStorage key
      partialize: (state) => ({
        // 只持久化主题偏好、激活知识库、查询历史、标签配置、样式配置和分组配置
        isDarkMode: state.isDarkMode,
        activeKbId: state.activeKbId,
        queryHistory: state.queryHistory,
        nodeTypeStyles: state.nodeTypeStyles,
        activeFilters: state.activeFilters,
        groupingState: state.groupingState,
        preferredLayout: state.preferredLayout,
      }),
    }
  )
);
