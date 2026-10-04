// Cytoscape.js 样式配置
type CytoscapeStyleValue = string | number;
type CytoscapeStyleRule = {
  selector: string;
  style: Record<string, CytoscapeStyleValue>;
};

type CytoscapeLayoutNode = {
  degree: () => number;
};

type CytoscapePosition = {
  x: number;
  y: number;
};

export function getCytoscapeStyles(isDarkMode: boolean): CytoscapeStyleRule[] {
  const textColor = isDarkMode ? '#ffffff' : '#000000';
  const backgroundColor = isDarkMode ? '#1e1e1e' : '#f5f5f5';

  return [
    // 节点默认样式
    {
      selector: 'node',
      style: {
        'background-color': 'data(color)',
        'background-image': 'data(image)',
        'background-fit': 'cover',
        'background-clip': 'none',
        label: 'data(label)',
        color: '#ffffff',
        'text-valign': 'center',
        'text-halign': 'center',
        'font-size': '12px',
        'font-weight': 'normal',
        width: 60,
        height: 60,
        'border-width': 0, // Neo4j 风格无边框
        'text-outline-width': 0, // Neo4j 风格无文字描边
        shape: 'ellipse',
      },
    },

    // 有图片的节点样式
    {
      selector: 'node[image]',
      style: {
        'background-image': 'data(image)',
        'background-fit': 'cover',
        'background-clip': 'none',
        'background-color': 'transparent',
        width: 80,
        height: 80,
        'border-width': 0, // Neo4j 风格无边框
        'text-valign': 'center', // 图片节点标签也居中
        'text-halign': 'center',
        'text-outline-width': 0, // Neo4j 风格无文字描边
      },
    },

    // 视频节点样式
    {
      selector: 'node[mediaType="video"]',
      style: {
        'background-image': 'data(image)',
        'background-fit': 'cover',
        'background-clip': 'none',
        'background-color': '#1976d2',
        width: 90,
        height: 90,
        'border-width': 4,
        'border-color': '#1976d2',
        'border-opacity': 1,
        // 添加半透明叠加层表示这是视频
        'overlay-opacity': 0.2,
        'overlay-color': '#1976d2',
      },
    },

    // 有视频的节点（包括混合媒体）
    {
      selector: 'node[isVideo]',
      style: {
        // 添加视频指示器
        'overlay-opacity': 0.15,
        'overlay-color': '#1976d2',
      },
    },

    // 音频节点样式
    {
      selector: 'node[mediaType="audio"]',
      style: {
        'background-color': '#f57c00',
        width: 70,
        height: 70,
        'border-width': 3,
        'border-color': '#ff9800',
        'border-opacity': 1,
        shape: 'round-rectangle',
      },
    },

    // 混合媒体节点样式（既有图片又有视频/音频）
    {
      selector: 'node[mediaType="mixed"]',
      style: {
        'background-image': 'data(image)',
        'background-fit': 'cover',
        'background-clip': 'none',
        'background-color': 'transparent',
        width: 85,
        height: 85,
        'border-width': 4,
        'border-color': '#9c27b0',
        'border-opacity': 1,
      },
    },

    // 节点悬停样式（移除:hover伪选择器，改用事件处理）

    // 节点选中样式
    {
      selector: 'node:selected',
      style: {
        'border-width': 4,
        'border-color': '#ff4081',
        'background-color': 'data(color)',
        'z-index': 999,
      },
    },

    // 边默认样式
    {
      selector: 'edge',
      style: {
        width: 2,
        'line-color': 'data(color)',
        'target-arrow-color': 'data(color)',
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        label: 'data(label)',
        'font-size': '10px',
        color: textColor,
        'text-rotation': 'autorotate',
        'text-background-color': backgroundColor,
        'text-background-opacity': 0.8,
        'text-background-padding': '3px',
        'text-outline-color': backgroundColor,
        'text-outline-width': 1,
      },
    },

    // 边悬停样式（移除:hover伪选择器，改用事件处理）

    // 边选中样式
    {
      selector: 'edge:selected',
      style: {
        width: 4,
        'line-color': '#ff4081',
        'target-arrow-color': '#ff4081',
      },
    },

    // 高亮样式（用于路径高亮等）
    {
      selector: '.highlighted',
      style: {
        'background-color': '#ffd700',
        'line-color': '#ffd700',
        'target-arrow-color': '#ffd700',
        'border-color': '#ffd700',
        'border-width': 4,
      },
    },

    // 路径节点高亮样式
    {
      selector: '.path-node',
      style: {
        'border-width': 4,
        'border-color': '#ff6b6b',
        'border-opacity': 1,
        'z-index': 999,
      },
    },

    // 路径边高亮样式
    {
      selector: '.path-edge',
      style: {
        width: 4,
        'line-color': '#ff6b6b',
        'target-arrow-color': '#ff6b6b',
        'z-index': 999,
      },
    },

    // 隐藏样式（用于过滤）
    {
      selector: '.hidden',
      style: {
        display: 'none',
      },
    },
  ];
}

