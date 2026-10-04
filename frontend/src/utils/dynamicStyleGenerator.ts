/**
 * 动态样式生成器
 * 根据用户配置生成 Cytoscape 样式
 */

import type { NodeTypeStyle } from './graphDataConverter';

type CytoscapeStyleValue = string | number;
type CytoscapeStyleMap = Record<string, CytoscapeStyleValue>;
type CytoscapeStyleRule = {
  selector: string;
  style: CytoscapeStyleMap;
};

type CytoscapeStyleCollection = {
  length: number;
  style: (styles: CytoscapeStyleMap) => void;
};

type CytoscapeStyleTarget = {
  elements: () => { length: number };
  nodes: (selector: string) => CytoscapeStyleCollection;
};

export function generateDynamicStylesByNodeType(
  nodeTypeStyles: Record<string, NodeTypeStyle>,
  isDarkMode: boolean
): CytoscapeStyleRule[] {
  console.log('🎨 Generating dynamic styles by node type:', nodeTypeStyles);
  
  const backgroundColor = isDarkMode ? '#1e1e1e' : '#f5f5f5';

  const styles: CytoscapeStyleRule[] = [];

  // 默认节点样式 (精致风格)
  const defaultNodeStyle = {
    selector: 'node',
    style: {
      'background-color': '#60A5FA',
      label: 'data(label)',
      color: '#0f172a',
      'text-valign': 'center',
      'text-halign': 'center',
      'font-size': '12px',
      'font-weight': '500',
      'font-family': '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      width: 52,
      height: 52,
      'border-width': 1,
      'border-color': 'rgba(15, 23, 42, 0.12)',
      'text-outline-width': 0,
      'text-background-opacity': 0,
      shape: 'ellipse',
      'overlay-opacity': 0,
      'shadow-blur': 6,
      'shadow-color': 'rgba(15, 23, 42, 0.18)',
      'shadow-opacity': 0.35,
      'shadow-offset-x': 0,
      'shadow-offset-y': 2,
    },
  };
  styles.push(defaultNodeStyle);

  // 有图片的节点样式 - 默认显示图片，优先级高于默认样式
  styles.push({
    selector: 'node[image]',
    style: {
      'background-image': 'data(image)',
      'background-fit': 'cover',
      'background-clip': 'none',
      'text-valign': 'center', // 图片节点标签也居中
      'text-halign': 'center',
      'text-margin-y': 0,
      'border-width': 1,
      'border-color': 'rgba(15, 23, 42, 0.12)',
      'text-outline-width': 0,
      'color': '#ffffff',
      width: 82,
      height: 82,
      'shadow-blur': 8,
      'shadow-color': 'rgba(15, 23, 42, 0.2)',
      'shadow-opacity': 0.35,
      'shadow-offset-x': 0,
      'shadow-offset-y': 2,
    },
  });

  // 为每个节点类型生成特定样式
  Object.entries(nodeTypeStyles).forEach(([nodeType, config]) => {
    // 使用 data() 函数来匹配节点类型
    const nodeTypeSelector = `node[type="${nodeType}"]`;
    
    const nodeTypeStyle: CytoscapeStyleRule = {
      selector: nodeTypeSelector,
      style: {
        'background-color': config.color ?? '#60A5FA',
        label: config.showLabels ? 'data(label)' : '',
        color: '#0f172a',
        'text-valign': 'center', // 始终居中
        'text-halign': 'center', // 始终居中
        'text-margin-y': 0,
        'font-size': `${config.labelSize ?? 12}px`,
        'font-weight': '500',
        'font-family': '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        width: config.size ?? 52,
        height: config.size ?? 52,
        'border-width': 1,
        'border-color': 'rgba(15, 23, 42, 0.12)',
        'text-outline-width': 0,
        'text-background-opacity': 0,
        shape: 'ellipse',
        'overlay-opacity': 0,
        'shadow-blur': 6,
        'shadow-color': 'rgba(15, 23, 42, 0.18)',
        'shadow-opacity': 0.35,
        'shadow-offset-x': 0,
        'shadow-offset-y': 2,
      },
    };
    
    // 图片显示逻辑：默认显示图片，除非明确设置为false
    if (config.showImages !== false) {
      nodeTypeStyle.style['background-image'] = 'data(image)';
      nodeTypeStyle.style['background-fit'] = 'cover';
      nodeTypeStyle.style['background-clip'] = 'none';
    }
    
    styles.push(nodeTypeStyle);
    
    console.log('🎨 Added style for node type:', nodeType, 'selector:', nodeTypeSelector, 'showImages:', config.showImages);
  });



  // 视频节点样式
  styles.push({
    selector: 'node[mediaType="video"]',
    style: {
      'overlay-opacity': 0.2,
      'overlay-color': '#1976d2',
    },
  });

  // 有视频的节点（包括混合媒体）
  styles.push({
    selector: 'node[isVideo]',
    style: {
      'overlay-opacity': 0.15,
      'overlay-color': '#1976d2',
    },
  });

  // 音频节点样式
  styles.push({
    selector: 'node[mediaType="audio"]',
    style: {
      shape: 'round-rectangle',
    },
  });

  // 节点选中样式
  styles.push({
    selector: 'node:selected',
    style: {
      'border-width': 3,
      'border-color': '#ff4081',
      'border-opacity': 0.8,
      'z-index': 999,
    },
  });

  // 边样式 (精致风格)
  styles.push({
    selector: 'edge',
    style: {
      width: 1.2,
      'line-color': 'rgba(15, 23, 42, 0.28)',
      'target-arrow-color': 'rgba(15, 23, 42, 0.28)',
      'target-arrow-shape': 'triangle-backcurve',
      'target-arrow-size': 7,
      'curve-style': 'bezier',
      label: 'data(label)',
      'font-size': '10px',
      'font-family': '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      color: '#475569',
      'text-rotation': 'autorotate',
      'text-background-color': backgroundColor,
      'text-background-opacity': 0.7,
      'text-background-padding': '2px',
      'text-outline-width': 0,
    },
  });

  // 低缩放时隐藏边标签
  styles.push({
    selector: 'edge.zoom-label-hidden',
    style: {
      label: '',
      'text-background-opacity': 0,
    },
  });

  // 边选中样式
  styles.push({
    selector: 'edge:selected',
    style: {
      width: 4,
      'line-color': '#ff4081',
      'target-arrow-color': '#ff4081',
    },
  });

  // 高亮样式
  styles.push({
    selector: '.highlighted',
    style: {
      'background-color': '#ffd700',
      'line-color': '#ffd700',
      'target-arrow-color': '#ffd700',
      'border-color': '#ffd700',
      'border-width': 4,
    },
  });

  // 路径节点高亮样式
  styles.push({
    selector: '.path-node',
    style: {
      'border-width': 4,
      'border-color': '#ff6b6b',
      'border-opacity': 1,
      'z-index': 999,
    },
  });

  // 路径边高亮样式
  styles.push({
    selector: '.path-edge',
    style: {
      width: 4,
      'line-color': '#ff6b6b',
      'target-arrow-color': '#ff6b6b',
      'z-index': 999,
    },
  });

  // 隐藏样式
  styles.push({
    selector: '.hidden',
    style: {
      display: 'none',
    },
  });

  // 复合节点（分组）样式
  styles.push({
    selector: 'node:parent',
    style: {
      'background-opacity': 0.2,
      'background-color': 'data(backgroundColor)',
      'border-width': 'data(borderWidth)',
      'border-color': 'data(borderColor)',
      'border-style': 'dashed',
      label: 'data(label)',
      'text-valign': 'top',
      'text-halign': 'center',
      'text-margin-y': 10,
      'font-size': '14px',
      'font-weight': 'bold',
      color: '#000000',
      'text-outline-width': 0,
      'text-background-opacity': 0.8,
      'text-background-color': '#ffffff',
      'text-background-padding': '4px',
      shape: 'round-rectangle',
      'padding': '20px',
    },
  });

  // 折叠的分组样式
  styles.push({
    selector: 'node:parent.collapsed',
    style: {
      'background-opacity': 0.8,
      'background-color': 'data(backgroundColor)',
      'border-style': 'solid',
      'border-width': 3,
      width: 80,
      height: 80,
      shape: 'round-rectangle',
      'text-valign': 'center',
      'text-halign': 'center',
      'text-margin-y': 0,
      'font-size': '12px',
      'padding': '10px',
    },
  });

  console.log('🎨 Generated styles:', styles);
  return styles;
}

