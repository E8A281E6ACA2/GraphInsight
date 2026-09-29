/**
 * 社区检测算法
 * 实现简单但有效的社区检测方法
 */

import type { Node, Edge } from '../store/graphStore';

export interface Community {
  id: string;
  name: string;
  nodeIds: string[];
  color: string;
  stats: CommunityStats;
}

export interface CommunityStats {
  nodeCount: number;
  internalEdges: number;
  externalEdges: number;
  density: number;
  modularity: number;
}

export interface DetectionResult {
  communities: Community[];
  totalModularity: number;
  algorithm: string;
  stats: {
    communityCount: number;
    averageSize: number;
    largestSize: number;
    smallestSize: number;
  };
}

// 预定义颜色方案
const COMMUNITY_COLORS = [
  '#1976d2', '#d32f2f', '#388e3c', '#f57c00',
  '#7b1fa2', '#00796b', '#c2185b', '#5d4037',
  '#455a64', '#e64a19', '#303f9f', '#689f38',
  '#8bc34a', '#ff9800', '#9c27b0', '#607d8b',
  '#795548', '#ff5722', '#3f51b5', '#4caf50',
];

/**
 * 基于连通分量的社区检测
 */
export function detectCommunitiesByConnectivity(
  nodes: Node[],
  edges: Edge[]
): DetectionResult {
  console.log('开始连通性社区检测...');
  
  // 构建邻接表
  const adjacencyList = buildAdjacencyList(nodes, edges);
  
  // 找出连通分量
  const components = findConnectedComponents(nodes, adjacencyList);
  
  // 对大的连通分量进行进一步细分
  const communities: Community[] = [];
  let colorIndex = 0;
  
  components.forEach((component, index) => {
    if (component.length <= 3) {
      // 小组件直接作为一个社区
      communities.push({
        id: `community_${index}`,
        name: `社区 ${index + 1}`,
        nodeIds: component,
        color: COMMUNITY_COLORS[colorIndex % COMMUNITY_COLORS.length],
        stats: calculateCommunityStats(component, edges),
      });
      colorIndex++;
    } else {
      // 大组件进行密度聚类
      const subCommunities = clusterByDensity(component, adjacencyList, edges);
      subCommunities.forEach((subCommunity, subIndex) => {
        communities.push({
          id: `community_${index}_${subIndex}`,
          name: `社区 ${communities.length + 1}`,
          nodeIds: subCommunity,
          color: COMMUNITY_COLORS[colorIndex % COMMUNITY_COLORS.length],
          stats: calculateCommunityStats(subCommunity, edges),
        });
        colorIndex++;
      });
    }
  });
  
  const totalModularity = calculateTotalModularity(communities, edges);
  
  console.log(`检测完成，发现 ${communities.length} 个社区，模块度: ${totalModularity.toFixed(3)}`);
  
  return {
    communities,
    totalModularity,
    algorithm: 'connectivity',
    stats: {
      communityCount: communities.length,
      averageSize: communities.reduce((sum, c) => sum + c.nodeIds.length, 0) / communities.length,
      largestSize: Math.max(...communities.map(c => c.nodeIds.length)),
      smallestSize: Math.min(...communities.map(c => c.nodeIds.length)),
    },
  };
}

/**
 * 基于节点度的社区检测
 */
export function detectCommunitiesByDegree(
  nodes: Node[],
  edges: Edge[]
): DetectionResult {
  console.log('开始度中心性社区检测...');
  
  const adjacencyList = buildAdjacencyList(nodes, edges);
  const communities: Community[] = [];
  const visited = new Set<string>();
  let colorIndex = 0;
  
  // 按度数排序节点
  const nodesByDegree = nodes
    .map(node => ({
      ...node,
      degree: adjacencyList.get(node.id)?.size || 0,
    }))
    .sort((a, b) => b.degree - a.degree);
  
  // 以高度节点为中心构建社区
  for (const centerNode of nodesByDegree) {
    if (visited.has(centerNode.id)) continue;
    
    const community = [centerNode.id];
    visited.add(centerNode.id);
    
    // 添加邻居节点
    const neighbors = adjacencyList.get(centerNode.id) || new Set();
    for (const neighborId of neighbors) {
      if (!visited.has(neighborId)) {
        community.push(neighborId);
        visited.add(neighborId);
      }
    }
    
    if (community.length >= 2) {
      communities.push({
        id: `degree_community_${communities.length}`,
        name: `度中心社区 ${communities.length + 1}`,
        nodeIds: community,
        color: COMMUNITY_COLORS[colorIndex % COMMUNITY_COLORS.length],
        stats: calculateCommunityStats(community, edges),
      });
      colorIndex++;
    }
  }
  
  // 处理孤立节点
  const isolatedNodes = nodes
    .filter(node => !visited.has(node.id))
    .map(node => node.id);
  
  if (isolatedNodes.length > 0) {
    communities.push({
      id: 'isolated_nodes',
      name: '孤立节点',
      nodeIds: isolatedNodes,
      color: COMMUNITY_COLORS[colorIndex % COMMUNITY_COLORS.length],
      stats: calculateCommunityStats(isolatedNodes, edges),
    });
  }
  
  const totalModularity = calculateTotalModularity(communities, edges);
  
  console.log(`检测完成，发现 ${communities.length} 个社区，模块度: ${totalModularity.toFixed(3)}`);
  
  return {
    communities,
    totalModularity,
    algorithm: 'degree',
    stats: {
      communityCount: communities.length,
      averageSize: communities.reduce((sum, c) => sum + c.nodeIds.length, 0) / communities.length,
      largestSize: Math.max(...communities.map(c => c.nodeIds.length)),
      smallestSize: Math.min(...communities.map(c => c.nodeIds.length)),
    },
  };
}

