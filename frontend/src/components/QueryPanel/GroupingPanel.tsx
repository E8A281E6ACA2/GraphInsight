import { useState } from 'react';
import {
  Box,
  Typography,
  Button,
  Switch,
  FormControlLabel,
  Divider,
  List,
  ListItem,
  ListItemText,
  ListItemSecondaryAction,
  IconButton,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Chip,
  Paper,
  Tooltip,
  Menu,
  MenuItem,
} from '@mui/material';
import {
  Add as AddIcon,
  Delete as DeleteIcon,
  Edit as EditIcon,
  Visibility as VisibilityIcon,
  VisibilityOff as VisibilityOffIcon,
  MoreVert as MoreVertIcon,
  Psychology as PsychologyIcon,
  Analytics as AnalyticsIcon,
  TrendingUp as TrendingUpIcon,
} from '@mui/icons-material';
import { useGraphStore } from '../../store/graphStore';
import type { NodeGroup } from '../../store/graphStore';
import {
  detectCommunitiesByConnectivity,
  detectCommunitiesByDegree,
  recommendDetectionAlgorithm,
  type DetectionResult
} from '../../utils/communityDetection';
import LoadingButton from '../Loading/LoadingButton';

interface GroupingPanelProps {
  onGroupingChange?: () => void;
}