/**
 * 应用样式到 Cytoscape 实例
 */
export function applyNodeTypeStylesToCytoscape(
  cy: CytoscapeStyleTarget,
  nodeTypeStyles: Record<string, NodeTypeStyle>
): void {
  console.log('🎨 Applying node type styles to cytoscape, elements count:', cy.elements().length);
  
  // 不重新设置整个样式表，而是逐个更新节点样式
  Object.entries(nodeTypeStyles).forEach(([nodeType, config]) => {
    const nodes = cy.nodes(`[type="${nodeType}"]`);
    console.log(`🎨 Updating ${nodes.length} nodes of type "${nodeType}"`);
    
    if (nodes.length > 0) {
      const styleUpdate: CytoscapeStyleMap = {
        'background-color': config.color ?? '#1976d2',
        'width': config.size ?? 60,
        'height': config.size ?? 60,
        'border-width': 0, // Neo4j 风格无边框
        'font-size': `${config.labelSize ?? 12}px`,
        'font-weight': 'normal',
        'font-family': '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        'label': config.showLabels ? 'data(label)' : '',
        'text-outline-width': 0, // Neo4j 风格无文字描边
        'text-background-opacity': 0,
        'color': '#ffffff', // 白色文字在彩色背景上更清晰
        'overlay-opacity': 0,
        'text-valign': 'center', // 始终保持标签垂直居中
        'text-halign': 'center', // 始终保持标签水平居中
        'text-margin-y': 0,
      };
      
      // 只有在配置中明确设置图片显示时才更新图片样式
      if (config.showImages !== undefined) {
        if (config.showImages) {
          styleUpdate['background-image'] = 'data(image)';
          styleUpdate['background-fit'] = 'cover';
          styleUpdate['background-clip'] = 'none';
        } else {
          styleUpdate['background-image'] = 'none';
        }
      }
      
      nodes.style(styleUpdate);
    }
  });
  
  console.log('🎨 Styles applied, elements still there:', cy.elements().length);
}

/**
 * 获取样式预览数据（用于实时预览）
 */
export function getNodeTypeStylePreview(nodeTypeStyle: NodeTypeStyle): {
  nodeSize: number;
  labelVisible: boolean;
  labelSize: number;
  borderWidth: number;
  color: string;
} {
  return {
    nodeSize: nodeTypeStyle.size || 60,
    labelVisible: nodeTypeStyle.showLabels !== false,
    labelSize: nodeTypeStyle.labelSize || 12,
    borderWidth: nodeTypeStyle.borderWidth || 2,
    color: nodeTypeStyle.color || '#1976d2',
  };
}
