import type { Edge, GraphData, Node, NodeGroup } from '../store/graphStore';
import { getNodeColor, getEdgeColor } from './colorMapping';
import { generateVideoThumbnail } from './videoThumbnail';
import { buildProxyMediaUrl } from './apiBase';

// Cytoscape 元素类型
export interface CytoscapeNode {
  data: {
    id: string;
    label: string;
    color: string;
    type: string;
    properties: Record<string, unknown>;
    // 媒体相关属性
    image?: string;
    video?: string;
    audio?: string;
    mediaType?: 'image' | 'video' | 'audio' | 'mixed';
    isVideo?: boolean;
    videoThumbnailUrl?: string;
    originalVideoUrl?: string;
    // 分组相关属性
    parent?: string;
    backgroundColor?: string;
    borderColor?: string;
    borderWidth?: number;
  };
  classes?: string;
}

export interface CytoscapeEdge {
  data: {
    id: string;
    source: string;
    target: string;
    label: string;
    color: string;
    type: string;
    properties: Record<string, unknown>;
  };
}

export type CytoscapeElement = CytoscapeNode | CytoscapeEdge;

export type NodeTypeStyle = {
  color?: string;
  size?: number;
  borderWidth?: number;
  showLabels?: boolean;
  labelSize?: number;
  showImages?: boolean;
  caption?: string[];
};

type CytoscapeNodeData = CytoscapeNode['data'];

type CytoscapeDataReader = {
  data: (key: string) => unknown;
};