export function GroupingPanel({ onGroupingChange }: GroupingPanelProps) {
  const {
    graphData,
    groupingState,
    setAutoGroupByType,
    setShowGroupLabels,
    createGroup,
    updateGroup,
    deleteGroup,
    toggleGroupCollapse,
    clearAllGroups,
  } = useGraphStore();

  // 对话框状态
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editingGroup, setEditingGroup] = useState<NodeGroup | null>(null);
  const [groupName, setGroupName] = useState('');
  const [groupColor, setGroupColor] = useState('#1976d2');
  const [selectedNodeTypes, setSelectedNodeTypes] = useState<string[]>([]);

  // 菜单状态
  const [menuAnchor, setMenuAnchor] = useState<null | HTMLElement>(null);
  const [menuGroupId, setMenuGroupId] = useState<string | null>(null);
  
  // 社区检测状态
  const [detectionResult, setDetectionResult] = useState<DetectionResult | null>(null);
  const [isDetecting, setIsDetecting] = useState(false);

  // 获取可用的节点类型
  const availableNodeTypes = graphData?.nodes
    ? [...new Set(graphData.nodes.map(node => node.labels[0] || 'Unknown'))]
    : [];

  // 预定义颜色
  const predefinedColors = [
    '#1976d2', '#d32f2f', '#388e3c', '#f57c00',
    '#7b1fa2', '#00796b', '#c2185b', '#5d4037',
    '#455a64', '#e64a19', '#303f9f', '#689f38',
  ];

  // 处理自动按类型分组
  const handleAutoGroupByType = (enabled: boolean) => {
    setAutoGroupByType(enabled);
    
    if (enabled && graphData?.nodes) {
      // 清除现有分组
      clearAllGroups();
      setDetectionResult(null);
      
      // 按节点类型自动创建分组
      const nodesByType: Record<string, string[]> = {};
      graphData.nodes.forEach(node => {
        const nodeType = node.labels[0] || 'Unknown';
        if (!nodesByType[nodeType]) {
          nodesByType[nodeType] = [];
        }
        nodesByType[nodeType].push(node.id);
      });

      // 为每个类型创建分组
      Object.entries(nodesByType).forEach(([nodeType, nodeIds], index) => {
        const color = predefinedColors[index % predefinedColors.length];
        createGroup(`${nodeType} 类型`, nodeIds, color);
      });

      onGroupingChange?.();
    } else if (!enabled) {
      // 清除所有分组
      clearAllGroups();
      setDetectionResult(null);
      onGroupingChange?.();
    }
  };

  // 智能社区检测
  const handleCommunityDetection = async (algorithm?: string) => {
    if (!graphData?.nodes || !graphData?.edges) return;
    
    setIsDetecting(true);
    
    try {
      // 推荐算法或使用指定算法
      const selectedAlgorithm = algorithm || recommendDetectionAlgorithm(graphData.nodes, graphData.edges);
      
      let result: DetectionResult;
      if (selectedAlgorithm === 'degree') {
        result = detectCommunitiesByDegree(graphData.nodes, graphData.edges);
      } else {
        result = detectCommunitiesByConnectivity(graphData.nodes, graphData.edges);
      }
      
      setDetectionResult(result);
      
      // 清除现有分组
      clearAllGroups();
      setAutoGroupByType(false);
      
      // 创建检测到的社区分组
      result.communities.forEach(community => {
        createGroup(community.name, community.nodeIds, community.color);
      });
      
      onGroupingChange?.();
      
    } catch (error) {
      console.error('社区检测失败:', error);
    } finally {
      setIsDetecting(false);
    }
  };

  // 打开创建分组对话框
  const handleCreateGroup = () => {
    setGroupName('');
    setGroupColor('#1976d2');
    setSelectedNodeTypes([]);
    setCreateDialogOpen(true);
  };

  // 确认创建分组
  const handleConfirmCreate = () => {
    if (!groupName.trim() || selectedNodeTypes.length === 0) return;

    // 获取选中类型的所有节点
    const nodeIds: string[] = [];
    if (graphData?.nodes) {
      graphData.nodes.forEach(node => {
        const nodeType = node.labels[0] || 'Unknown';
        if (selectedNodeTypes.includes(nodeType)) {
          nodeIds.push(node.id);
        }
      });
    }

    createGroup(groupName.trim(), nodeIds, groupColor);
    setCreateDialogOpen(false);
    onGroupingChange?.();
  };

  // 打开编辑分组对话框
  const handleEditGroup = (group: NodeGroup) => {
    setEditingGroup(group);
    setGroupName(group.name);
    setGroupColor(group.color);
    setEditDialogOpen(true);
  };

  // 确认编辑分组
  const handleConfirmEdit = () => {
    if (!editingGroup || !groupName.trim()) return;

    updateGroup(editingGroup.id, {
      name: groupName.trim(),
      color: groupColor,
      style: {
        backgroundColor: groupColor + '20',
        borderColor: groupColor,
        borderWidth: editingGroup.style?.borderWidth || 2,
        opacity: editingGroup.style?.opacity || 0.8,
      },
    });
    setEditDialogOpen(false);
    setEditingGroup(null);
    onGroupingChange?.();
  };

  // 删除分组
  const handleDeleteGroup = (groupId: string) => {
    deleteGroup(groupId);
    onGroupingChange?.();
  };

  // 切换分组折叠状态
  const handleToggleCollapse = (groupId: string) => {
    toggleGroupCollapse(groupId);
    onGroupingChange?.();
  };

  // 菜单处理
  const handleMenuOpen = (event: React.MouseEvent<HTMLElement>, groupId: string) => {
    setMenuAnchor(event.currentTarget);
    setMenuGroupId(groupId);
  };

  const handleMenuClose = () => {
    setMenuAnchor(null);
    setMenuGroupId(null);
  };

  const nodeCount = graphData?.nodes.length || 0;
  const groupCount = groupingState.groups.length;

  return (
    <Box sx={{ p: 2 }}>
      <Typography variant="h6" gutterBottom>
        节点分组
      </Typography>

      {/* 分组信息 */}
      <Paper variant="outlined" sx={{ p: 2, mb: 3, bgcolor: 'background.default' }}>
        <Typography variant="body2" color="text.secondary" gutterBottom>
          当前图谱: {nodeCount} 个节点, {groupCount} 个分组
        </Typography>
        {detectionResult && (
          <Box sx={{ mt: 1 }}>
            <Typography variant="caption" color="primary" display="block">
              社区检测结果 ({detectionResult.algorithm === 'connectivity' ? '连通性' : '度中心性'}):
            </Typography>
            <Typography variant="caption" color="text.secondary">
              模块度: {detectionResult.totalModularity.toFixed(3)} | 
              平均大小: {detectionResult.stats.averageSize.toFixed(1)} | 
              最大社区: {detectionResult.stats.largestSize} 个节点
            </Typography>
          </Box>
        )}
      </Paper>

      {/* 分组控制 */}
      <Box sx={{ mb: 3 }}>
        <FormControlLabel
          control={
            <Switch
              size="small"
              checked={groupingState.autoGroupByType}
              onChange={(e) => handleAutoGroupByType(e.target.checked)}
            />
          }
          label="按类型自动分组"
        />
        
        <FormControlLabel
          control={
            <Switch
              size="small"
              checked={groupingState.showGroupLabels}
              onChange={(e) => setShowGroupLabels(e.target.checked)}
            />
          }
          label="显示分组标签"
        />
      </Box>

      {/* 智能社区检测 */}
      <Box sx={{ mb: 3 }}>
        <Typography variant="subtitle2" gutterBottom sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <PsychologyIcon fontSize="small" />
          智能社区检测
        </Typography>
        
        <Box sx={{ display: 'flex', gap: 1, mb: 2 }}>
          <LoadingButton
            variant="contained"
            size="small"
            startIcon={<AnalyticsIcon />}
            loading={isDetecting}
            onClick={() => handleCommunityDetection()}
            disabled={isDetecting || !graphData?.nodes || graphData.nodes.length < 3}
            label="自动检测"
            loadingLabel="检测中..."
            sx={{ flex: 1 }}
          />
          
          <Button
            variant="outlined"
            size="small"
            startIcon={<TrendingUpIcon />}
            onClick={() => handleCommunityDetection('degree')}
            disabled={isDetecting || !graphData?.nodes || graphData.nodes.length < 3}
            sx={{ flex: 1 }}
          >
            度中心性
          </Button>
        </Box>
        
        {graphData?.nodes && graphData.nodes.length >= 3 && (
          <Typography variant="caption" color="text.secondary">
            推荐算法: {recommendDetectionAlgorithm(graphData.nodes, graphData.edges || [])}
            {recommendDetectionAlgorithm(graphData.nodes, graphData.edges || []) === 'connectivity' ? ' (连通性)' : ' (度中心性)'}
          </Typography>
        )}
      </Box>

      {/* 创建分组按钮 */}
      <Button
        variant="outlined"
        size="small"
        startIcon={<AddIcon />}
        onClick={handleCreateGroup}
        fullWidth
        sx={{ mb: 2 }}
        disabled={groupingState.autoGroupByType || isDetecting}
      >
        手动创建分组
      </Button>

      <Divider sx={{ mb: 2 }} />

      {/* 分组列表 */}
      <Typography variant="subtitle2" gutterBottom>
        分组列表 ({groupCount})
      </Typography>

      {groupingState.groups.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ textAlign: 'center', py: 3 }}>
          暂无分组
          <br />
          {groupingState.autoGroupByType ? '启用自动分组或' : ''}创建分组来组织节点
        </Typography>
      ) : (
        <List dense>
          {groupingState.groups.map((group) => (
            <ListItem
              key={group.id}
              sx={{
                border: 1,
                borderColor: 'divider',
                borderRadius: 1,
                mb: 1,
                bgcolor: 'background.paper',
              }}
            >
              <Box sx={{ display: 'flex', alignItems: 'center', mr: 1 }}>
                <Box
                  sx={{
                    width: 16,
                    height: 16,
                    borderRadius: '50%',
                    bgcolor: group.color,
                    mr: 1,
                  }}
                />
                <Tooltip title={group.collapsed ? '展开分组' : '折叠分组'}>
                  <IconButton
                    size="small"
                    onClick={() => handleToggleCollapse(group.id)}
                  >
                    {group.collapsed ? <VisibilityOffIcon /> : <VisibilityIcon />}
                  </IconButton>
                </Tooltip>
              </Box>
              
              <ListItemText
                primary={group.name}
                secondary={
                  <Box>
                    <Typography variant="caption" display="block">
                      {group.nodeIds.length} 个节点{group.collapsed ? ' (已折叠)' : ''}
                    </Typography>
                    {detectionResult && (
                      <Typography variant="caption" color="text.secondary">
                        {(() => {
                          const community = detectionResult.communities.find(c => 
                            c.nodeIds.length === group.nodeIds.length &&
                            c.nodeIds.every(id => group.nodeIds.includes(id))
                          );
                          if (community) {
                            return `密度: ${(community.stats.density * 100).toFixed(1)}% | 模块度: ${community.stats.modularity.toFixed(3)}`;
                          }
                          return '';
                        })()}
                      </Typography>
                    )}
                  </Box>
                }
              />
              
              <ListItemSecondaryAction>
                <IconButton
                  size="small"
                  onClick={(e) => handleMenuOpen(e, group.id)}
                >
                  <MoreVertIcon />
                </IconButton>
              </ListItemSecondaryAction>
            </ListItem>
          ))}
        </List>
      )}

      {/* 清除所有分组 */}
      {groupingState.groups.length > 0 && (
        <Button
          variant="text"
          size="small"
          color="error"
          onClick={() => {
            clearAllGroups();
            onGroupingChange?.();
          }}
          sx={{ mt: 2 }}
        >
          清除所有分组
        </Button>
      )}

      {/* 分组菜单 */}
      <Menu
        anchorEl={menuAnchor}
        open={Boolean(menuAnchor)}
        onClose={handleMenuClose}
      >
        <MenuItem
          onClick={() => {
            const group = groupingState.groups.find(g => g.id === menuGroupId);
            if (group) {
              handleEditGroup(group);
            }
            handleMenuClose();
          }}
        >
          <EditIcon sx={{ mr: 1 }} />
          编辑分组
        </MenuItem>
        <MenuItem
          onClick={() => {
            if (menuGroupId) {
              handleDeleteGroup(menuGroupId);
            }
            handleMenuClose();
          }}
          sx={{ color: 'error.main' }}
        >
          <DeleteIcon sx={{ mr: 1 }} />
          删除分组
        </MenuItem>
      </Menu>

      {/* 创建分组对话框 */}
      <Dialog open={createDialogOpen} onClose={() => setCreateDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>创建节点分组</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            margin="dense"
            label="分组名称"
            fullWidth
            variant="outlined"
            value={groupName}
            onChange={(e) => setGroupName(e.target.value)}
            sx={{ mb: 2 }}
          />
          
          <Typography variant="subtitle2" gutterBottom>
            选择节点类型:
          </Typography>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mb: 2 }}>
            {availableNodeTypes.map((nodeType) => (
              <Chip
                key={nodeType}
                label={nodeType}
                variant={selectedNodeTypes.includes(nodeType) ? 'filled' : 'outlined'}
                onClick={() => {
                  setSelectedNodeTypes(prev =>
                    prev.includes(nodeType)
                      ? prev.filter(t => t !== nodeType)
                      : [...prev, nodeType]
                  );
                }}
                size="small"
              />
            ))}
          </Box>
          
          <Typography variant="subtitle2" gutterBottom>
            分组颜色:
          </Typography>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
            {predefinedColors.map((color) => (
              <Box
                key={color}
                sx={{
                  width: 32,
                  height: 32,
                  borderRadius: '50%',
                  bgcolor: color,
                  cursor: 'pointer',
                  border: groupColor === color ? 3 : 1,
                  borderColor: groupColor === color ? 'primary.main' : 'divider',
                }}
                onClick={() => setGroupColor(color)}
              />
            ))}
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateDialogOpen(false)}>取消</Button>
          <Button
            onClick={handleConfirmCreate}
            variant="contained"
            disabled={!groupName.trim() || selectedNodeTypes.length === 0}
          >
            创建
          </Button>
        </DialogActions>
      </Dialog>

      {/* 编辑分组对话框 */}
      <Dialog open={editDialogOpen} onClose={() => setEditDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>编辑分组</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            margin="dense"
            label="分组名称"
            fullWidth
            variant="outlined"
            value={groupName}
            onChange={(e) => setGroupName(e.target.value)}
            sx={{ mb: 2 }}
          />
          
          <Typography variant="subtitle2" gutterBottom>
            分组颜色:
          </Typography>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
            {predefinedColors.map((color) => (
              <Box
                key={color}
                sx={{
                  width: 32,
                  height: 32,
                  borderRadius: '50%',
                  bgcolor: color,
                  cursor: 'pointer',
                  border: groupColor === color ? 3 : 1,
                  borderColor: groupColor === color ? 'primary.main' : 'divider',
                }}
                onClick={() => setGroupColor(color)}
              />
            ))}
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEditDialogOpen(false)}>取消</Button>
          <Button
            onClick={handleConfirmEdit}
            variant="contained"
            disabled={!groupName.trim()}
          >
            保存
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
