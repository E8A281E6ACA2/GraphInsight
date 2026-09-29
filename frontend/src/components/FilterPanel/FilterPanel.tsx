import { useMemo } from 'react';
import {
  Box,
  Typography,
  FormGroup,
  FormControlLabel,
  Checkbox,
  Divider,
  Button,
  Paper,
  Chip,
} from '@mui/material';
import { Clear as ClearIcon } from '@mui/icons-material';
import { useGraphStore } from '../../store/graphStore';
import { getNodeLabel, NODE_COLORS } from '../../utils/colorMapping';

export function FilterPanel() {
  const graphData = useGraphStore((state) => state.graphData);
  const activeFilters = useGraphStore((state) => state.activeFilters);
  const setNodeTypeFilter = useGraphStore((state) => state.setNodeTypeFilter);
  const setRelationshipTypeFilter = useGraphStore((state) => state.setRelationshipTypeFilter);
  const clearFilters = useGraphStore((state) => state.clearFilters);

  // 从图数据中提取节点类型
  const nodeTypes = useMemo(() => {
    if (!graphData) return [];
    const types = new Set<string>();
    graphData.nodes.forEach((node) => {
      node.labels.forEach((label) => types.add(label));
    });
    return Array.from(types).sort();
  }, [graphData]);

  // 从图数据中提取关系类型
  const relationshipTypes = useMemo(() => {
    if (!graphData) return [];
    const types = new Set<string>();
    graphData.edges.forEach((edge) => {
      types.add(edge.type);
    });
    return Array.from(types).sort();
  }, [graphData]);

  // 计算节点类型的数量
  const nodeTypeCounts = useMemo(() => {
    if (!graphData) return {};
    const counts: Record<string, number> = {};
    graphData.nodes.forEach((node) => {
      node.labels.forEach((label) => {
        counts[label] = (counts[label] || 0) + 1;
      });
    });
    return counts;
  }, [graphData]);

  // 计算关系类型的数量
  const relationshipTypeCounts = useMemo(() => {
    if (!graphData) return {};
    const counts: Record<string, number> = {};
    graphData.edges.forEach((edge) => {
      counts[edge.type] = (counts[edge.type] || 0) + 1;
    });
    return counts;
  }, [graphData]);

  const handleNodeTypeChange = (type: string, checked: boolean) => {
    const newTypes = checked
      ? [...activeFilters.nodeTypes, type]
      : activeFilters.nodeTypes.filter((t) => t !== type);
    setNodeTypeFilter(newTypes);
  };

  const handleRelationshipTypeChange = (type: string, checked: boolean) => {
    const newTypes = checked
      ? [...activeFilters.relationshipTypes, type]
      : activeFilters.relationshipTypes.filter((t) => t !== type);
    setRelationshipTypeFilter(newTypes);
  };

  const hasActiveFilters =
    activeFilters.nodeTypes.length > 0 || activeFilters.relationshipTypes.length > 0;

  if (!graphData) {
    return (
      <Box sx={{ p: 2 }}>
        <Typography variant="body2" color="text.secondary">
          执行查询后显示过滤选项
        </Typography>
      </Box>
    );
  }

  return (
    <Box sx={{ p: 2 }}>
      {/* 标题和清除按钮 */}
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
        <Typography variant="h6" fontWeight={600}>
          过滤器
        </Typography>
        {hasActiveFilters && (
          <Button
            size="small"
            startIcon={<ClearIcon />}
            onClick={clearFilters}
            color="secondary"
          >
            清除
          </Button>
        )}
      </Box>

      {/* 节点类型过滤器 */}
      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" fontWeight={600} gutterBottom>
          节点类型
        </Typography>
        <FormGroup>
          {nodeTypes.map((type) => (
            <FormControlLabel
              key={type}
              control={
                <Checkbox
                  checked={activeFilters.nodeTypes.includes(type)}
                  onChange={(e) => handleNodeTypeChange(type, e.target.checked)}
                  size="small"
                  sx={{
                    color: NODE_COLORS[type] || NODE_COLORS.default,
                    '&.Mui-checked': {
                      color: NODE_COLORS[type] || NODE_COLORS.default,
                    },
                  }}
                />
              }
              label={
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <Typography variant="body2">{getNodeLabel([type])}</Typography>
                  <Chip
                    label={nodeTypeCounts[type] || 0}
                    size="small"
                    sx={{
                      height: 20,
                      fontSize: '0.7rem',
                      bgcolor: NODE_COLORS[type] || NODE_COLORS.default,
                      color: 'white',
                    }}
                  />
                </Box>
              }
            />
          ))}
        </FormGroup>
      </Paper>

      <Divider sx={{ my: 2 }} />

      {/* 关系类型过滤器 */}
      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="subtitle2" fontWeight={600} gutterBottom>
          关系类型
        </Typography>
        <FormGroup>
          {relationshipTypes.map((type) => (
            <FormControlLabel
              key={type}
              control={
                <Checkbox
                  checked={activeFilters.relationshipTypes.includes(type)}
                  onChange={(e) => handleRelationshipTypeChange(type, e.target.checked)}
                  size="small"
                />
              }
              label={
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <Typography variant="body2">{type}</Typography>
                  <Chip
                    label={relationshipTypeCounts[type] || 0}
                    size="small"
                    variant="outlined"
                    sx={{ height: 20, fontSize: '0.7rem' }}
                  />
                </Box>
              }
            />
          ))}
        </FormGroup>
      </Paper>

      {/* 活动过滤器摘要 */}
      {hasActiveFilters && (
        <Box sx={{ mt: 2, p: 2, bgcolor: 'action.hover', borderRadius: 1 }}>
          <Typography variant="caption" color="text.secondary" display="block" gutterBottom>
            已激活过滤器
          </Typography>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
            {activeFilters.nodeTypes.map((type) => (
              <Chip
                key={type}
                label={getNodeLabel([type])}
                size="small"
                onDelete={() => handleNodeTypeChange(type, false)}
                sx={{
                  bgcolor: NODE_COLORS[type] || NODE_COLORS.default,
                  color: 'white',
                }}
              />
            ))}
            {activeFilters.relationshipTypes.map((type) => (
              <Chip
                key={type}
                label={type}
                size="small"
                onDelete={() => handleRelationshipTypeChange(type, false)}
              />
            ))}
          </Box>
        </Box>
      )}
    </Box>
  );
}