/**
 * 构建邻接表
 */
function buildAdjacencyList(nodes: Node[], edges: Edge[]): Map<string, Set<string>> {
  const adjacencyList = new Map<string, Set<string>>();
  
  // 初始化所有节点
  nodes.forEach(node => {
    adjacencyList.set(node.id, new Set());
  });
  
  // 添加边
  edges.forEach(edge => {
    const sourceSet = adjacencyList.get(edge.source);
    const targetSet = adjacencyList.get(edge.target);
    
    if (sourceSet && targetSet) {
      sourceSet.add(edge.target);
      targetSet.add(edge.source); // 无向图
    }
  });
  
  return adjacencyList;
}

/**
 * 找出连通分量
 */
function findConnectedComponents(
  nodes: Node[],
  adjacencyList: Map<string, Set<string>>
): string[][] {
  const visited = new Set<string>();
  const components: string[][] = [];
  
  function dfs(nodeId: string, component: string[]) {
    visited.add(nodeId);
    component.push(nodeId);
    
    const neighbors = adjacencyList.get(nodeId) || new Set();
    for (const neighborId of neighbors) {
      if (!visited.has(neighborId)) {
        dfs(neighborId, component);
      }
    }
  }
  
  nodes.forEach(node => {
    if (!visited.has(node.id)) {
      const component: string[] = [];
      dfs(node.id, component);
      components.push(component);
    }
  });
  
  return components;
}

/**
 * 基于密度的聚类
 */
function clusterByDensity(
  nodeIds: string[],
  adjacencyList: Map<string, Set<string>>,
  edges: Edge[]
): string[][] {
  if (nodeIds.length <= 5) {
    return [nodeIds]; // 小组件不再细分
  }
  
  // 计算子图的边密度
  const subgraphEdges = edges.filter(edge => 
    nodeIds.includes(edge.source) && nodeIds.includes(edge.target)
  );
  
  const maxPossibleEdges = (nodeIds.length * (nodeIds.length - 1)) / 2;
  const density = subgraphEdges.length / maxPossibleEdges;
  
  if (density > 0.3) {
    return [nodeIds]; // 密度高，不细分
  }
  
  // 密度低，尝试细分
  // 简单的启发式：按度数分组
  const nodesByDegree = nodeIds.map(nodeId => ({
    id: nodeId,
    degree: adjacencyList.get(nodeId)?.size || 0,
  }));
  
  const midpoint = Math.floor(nodesByDegree.length / 2);
  nodesByDegree.sort((a, b) => b.degree - a.degree);
  
  const highDegreeNodes = nodesByDegree.slice(0, midpoint).map(n => n.id);
  const lowDegreeNodes = nodesByDegree.slice(midpoint).map(n => n.id);
  
  return [highDegreeNodes, lowDegreeNodes].filter(group => group.length > 0);
}

/**
 * 计算社区统计信息
 */
function calculateCommunityStats(
  nodeIds: string[],
  edges: Edge[]
): CommunityStats {
  const nodeSet = new Set(nodeIds);
  
  let internalEdges = 0;
  let externalEdges = 0;
  
  edges.forEach(edge => {
    const sourceInCommunity = nodeSet.has(edge.source);
    const targetInCommunity = nodeSet.has(edge.target);
    
    if (sourceInCommunity && targetInCommunity) {
      internalEdges++;
    } else if (sourceInCommunity || targetInCommunity) {
      externalEdges++;
    }
  });
  
  const maxPossibleEdges = (nodeIds.length * (nodeIds.length - 1)) / 2;
  const density = maxPossibleEdges > 0 ? internalEdges / maxPossibleEdges : 0;
  
  return {
    nodeCount: nodeIds.length,
    internalEdges,
    externalEdges,
    density,
    modularity: 0, // 将在总体计算中设置
  };
}

/**
 * 计算总模块度
 */
function calculateTotalModularity(communities: Community[], edges: Edge[]): number {
  const totalEdges = edges.length;
  if (totalEdges === 0) return 0;
  
  let modularity = 0;
  
  communities.forEach(community => {
    const nodeSet = new Set(community.nodeIds);
    
    // 计算社区内边数
    const internalEdges = edges.filter(edge => 
      nodeSet.has(edge.source) && nodeSet.has(edge.target)
    ).length;
    
    // 计算社区的度数总和
    const communityDegree = edges.filter(edge => 
      nodeSet.has(edge.source) || nodeSet.has(edge.target)
    ).length;
    
    // 模块度贡献
    const expectedEdges = (communityDegree * communityDegree) / (4 * totalEdges);
    modularity += (internalEdges / totalEdges) - expectedEdges / totalEdges;
    
    // 更新社区的模块度
    community.stats.modularity = (internalEdges / totalEdges) - expectedEdges / totalEdges;
  });
  
  return modularity;
}

/**
 * 推荐最佳社区检测算法
 */
export function recommendDetectionAlgorithm(nodes: Node[], edges: Edge[]): string {
  const nodeCount = nodes.length;
  const edgeCount = edges.length;
  const density = edgeCount / (nodeCount * (nodeCount - 1) / 2);
  
  if (nodeCount <= 20) {
    return 'connectivity'; // 小图用连通性
  } else if (density > 0.1) {
    return 'degree'; // 密集图用度中心性
  } else {
    return 'connectivity'; // 稀疏图用连通性
  }
}