// 将 API 格式的图数据转换为 Cytoscape 格式
export function convertToCytoscapeFormat(
  graphData: GraphData | null, 
  groups?: NodeGroup[],
  showGroupLabels?: boolean,
  nodeTypeStyles?: Record<string, NodeTypeStyle>
): CytoscapeElement[] {
  if (!graphData) {
    return [];
  }

  const elements: CytoscapeElement[] = [];

  // 转换节点
  graphData.nodes.forEach((node) => {
    const type = node.labels[0] || 'Unknown';
    const label = generateNodeLabel(node, nodeTypeStyles?.[type]);
    const color = getNodeColor(node.labels);
    
    // 提取图片信息（支持中英文属性名）
    let imageUrl: string | null = null;
    const imageKeys = ['images', 'image', 'imageUrl', '图片', '图像', '照片'];
    
    for (const key of imageKeys) {
      if (node.properties[key]) {
        const value = node.properties[key];
        if (typeof value === 'string') {
          imageUrl = value;
          break;
        } else if (Array.isArray(value) && value.length > 0) {
          imageUrl = value[0]; // 使用第一张图片
          break;
        }
      }
    }

    const nodeData: CytoscapeNodeData = {
      id: node.id,
      label,
      color,
      type,
      properties: node.properties,
    };

    // 媒体类型标记
    let hasImage = false;
    let hasVideo = false;
    let hasAudio = false;

    // 如果有图片，添加图片URL（通过代理避免CORS问题）
    if (imageUrl) {
      hasImage = true;
      // 如果是外部URL，使用代理
      if (imageUrl.startsWith('http://') || imageUrl.startsWith('https://')) {
        nodeData.image = buildProxyMediaUrl(imageUrl);
      } else {
        // 本地图片直接使用
        nodeData.image = imageUrl;
      }
    }

    // 提取视频信息（支持更灵活的属性名匹配）
    let videoUrl: string | null = null;
    
    // 首先检查常见的视频属性名
    const videoKeys = ['videos', 'video', 'videoUrl', '视频', '影片'];
    for (const key of videoKeys) {
      if (node.properties[key]) {
        const value = node.properties[key];
        if (typeof value === 'string') {
          videoUrl = value;
          break;
        } else if (Array.isArray(value) && value.length > 0) {
          videoUrl = value[0]; // 使用第一个视频
          break;
        }
      }
    }
    
    // 如果没找到，检查所有包含"视频"或"video"关键字的属性
    if (!videoUrl) {
      for (const [key, value] of Object.entries(node.properties)) {
        if ((key.includes('视频') || key.toLowerCase().includes('video')) && 
            typeof value === 'string' && 
            (value.startsWith('http') || value.includes('.mp4') || value.includes('.webm'))) {
          videoUrl = value;
          break;
        }
      }
    }
    
    if (videoUrl) {
      hasVideo = true;
      if (videoUrl.startsWith('http://') || videoUrl.startsWith('https://')) {
        nodeData.video = buildProxyMediaUrl(videoUrl);
      } else {
        nodeData.video = videoUrl;
      }
      
      // 如果没有图片，生成视频第一帧缩略图
      if (!hasImage) {
        // 先设置一个加载中的占位符
        nodeData.image = `data:image/svg+xml;base64,${btoa(`
          <svg xmlns="http://www.w3.org/2000/svg" width="90" height="90" viewBox="0 0 90 90">
            <rect width="90" height="90" fill="#1976d2" rx="8"/>
            <circle cx="45" cy="45" r="18" fill="rgba(255,255,255,0.9)"/>
            <polygon points="38,35 38,55 58,45" fill="#1976d2"/>
            <text x="45" y="78" text-anchor="middle" fill="white" font-size="8">LOADING...</text>
          </svg>
        `)}`;
        
        // 异步生成真实的视频缩略图
        generateVideoThumbnail(nodeData.video).then((thumbnailDataUrl) => {
          // 这里我们需要一个回调来更新节点
          nodeData.videoThumbnailUrl = thumbnailDataUrl;
        }).catch((error) => {
          console.warn('Failed to generate video thumbnail:', error);
        });
      }
      
      // 标记为视频节点，用于添加播放图标叠加
      nodeData.isVideo = true;
      // 保存原始视频URL用于播放
      nodeData.originalVideoUrl = videoUrl;
    }

    // 提取音频信息
    const audioKeys = ['audios', 'audio', 'audioUrl', '音频', '音乐', '声音'];
    for (const key of audioKeys) {
      if (node.properties[key]) {
        const value = node.properties[key];
        let audioUrl: string | null = null;
        if (typeof value === 'string') {
          audioUrl = value;
        } else if (Array.isArray(value) && value.length > 0) {
          audioUrl = value[0]; // 使用第一个音频
        }
        
        if (audioUrl) {
          hasAudio = true;
          if (audioUrl.startsWith('http://') || audioUrl.startsWith('https://')) {
            nodeData.audio = buildProxyMediaUrl(audioUrl);
          } else {
            nodeData.audio = audioUrl;
          }
          
          // 如果没有图片和视频，使用音频图标
          if (!hasImage && !hasVideo) {
            nodeData.image = 'data:image/svg+xml;base64,' + btoa(`
              <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 24 24" fill="white">
                <rect width="24" height="24" fill="#f57c00" rx="4"/>
                <path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" fill="white"/>
              </svg>
            `);
          }
          break;
        }
      }
    }

    // 设置媒体类型
    if (hasImage && (hasVideo || hasAudio)) {
      nodeData.mediaType = 'mixed';
    } else if (hasVideo) {
      nodeData.mediaType = 'video';
    } else if (hasAudio) {
      nodeData.mediaType = 'audio';
    } else if (hasImage) {
      nodeData.mediaType = 'image';
    }

    elements.push({
      data: nodeData,
    });
  });

  // 转换边
  graphData.edges.forEach((edge) => {
    const label = getEdgeLabel(edge);
    const color = getEdgeColor(edge.type);

    elements.push({
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label,
        color,
        type: edge.type,
        properties: edge.properties,
      },
    });
  });

  // 添加分组（复合节点）
  if (groups && groups.length > 0) {
    groups.forEach((group) => {
      // 只为非空分组创建复合节点
      if (group.nodeIds.length > 0) {
        const groupElement: CytoscapeNode = {
          data: {
            id: group.id,
            label: showGroupLabels ? group.name : '',
            color: group.color,
            type: 'group',
            properties: {},
            backgroundColor: group.style?.backgroundColor || (group.color + '20'),
            borderColor: group.style?.borderColor || group.color,
            borderWidth: group.style?.borderWidth || 2,
          },
        };

        // 如果分组是折叠的，添加折叠样式类
        if (group.collapsed) {
          groupElement.classes = 'collapsed';
        }

        elements.push(groupElement);

        // 为分组中的节点设置父节点
        group.nodeIds.forEach((nodeId) => {
          const nodeElement = elements.find(
            (el) => 'source' in el.data === false && el.data.id === nodeId
          ) as CytoscapeNode | undefined;
          
          if (nodeElement) {
            // 如果分组是折叠的，隐藏子节点
            if (group.collapsed) {
              nodeElement.classes = 'hidden';
            } else {
              // 设置父节点
              nodeElement.data.parent = group.id;
            }
          }
        });
      }
    });
  }

  return elements;
}

