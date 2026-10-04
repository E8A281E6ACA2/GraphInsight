import { useState } from 'react';
import {
  Box,
  Typography,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Button,
  Slider,
  FormControlLabel,
  Switch,
  Divider,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Paper,
} from '@mui/material';
import {
  ExpandMore as ExpandMoreIcon,
  PlayArrow as PlayIcon,
  Refresh as RefreshIcon,
} from '@mui/icons-material';
import { LAYOUT_CONFIGS, LAYOUT_NAMES, type LayoutType } from '../../utils/cytoscapeConfig';
import { useGraphStore } from '../../store/graphStore';
import type { LayoutConfig, LayoutConfigValue } from '../../renderers/core/types';

interface LayoutPanelProps {
  onLayoutChange: (layout: LayoutType, config?: LayoutConfig) => void;
}

export function LayoutPanel({ onLayoutChange }: LayoutPanelProps) {
  const { graphData, preferredLayout, setPreferredLayout } = useGraphStore();
  const [selectedLayout, setSelectedLayout] = useState<LayoutType>(preferredLayout as LayoutType || 'cose');
  const [layoutConfig, setLayoutConfig] = useState<LayoutConfig>(LAYOUT_CONFIGS[preferredLayout as LayoutType] || LAYOUT_CONFIGS.cose);

  const numberConfig = (key: string, fallback: number) => {
    const value = layoutConfig[key];
    return typeof value === 'number' ? value : fallback;
  };

  const handleLayoutSelect = (layout: LayoutType) => {
    setSelectedLayout(layout);
    setLayoutConfig(LAYOUT_CONFIGS[layout] || {});
    setPreferredLayout(layout); // 保存用户偏好
  };

  const handleApplyRecommended = () => {
    const nextConfig = LAYOUT_CONFIGS[recommendedLayout] || {};
    setSelectedLayout(recommendedLayout);
    setLayoutConfig(nextConfig);
    setPreferredLayout(recommendedLayout);
    onLayoutChange(recommendedLayout, nextConfig);
  };

  const handleConfigChange = (key: string, value: LayoutConfigValue) => {
    setLayoutConfig((prev) => ({
      ...prev,
      [key]: value,
    }));
  };

  const handleApplyLayout = () => {
    onLayoutChange(selectedLayout, layoutConfig);
  };

  const handleResetConfig = () => {
    setLayoutConfig(LAYOUT_CONFIGS[selectedLayout] || {});
  };

  const renderLayoutControls = () => {
    switch (selectedLayout) {
      case 'cose':
      case 'fcose':
      case 'cose-compact':
      case 'cose-loose':
        return (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2" gutterBottom color="primary">
              力导向参数
            </Typography>
            
            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" gutterBottom>
                节点排斥力: {numberConfig('nodeRepulsion', 8000)}
              </Typography>
              <Slider
                size="small"
                value={numberConfig('nodeRepulsion', 8000)}
                onChange={(_, value) => handleConfigChange('nodeRepulsion', value)}
                min={1000}
                max={20000}
                step={500}
              />
            </Box>

            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" gutterBottom>
                理想边长: {numberConfig('idealEdgeLength', 100)}
              </Typography>
              <Slider
                size="small"
                value={numberConfig('idealEdgeLength', 100)}
                onChange={(_, value) => handleConfigChange('idealEdgeLength', value)}
                min={20}
                max={200}
                step={10}
              />
            </Box>

            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" gutterBottom>
                重力: {numberConfig('gravity', 80)}
              </Typography>
              <Slider
                size="small"
                value={numberConfig('gravity', 80)}
                onChange={(_, value) => handleConfigChange('gravity', value)}
                min={10}
                max={200}
                step={5}
              />
            </Box>
          </Box>
        );

      case 'circle':
      case 'circle-large':
      case 'circle-spiral':
        return (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2" gutterBottom color="primary">
              圆形布局参数
            </Typography>
            
            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" gutterBottom>
                半径: {numberConfig('radius', 200)}
              </Typography>
              <Slider
                size="small"
                value={numberConfig('radius', 200)}
                onChange={(_, value) => handleConfigChange('radius', value)}
                min={50}
                max={500}
                step={10}
              />
            </Box>

            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={layoutConfig.clockwise !== false}
                  onChange={(e) => handleConfigChange('clockwise', e.target.checked)}
                />
              }
              label="顺时针排列"
            />
          </Box>
        );

      case 'grid':
        return (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2" gutterBottom color="primary">
              网格布局参数
            </Typography>
            
            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" gutterBottom>
                行数: {numberConfig('rows', 0) || '自动'}
              </Typography>
              <Slider
                size="small"
                value={numberConfig('rows', 0)}
                onChange={(_, value) => handleConfigChange('rows', value === 0 ? undefined : value)}
                min={0}
                max={20}
                step={1}
              />
            </Box>

            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" gutterBottom>
                列数: {numberConfig('cols', 0) || '自动'}
              </Typography>
              <Slider
                size="small"
                value={numberConfig('cols', 0)}
                onChange={(_, value) => handleConfigChange('cols', value === 0 ? undefined : value)}
                min={0}
                max={20}
                step={1}
              />
            </Box>
          </Box>
        );

      case 'concentric':
        return (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2" gutterBottom color="primary">
              同心圆布局参数
            </Typography>
            
            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" gutterBottom>
                节点间距: {numberConfig('minNodeSpacing', 50)}
              </Typography>
              <Slider
                size="small"
                value={numberConfig('minNodeSpacing', 50)}
                onChange={(_, value) => handleConfigChange('minNodeSpacing', value)}
                min={10}
                max={200}
                step={5}
              />
            </Box>
          </Box>
        );

      case 'breadthfirst':
      case 'breadthfirst-vertical':
      case 'breadthfirst-horizontal':
        return (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2" gutterBottom color="primary">
              层次布局参数
            </Typography>
            
            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" gutterBottom>
                间距因子: {numberConfig('spacingFactor', 1.5)}
              </Typography>
              <Slider
                size="small"
                value={numberConfig('spacingFactor', 1.5)}
                onChange={(_, value) => handleConfigChange('spacingFactor', value)}
                min={0.5}
                max={3}
                step={0.1}
              />
            </Box>

            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={layoutConfig.directed !== false}
                  onChange={(e) => handleConfigChange('directed', e.target.checked)}
                />
              }
              label="有向图模式"
            />
          </Box>
        );

      default:
        return (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
            此布局算法无可调参数
          </Typography>
        );
    }
  };

  const nodeCount = graphData?.nodes.length || 0;
  const edgeCount = graphData?.edges.length || 0;

  // 智能布局推荐
  const getRecommendedLayout = (): LayoutType => {
    if (nodeCount === 0) return 'cose';
    
    const density = edgeCount / (nodeCount * (nodeCount - 1) / 2);
    
    if (nodeCount <= 10) {
      return 'circle'; // 小图谱用圆形
    } else if (nodeCount <= 30) {
      return density > 0.3 ? 'cose-compact' : 'cose'; // 中小图谱
    } else if (nodeCount <= 100) {
      return density > 0.2 ? 'cose-compact' : 'cose-loose'; // 中等图谱
    } else {
      return 'fcose'; // 大图谱用快速算法（优化的cose）
    }
  };

  const recommendedLayout = getRecommendedLayout();
  const isRecommended = selectedLayout === recommendedLayout;

  return (
    <Box sx={{ p: 2 }}>
      <Typography variant="h6" gutterBottom>
        图谱布局
      </Typography>
      
      {/* 图谱信息和推荐 */}
      <Paper variant="outlined" sx={{ p: 2, mb: 3, bgcolor: 'background.default' }}>
        <Typography variant="body2" color="text.secondary" gutterBottom>
          当前图谱: {nodeCount} 个节点, {edgeCount} 条边
        </Typography>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1 }}>
          <Typography variant="caption" color="primary">
            推荐布局: {LAYOUT_NAMES[recommendedLayout]}
          </Typography>
          {!isRecommended && (
            <Button
              size="small"
              variant="outlined"
              onClick={handleApplyRecommended}
              sx={{ fontSize: '0.7rem', py: 0.5, px: 1 }}
            >
              应用推荐
            </Button>
          )}
        </Box>
      </Paper>

      {/* 布局选择 */}
      <FormControl fullWidth sx={{ mb: 2 }}>
        <InputLabel size="small">布局算法</InputLabel>
        <Select
          size="small"
          value={selectedLayout}
          onChange={(e) => handleLayoutSelect(e.target.value as LayoutType)}
          label="布局算法"
        >
          {/* 力导向布局组 */}
          <Typography variant="caption" sx={{ px: 2, py: 1, color: 'text.secondary', fontWeight: 'bold' }}>
            力导向布局
          </Typography>
          {['cose', 'fcose', 'cose-compact', 'cose-loose'].map((key) => (
            <MenuItem key={key} value={key} sx={{ pl: 3 }}>
              {LAYOUT_NAMES[key as LayoutType]}
              {key === recommendedLayout && ' ⭐'}
            </MenuItem>
          ))}
          
          {/* 几何布局组 */}
          <Typography variant="caption" sx={{ px: 2, py: 1, color: 'text.secondary', fontWeight: 'bold' }}>
            几何布局
          </Typography>
          {['circle', 'circle-large', 'circle-spiral', 'grid', 'concentric'].map((key) => (
            <MenuItem key={key} value={key} sx={{ pl: 3 }}>
              {LAYOUT_NAMES[key as LayoutType]}
              {key === recommendedLayout && ' ⭐'}
            </MenuItem>
          ))}
          
          {/* 层次布局组 */}
          <Typography variant="caption" sx={{ px: 2, py: 1, color: 'text.secondary', fontWeight: 'bold' }}>
            层次布局
          </Typography>
          {['breadthfirst', 'breadthfirst-vertical', 'breadthfirst-horizontal'].map((key) => (
            <MenuItem key={key} value={key} sx={{ pl: 3 }}>
              {LAYOUT_NAMES[key as LayoutType]}
              {key === recommendedLayout && ' ⭐'}
            </MenuItem>
          ))}
          
          {/* 其他布局组 */}
          <Typography variant="caption" sx={{ px: 2, py: 1, color: 'text.secondary', fontWeight: 'bold' }}>
            其他布局
          </Typography>
          {['random', 'preset', 'null'].map((key) => (
            <MenuItem key={key} value={key} sx={{ pl: 3 }}>
              {LAYOUT_NAMES[key as LayoutType]}
              {key === recommendedLayout && ' ⭐'}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {/* 快速应用按钮 */}
      <Box sx={{ display: 'flex', gap: 1, mb: 3 }}>
        <Button
          variant="contained"
          size="small"
          startIcon={<PlayIcon />}
          onClick={handleApplyLayout}
          fullWidth
        >
          应用布局
        </Button>
        <Button
          variant="outlined"
          size="small"
          startIcon={<RefreshIcon />}
          onClick={handleResetConfig}
        >
          重置
        </Button>
      </Box>

      <Divider sx={{ mb: 2 }} />

      {/* 通用参数 */}
      <Accordion>
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Typography variant="subtitle2">通用参数</Typography>
        </AccordionSummary>
        <AccordionDetails>
          <Box sx={{ mb: 2 }}>
            <Typography variant="body2" gutterBottom>
              动画时长: {numberConfig('animationDuration', 500)}ms
            </Typography>
            <Slider
              size="small"
              value={numberConfig('animationDuration', 500)}
              onChange={(_, value) => handleConfigChange('animationDuration', value)}
              min={0}
              max={2000}
              step={100}
            />
          </Box>

          <FormControlLabel
            control={
              <Switch
                size="small"
                checked={layoutConfig.animate !== false}
                onChange={(e) => handleConfigChange('animate', e.target.checked)}
              />
            }
            label="启用动画"
          />

          <FormControlLabel
            control={
              <Switch
                size="small"
                checked={layoutConfig.fit !== false}
                onChange={(e) => handleConfigChange('fit', e.target.checked)}
              />
            }
            label="自动适应视口"
          />
        </AccordionDetails>
      </Accordion>

      {/* 布局特定参数 */}
      <Accordion>
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Typography variant="subtitle2">布局参数</Typography>
        </AccordionSummary>
        <AccordionDetails>
          {renderLayoutControls()}
        </AccordionDetails>
      </Accordion>
    </Box>
  );
}
