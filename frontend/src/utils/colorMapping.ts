// 节点类型颜色映射
export const NODE_COLORS: Record<string, string> = {
  Document: '#2563eb',
  Chunk: '#f59e0b',
  Entity: '#06b6d4',
  CausalFactView: '#7c3aed',
  TemporalFactView: '#db2777',
  Group: '#64748b',
  Crop: '#2e7d32', // 深绿色 - 作物
  Disease: '#d32f2f', // 红色 - 病害
  Pest: '#f57c00', // 橙色 - 虫害
  Technology: '#1976d2', // 蓝色 - 技术
  Fertilizer: '#7b1fa2', // 紫色 - 肥料
  default: '#757575', // 灰色 - 默认
};

const FALLBACK_NODE_PALETTE = [
  '#4e79a7',
  '#f28e2b',
  '#e15759',
  '#76b7b2',
  '#59a14f',
  '#edc949',
  '#af7aa1',
  '#ff9da7',
  '#9c755f',
  '#bab0ab',
  '#86bc86',
  '#f1ce63',
];

const NORMALIZED_NODE_COLORS = Object.entries(NODE_COLORS).reduce<Record<string, string>>(
  (acc, [key, value]) => {
    acc[key.toLowerCase()] = value;
    return acc;
  },
  {}
);

function hashString(input: string): number {
  let hash = 0;
  for (let i = 0; i < input.length; i += 1) {
    hash = (hash * 31 + input.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function getFallbackColor(key: string) {
  if (!key) return NODE_COLORS.default;
  const index = hashString(key) % FALLBACK_NODE_PALETTE.length;
  return FALLBACK_NODE_PALETTE[index];
}

// 根据节点标签获取颜色
export function getNodeColor(labels: string[]): string {
  if (!labels || labels.length === 0) {
    return NODE_COLORS.default;
  }

  // 优先匹配第一个标签
  for (const label of labels) {
    const color = NODE_COLORS[label] || NORMALIZED_NODE_COLORS[label.toLowerCase()];
    if (color) {
      return color;
    }
  }

  return getFallbackColor(labels[0]);
}

// 关系类型颜色映射
export const EDGE_COLORS: Record<string, string> = {
  AFFECTED_BY: '#d32f2f', // 红色 - 受影响
  PREVENTS: '#2e7d32', // 绿色 - 预防
  REQUIRES: '#1976d2', // 蓝色 - 需要
  USES: '#7b1fa2', // 紫色 - 使用
  HAS_CHUNK: '#2563eb',
  MENTIONS: '#f2b705',
  RELATED_TO: '#64748b',
  FACT_SOURCE: '#0ea5e9',
  FACT_TARGET: '#f59e0b',
  SAME_AS: '#8b5cf6',
  default: '#9e9e9e', // 灰色 - 默认
};

const FALLBACK_EDGE_PALETTE = [
  '#2563eb',
  '#16a34a',
  '#dc2626',
  '#7c3aed',
  '#0891b2',
  '#ca8a04',
  '#db2777',
  '#475569',
];

// 根据关系类型获取颜色
export function getEdgeColor(type: string): string {
  if (!type) return EDGE_COLORS.default;
  return EDGE_COLORS[type] || FALLBACK_EDGE_PALETTE[hashString(type) % FALLBACK_EDGE_PALETTE.length];
}

// 节点类型显示名称映射
export const NODE_LABELS: Record<string, string> = {
  Document: '文档',
  Chunk: '片段',
  Entity: '实体',
  CausalFactView: '因果事实',
  TemporalFactView: '时序事实',
  Group: '分组',
  Crop: '作物',
  Disease: '病害',
  Pest: '虫害',
  Technology: '技术',
  Fertilizer: '肥料',
};

const NORMALIZED_NODE_LABELS = Object.entries(NODE_LABELS).reduce<Record<string, string>>(
  (acc, [key, value]) => {
    acc[key.toLowerCase()] = value;
    return acc;
  },
  {}
);

// 获取节点类型显示名称
export function getNodeLabel(labels: string[]): string {
  if (!labels || labels.length === 0) {
    return '未知';
  }

  for (const label of labels) {
    const displayName = NODE_LABELS[label] || NORMALIZED_NODE_LABELS[label.toLowerCase()];
    if (displayName) {
      return displayName;
    }
  }

  return labels[0];
}