function getEdgeLabel(edge: Edge): string {
  const props = edge.properties || {};
  const candidates = [
    'label',
    'name',
    'relation',
    'rel',
    'predicate',
    'title',
    '关系',
    '关系名',
    '名称',
    '标题',
  ];
  for (const key of candidates) {
    const raw = props[key];
    if (raw === null || raw === undefined) continue;
    const text = String(raw).trim();
    if (text) return text;
  }
  return edge.type || '';
}

// 简单的节点标签生成 - 使用合理的默认逻辑
export function generateNodeLabel(node: Node, nodeTypeStyle?: NodeTypeStyle): string {
  const labels = Array.isArray(node.labels) ? node.labels : [];
  if (labels.includes('Chunk')) {
    const idx = node.properties?.index;
    if (Number.isFinite(idx)) {
      return `片段 ${Number(idx) + 1}`;
    }
    const text = node.properties?.text;
    if (text) {
      const trimmed = String(text).trim().replace(/\s+/g, ' ');
      return trimmed.length > 24 ? `${trimmed.substring(0, 24)}...` : trimmed;
    }
  }

  // 如果有样式配置且配置了caption属性，使用caption
  if (nodeTypeStyle?.caption && Array.isArray(nodeTypeStyle.caption) && nodeTypeStyle.caption.length > 0) {
    const captionParts: string[] = [];
    
    for (const prop of nodeTypeStyle.caption) {
      if (node.properties[prop]) {
        const value = String(node.properties[prop]).trim();
        if (value) {
          captionParts.push(value);
        }
      }
    }
    
    if (captionParts.length > 0) {
      const label = captionParts.join(' | ');
      // 基本的长度限制
      return label.length > 50 ? label.substring(0, 50) + '...' : label;
    }
  }
  
  // 回退到默认的优先级逻辑
  const candidates = ['name', 'title', '名称', '标题', 'label'];
  
  for (const prop of candidates) {
    if (node.properties[prop]) {
      const value = String(node.properties[prop]).trim();
      // 基本的长度限制
      return value.length > 30 ? value.substring(0, 30) + '...' : value;
    }
  }
  
  // 回退到节点 ID
  return node.id;
}

// 获取节点的显示名称
export function getNodeDisplayName(node: Node | CytoscapeDataReader): string {
  if ('data' in node && typeof node.data === 'function') {
    return String(node.data('label') || node.data('id') || 'Unknown');
  }
  return 'id' in node ? node.id : 'Unknown';
}

// 获取边的显示名称
export function getEdgeDisplayName(edge: Edge | CytoscapeDataReader): string {
  if ('data' in edge && typeof edge.data === 'function') {
    return String(edge.data('label') || edge.data('type') || '');
  }
  return 'type' in edge ? edge.type : '';
}
