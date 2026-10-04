export type RendererNode = {
  id: string;
  label: string;
  color: string;
  radius: number;
  type: string;
  properties: Record<string, unknown>;
  cluster?: string | null;
  neighbors: string[];
  degree: number;
  indegree: number;
  outdegree: number;
  mediaType?: 'image' | 'video' | 'audio' | 'mixed';
  image?: string;
  video?: string;
  audio?: string;
  isVideo?: boolean;
  videoThumbnailUrl?: string;
  originalVideoUrl?: string;
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
};

export type RendererEdge = {
  id: string;
  source: string;
  target: string;
  predicate: string;
  color: string;
  type: string;
  properties: Record<string, unknown>;
};

export type RendererCluster = {
  id: string;
  label: string;
  color: string;
  size: number;
  members: string[];
};

export type RendererData = {
  nodes: RendererNode[];
  edges: RendererEdge[];
  clusters: RendererCluster[];
  topEntities: Array<{ id: string; label: string; degree: number; cluster?: string | null }>;
  topRelations: Array<{ predicate: string; count: number; color: string }>;
  stats: {
    entities: number;
    relations: number;
    relationTypes: number;
    entityClusters: number;
    edgeClusters: number;
    isolatedEntities: number;
    components: number;
    averageDegree: number;
  };
};

export type RendererActiveElement =
  | { type: 'node'; id: string }
  | { type: 'edge'; id: string };

export type RendererEventHandlers = {
  onClick?: (payload: { type: 'node' | 'edge' | 'background'; id?: string; x: number; y: number }) => void;
  onDoubleClick?: (payload: { type: 'node' | 'background'; id?: string }) => void;
  onContextMenu?: (payload: { type: 'node' | 'edge' | 'background'; id?: string; x: number; y: number }) => void;
  onHover?: (payload: { type: 'node' | 'edge' | 'background'; id?: string; x: number; y: number }) => void;
  onTransform?: (transform: { x: number; y: number; k: number }) => void;
};

export type LayoutConfigValue = string | number | boolean | number[] | ((...args: never[]) => unknown) | undefined;
export type LayoutConfig = Record<string, LayoutConfigValue>;

export type RendererAPI = {
  updateData: (data: RendererData) => void;
  applyLayout: (layout: string, config?: LayoutConfig) => void;
  setActiveElement: (active: RendererActiveElement | null) => void;
  setSearchHighlight: (payload: { nodeIds?: string[]; edgeIds?: string[] }) => void;
  clearSearchHighlight: () => void;
  setPathHighlight: (payload: { nodeIds?: string[]; edgeIds?: string[] }) => void;
  clearPathHighlight: () => void;
  setFilter: (filter: {
    nodeTypes?: string[];
    edgeTypes?: string[];
    hiddenNodeIds?: Set<string>;
    hiddenEdgeIds?: Set<string>;
  }) => void;
  setNodeSizeOverrides: (overrides: Record<string, number> | Map<string, number> | null) => void;
  zoomBy: (factor: number) => void;
  zoomTo: (k: number) => void;
  panTo: (x: number, y: number) => void;
  center: () => void;
  fitTo: (nodeIds?: string[], padding?: number) => void;
  getTransform: () => { x: number; y: number; k: number };
  getViewportSize: () => { width: number; height: number };
  getNodeById: (id: string) => RendererNode | undefined;
  getEdgeById: (id: string) => RendererEdge | undefined;
  getNeighbors: (id: string) => string[];
  getAllNodes: () => RendererNode[];
  getAllEdges: () => RendererEdge[];
  exportPNG: (options?: { background?: string; scale?: number }) => Promise<Blob>;
  exportSVG: (options?: { background?: string }) => Promise<string>;
  destroy: () => void;
};

export type RendererOptions = {
  width?: number;
  height?: number;
  minZoom?: number;
  maxZoom?: number;
  initialZoom?: number;
  styleName?: string;
};
