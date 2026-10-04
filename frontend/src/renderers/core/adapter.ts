import type { GraphData } from '../../store/graphStore';
import { getNodeColor, getEdgeColor } from '../../utils/colorMapping';
import { generateNodeLabel } from '../../utils/graphDataConverter';
import { buildProxyMediaUrl } from '../../utils/apiBase';
import type { RendererCluster, RendererData, RendererEdge, RendererNode } from './types';

const DEFAULT_RADIUS = 24;

export function adaptGraphData(
  graphData: GraphData | null,
  nodeTypeStyles?: Record<string, { color?: string; size?: number; caption?: string[] }>
): RendererData {
  if (!graphData) {
    return emptyRendererData();
  }

  const nodes: RendererNode[] = graphData.nodes.map((node) => {
    const type = node.labels[0] || 'Unknown';
    const label = generateNodeLabel(node, nodeTypeStyles?.[type]);
    const color = nodeTypeStyles?.[type]?.color || getNodeColor(node.labels);

    const media = extractMedia(node.properties);

    return {
      id: node.id,
      label,
      color,
      radius: nodeTypeStyles?.[type]?.size ? nodeTypeStyles[type].size / 2 : DEFAULT_RADIUS,
      type,
      properties: node.properties,
      neighbors: [],
      degree: 0,
      indegree: 0,
      outdegree: 0,
      mediaType: media.mediaType,
      image: media.image,
      video: media.video,
      audio: media.audio,
      isVideo: media.isVideo,
      videoThumbnailUrl: media.videoThumbnailUrl,
      originalVideoUrl: media.originalVideoUrl,
    };
  });

  const nodeById = new Map(nodes.map((n) => [n.id, n] as const));

  const edges: RendererEdge[] = graphData.edges.map((edge) => {
    const color = getEdgeColor(edge.type);
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      predicate: edge.type || '',
      color,
      type: edge.type,
      properties: edge.properties,
    };
  });

  edges.forEach((edge) => {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) return;

    source.outdegree += 1;
    target.indegree += 1;
    source.degree += 1;
    target.degree += 1;

    if (!source.neighbors.includes(target.id)) source.neighbors.push(target.id);
    if (!target.neighbors.includes(source.id)) target.neighbors.push(source.id);
  });

  const clusters: RendererCluster[] = [];
  const topEntities = [...nodes]
    .sort((a, b) => b.degree - a.degree)
    .slice(0, 10)
    .map((node) => ({ id: node.id, label: node.label, degree: node.degree, cluster: node.cluster }));

  const relationCounts = new Map<string, { count: number; color: string }>();
  edges.forEach((edge) => {
    const entry = relationCounts.get(edge.predicate) || { count: 0, color: edge.color };
    entry.count += 1;
    relationCounts.set(edge.predicate, entry);
  });
  const topRelations = [...relationCounts.entries()]
    .map(([predicate, entry]) => ({ predicate, count: entry.count, color: entry.color }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 10);

  const stats = {
    entities: nodes.length,
    relations: edges.length,
    relationTypes: relationCounts.size,
    entityClusters: clusters.length,
    edgeClusters: 0,
    isolatedEntities: nodes.filter((n) => n.degree === 0).length,
    components: 1,
    averageDegree: nodes.length ? edges.length * 2 / nodes.length : 0,
  };

  return {
    nodes,
    edges,
    clusters,
    topEntities,
    topRelations,
    stats,
  };
}

function extractMedia(
  properties: Record<string, unknown>
): {
  image?: string;
  video?: string;
  audio?: string;
  mediaType?: RendererNode['mediaType'];
  isVideo: boolean;
  originalVideoUrl?: string;
  videoThumbnailUrl?: string;
} {
  let imageUrl: string | null = null;
  const imageKeys = ['images', 'image', 'imageUrl', '图片', '图像', '照片'];
  for (const key of imageKeys) {
    const value = properties[key];
    if (typeof value === 'string') {
      imageUrl = value;
      break;
    }
    if (Array.isArray(value) && value.length > 0) {
      imageUrl = value[0];
      break;
    }
  }

  const proxiedImage = imageUrl
    ? (imageUrl.startsWith('http://') || imageUrl.startsWith('https://'))
      ? buildProxyMediaUrl(imageUrl)
      : imageUrl
    : undefined;

  let videoUrl: string | null = null;
  const videoKeys = ['videos', 'video', 'videoUrl', '视频', '影片'];
  for (const key of videoKeys) {
    const value = properties[key];
    if (typeof value === 'string') {
      videoUrl = value;
      break;
    }
    if (Array.isArray(value) && value.length > 0) {
      videoUrl = value[0];
      break;
    }
  }

  if (!videoUrl) {
    for (const [key, value] of Object.entries(properties)) {
      if ((key.includes('视频') || key.toLowerCase().includes('video')) && typeof value === 'string') {
        if (value.startsWith('http') || value.includes('.mp4') || value.includes('.webm')) {
          videoUrl = value;
          break;
        }
      }
    }
  }

  const proxiedVideo = videoUrl
    ? (videoUrl.startsWith('http://') || videoUrl.startsWith('https://'))
      ? buildProxyMediaUrl(videoUrl)
      : videoUrl
    : undefined;

  let audioUrl: string | null = null;
  const audioKeys = ['audios', 'audio', 'audioUrl', '音频', '音乐', '声音'];
  for (const key of audioKeys) {
    const value = properties[key];
    if (typeof value === 'string') {
      audioUrl = value;
      break;
    }
    if (Array.isArray(value) && value.length > 0) {
      audioUrl = value[0];
      break;
    }
  }

  const proxiedAudio = audioUrl
    ? (audioUrl.startsWith('http://') || audioUrl.startsWith('https://'))
      ? buildProxyMediaUrl(audioUrl)
      : audioUrl
    : undefined;

  const mediaType: RendererNode['mediaType'] = proxiedImage && (proxiedVideo || proxiedAudio)
    ? 'mixed'
    : proxiedVideo
      ? 'video'
      : proxiedAudio
        ? 'audio'
        : proxiedImage
          ? 'image'
          : undefined;

  return {
    image: proxiedImage,
    video: proxiedVideo,
    audio: proxiedAudio,
    mediaType,
    isVideo: Boolean(proxiedVideo),
    originalVideoUrl: videoUrl || undefined,
    videoThumbnailUrl: undefined as string | undefined,
  };
}

function emptyRendererData(): RendererData {
  return {
    nodes: [],
    edges: [],
    clusters: [],
    topEntities: [],
    topRelations: [],
    stats: {
      entities: 0,
      relations: 0,
      relationTypes: 0,
      entityClusters: 0,
      edgeClusters: 0,
      isolatedEntities: 0,
      components: 0,
      averageDegree: 0,
    },
  };
}