// Cytoscape.js 布局配置
export const LAYOUT_CONFIGS = {
  // 力导向布局（默认）
  cose: {
    name: 'cose',
    animate: true,
    animationDuration: 600,
    animationEasing: 'ease-out',
    nodeRepulsion: 10000,
    idealEdgeLength: 130,
    edgeElasticity: 120,
    nestingFactor: 5,
    gravity: 70,
    numIter: 1200,
    initialTemp: 200,
    coolingFactor: 0.95,
    minTemp: 1.0,
  },

  // 快速力导向布局（使用cose算法优化参数）
  fcose: {
    name: 'cose', // 使用cose算法但优化参数
    animate: true,
    animationDuration: 300, // 更快的动画
    animationEasing: 'ease-out',
    nodeRepulsion: 6000, // 中等排斥力
    idealEdgeLength: 80,
    edgeElasticity: 150,
    nestingFactor: 3,
    gravity: 100,
    numIter: 800, // 更少的迭代次数，更快
    initialTemp: 150,
    coolingFactor: 0.96,
    minTemp: 1.0,
  },

  // 紧凑力导向布局（新增）
  'cose-compact': {
    name: 'cose',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    nodeRepulsion: 4000,
    idealEdgeLength: 60,
    edgeElasticity: 200,
    nestingFactor: 1,
    gravity: 120,
    numIter: 1500,
    initialTemp: 100,
    coolingFactor: 0.98,
    minTemp: 1.0,
  },

  // 松散力导向布局（新增）
  'cose-loose': {
    name: 'cose',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    nodeRepulsion: 15000,
    idealEdgeLength: 150,
    edgeElasticity: 50,
    nestingFactor: 10,
    gravity: 40,
    numIter: 800,
    initialTemp: 300,
    coolingFactor: 0.92,
    minTemp: 1.0,
  },

  // 圆形布局
  circle: {
    name: 'circle',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    radius: 200,
    startAngle: (3 / 2) * Math.PI,
    sweep: 2 * Math.PI,
    clockwise: true,
  },

  // 网格布局
  grid: {
    name: 'grid',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    rows: undefined,
    cols: undefined,
    position: () => ({}),
  },

  // 同心圆布局
  concentric: {
    name: 'concentric',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    minNodeSpacing: 50,
    concentric: (node: CytoscapeLayoutNode) => node.degree(),
    levelWidth: () => 2,
  },

  // 层次布局（面包屑）
  breadthfirst: {
    name: 'breadthfirst',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    directed: true,
    spacingFactor: 1.5,
  },

  // 垂直层次布局（新增）
  'breadthfirst-vertical': {
    name: 'breadthfirst',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    directed: true,
    spacingFactor: 2.0,
    circle: false,
    grid: false,
    avoidOverlap: true,
    roots: undefined, // 可以指定根节点
  },

  // 水平层次布局（新增）
  'breadthfirst-horizontal': {
    name: 'breadthfirst',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    directed: true,
    spacingFactor: 1.8,
    circle: false,
    grid: false,
    avoidOverlap: true,
    transform: (_node: CytoscapeLayoutNode, position: CytoscapePosition) => {
      // 交换x和y坐标实现水平布局
      return { x: position.y, y: position.x };
    },
  },

  // 随机布局
  random: {
    name: 'random',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    fit: true,
  },

  // 预设布局
  preset: {
    name: 'preset',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    fit: true,
  },

  // Null布局（保持当前位置）
  null: {
    name: 'null',
    animate: false,
  },

  // 大圆形布局（新增）
  'circle-large': {
    name: 'circle',
    animate: true,
    animationDuration: 500,
    animationEasing: 'ease-out',
    radius: 300,
    startAngle: 0,
    sweep: 2 * Math.PI,
    clockwise: true,
  },

  // 螺旋布局（新增）
  'circle-spiral': {
    name: 'circle',
    animate: true,
    animationDuration: 800,
    animationEasing: 'ease-out',
    radius: 150,
    startAngle: 0,
    sweep: 4 * Math.PI, // 两圈螺旋
    clockwise: true,
  },
};

// 布局类型
export type LayoutType = keyof typeof LAYOUT_CONFIGS;

// 布局显示名称
export const LAYOUT_NAMES: Record<LayoutType, string> = {
  cose: '力导向',
  fcose: '快速力导向',
  'cose-compact': '紧凑力导向',
  'cose-loose': '松散力导向',
  circle: '圆形',
  'circle-large': '大圆形',
  'circle-spiral': '螺旋',
  grid: '网格',
  concentric: '同心圆',
  breadthfirst: '层次',
  'breadthfirst-vertical': '垂直层次',
  'breadthfirst-horizontal': '水平层次',
  random: '随机',
  preset: '预设',
  null: '固定',
};
