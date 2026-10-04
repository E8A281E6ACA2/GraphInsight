import { useState } from 'react';
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Box,
  Typography,
  Slider,
  FormControlLabel,
  Switch,
  Divider,
  TextField,
  Chip,
} from '@mui/material';
import { 
  Palette as PaletteIcon,
} from '@mui/icons-material';

interface NodeTypeStyleConfig {
  color: string;
  size: number;
  borderWidth: number;
  showLabels: boolean;
  labelSize: number;
  showImages: boolean;
  caption: string[];
}

interface NodeTypeStyleConfigProps {
  open: boolean;
  onClose: () => void;
  nodeType: string;
  config: NodeTypeStyleConfig;
  availableProperties: string[];
  onConfigChange: (config: NodeTypeStyleConfig) => void;
}

// Neo4j Browser 风格的颜色选项
const COLOR_OPTIONS = [
  '#68BDF6', '#6DCE9E', '#FFD86E', '#DE9BF9', '#FB95AF',
  '#FFB366', '#A5ABB6', '#009CC4', '#F79767', '#57C7E3',
  '#F16667', '#D9C8AE', '#8DCC93', '#ECB5C9', '#4C8EDA',
  '#FFC454', '#DA7194', '#569480', '#848484', '#D9D9D9'
];

export function NodeTypeStyleConfig({
  open,
  onClose,
  nodeType,
  config,
  availableProperties,
  onConfigChange,
}: NodeTypeStyleConfigProps) {
  const [draft, setDraft] = useState<{ key: string; value: NodeTypeStyleConfig } | null>(null);
  const configKey = `${nodeType}:${open ? 'open' : 'closed'}`;
  const tempConfig = draft?.key === configKey ? draft.value : config;
  const setTempConfig = (next: NodeTypeStyleConfig | ((prev: NodeTypeStyleConfig) => NodeTypeStyleConfig)) => {
    setDraft({
      key: configKey,
      value: typeof next === 'function' ? next(tempConfig) : next,
    });
  };

  const handleSave = () => {
    console.log('🎨 NodeTypeStyleConfig - Saving config for:', nodeType, tempConfig);
    onConfigChange(tempConfig);
    console.log('🎨 NodeTypeStyleConfig - Config change callback called');
    setDraft(null);
    onClose();
  };

  const handleReset = () => {
    setTempConfig({
      color: '#1976d2',
      size: 60,
      borderWidth: 2,
      showLabels: true,
      labelSize: 12,
      showImages: true,
      caption: ['name'],
    });
  };

  const handleCaptionToggle = (property: string) => {
    const current = tempConfig.caption || [];
    const updated = current.includes(property)
      ? current.filter(p => p !== property)
      : [...current, property];
    
    setTempConfig({
      ...tempConfig,
      caption: updated,
    });
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>
        <Box display="flex" alignItems="center" justifyContent="space-between">
          <Box display="flex" alignItems="center" gap={1}>
            <PaletteIcon />
            <Typography variant="h6">
              配置 "{nodeType}" 节点样式
            </Typography>
          </Box>
        </Box>
      </DialogTitle>
      
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
          为 "{nodeType}" 类型的所有节点配置统一的样式和显示属性
        </Typography>

        {/* 样式预览 */}
        <Box sx={{ mb: 3, p: 2, border: '1px solid', borderColor: 'divider', borderRadius: 1 }}>
          <Typography variant="subtitle2" gutterBottom>
            样式预览
          </Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
            <Box
              sx={{
                width: tempConfig.size / 1.5,
                height: tempConfig.size / 1.5,
                backgroundColor: tempConfig.color,
                borderRadius: '50%',
                border: `${tempConfig.borderWidth}px solid #ffffff`,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: `${tempConfig.labelSize}px`,
                color: 'white',
                fontWeight: 'bold',
                boxShadow: 2,
              }}
            >
              {nodeType.charAt(0).toUpperCase()}
            </Box>
            <Box>
              <Typography variant="body2" fontWeight="bold">
                {nodeType}
              </Typography>
              {tempConfig.showLabels && tempConfig.caption.length > 0 && (
                <Typography variant="caption" color="text.secondary">
                  显示: {tempConfig.caption.join(' | ')}
                </Typography>
              )}
            </Box>
          </Box>
        </Box>

        <Box sx={{ display: 'flex', gap: 3 }}>
          {/* 左列：颜色和大小 */}
          <Box sx={{ flex: 1 }}>
            {/* 颜色选择 */}
            <Typography variant="subtitle2" gutterBottom>
              节点颜色
            </Typography>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mb: 3 }}>
              {COLOR_OPTIONS.map((color) => (
                <Box
                  key={color}
                  onClick={() => setTempConfig({ ...tempConfig, color })}
                  sx={{
                    width: 32,
                    height: 32,
                    backgroundColor: color,
                    borderRadius: '50%',
                    cursor: 'pointer',
                    border: tempConfig.color === color ? '3px solid #000' : '2px solid #fff',
                    boxShadow: 1,
                    '&:hover': {
                      transform: 'scale(1.1)',
                    },
                    transition: 'all 0.2s',
                  }}
                />
              ))}
            </Box>

            {/* 自定义颜色 */}
            <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', mb: 3 }}>
              <TextField
                size="small"
                label="自定义颜色"
                value={tempConfig.color}
                onChange={(e) => setTempConfig({ ...tempConfig, color: e.target.value })}
                sx={{ flex: 1 }}
              />
              <Box
                sx={{
                  width: 40,
                  height: 40,
                  backgroundColor: tempConfig.color,
                  border: '1px solid #ccc',
                  borderRadius: 1,
                }}
              />
            </Box>

            {/* 节点大小 */}
            <Typography gutterBottom>节点大小: {tempConfig.size}px</Typography>
            <Slider
              value={tempConfig.size}
              onChange={(_, value) => setTempConfig({ ...tempConfig, size: value as number })}
              min={30}
              max={120}
              valueLabelDisplay="auto"
              sx={{ mb: 3 }}
            />

            {/* 边框宽度 */}
            <Typography gutterBottom>边框宽度: {tempConfig.borderWidth}px</Typography>
            <Slider
              value={tempConfig.borderWidth}
              onChange={(_, value) => setTempConfig({ ...tempConfig, borderWidth: value as number })}
              min={0}
              max={8}
              valueLabelDisplay="auto"
              sx={{ mb: 3 }}
            />
          </Box>

          {/* 右列：标签和属性 */}
          <Box sx={{ flex: 1 }}>
            {/* 标签设置 */}
            <FormControlLabel
              control={
                <Switch
                  checked={tempConfig.showLabels}
                  onChange={(e) => setTempConfig({ ...tempConfig, showLabels: e.target.checked })}
                />
              }
              label="显示节点标签"
              sx={{ mb: 2 }}
            />

            {tempConfig.showLabels && (
              <>
                <Typography gutterBottom>标签字体大小: {tempConfig.labelSize}px</Typography>
                <Slider
                  value={tempConfig.labelSize}
                  onChange={(_, value) => setTempConfig({ ...tempConfig, labelSize: value as number })}
                  min={8}
                  max={24}
                  valueLabelDisplay="auto"
                  sx={{ mb: 3 }}
                />

                {/* 标签属性选择 */}
                <Typography variant="subtitle2" gutterBottom>
                  标签显示属性 (Caption)
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                  选择要在节点标签中显示的属性
                </Typography>
                
                <Box sx={{ mb: 2 }}>
                  <Typography variant="caption" color="text.secondary">
                    当前显示:
                  </Typography>
                  <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mt: 1 }}>
                    {(tempConfig.caption || []).map((prop) => (
                      <Chip
                        key={prop}
                        label={prop}
                        onDelete={() => handleCaptionToggle(prop)}
                        color="primary"
                        size="small"
                      />
                    ))}
                    {(!tempConfig.caption || tempConfig.caption.length === 0) && (
                      <Typography variant="body2" color="text.secondary">
                        未选择属性 (将显示节点ID)
                      </Typography>
                    )}
                  </Box>
                </Box>

                <Box>
                  <Typography variant="caption" color="text.secondary">
                    可用属性:
                  </Typography>
                  <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mt: 1 }}>
                    {availableProperties.map((prop) => {
                      const isSelected = (tempConfig.caption || []).includes(prop);
                      return (
                        <Chip
                          key={prop}
                          label={prop}
                          onClick={() => handleCaptionToggle(prop)}
                          variant={isSelected ? "filled" : "outlined"}
                          color={isSelected ? "primary" : "default"}
                          size="small"
                          sx={{ cursor: 'pointer' }}
                        />
                      );
                    })}
                  </Box>
                </Box>
              </>
            )}

            <Divider sx={{ my: 2 }} />

            {/* 图片设置 */}
            <FormControlLabel
              control={
                <Switch
                  checked={tempConfig.showImages}
                  onChange={(e) => setTempConfig({ ...tempConfig, showImages: e.target.checked })}
                />
              }
              label="显示节点图片"
            />
          </Box>
        </Box>
      </DialogContent>

      <DialogActions>
        <Button onClick={handleReset} color="secondary">
          重置默认
        </Button>
        <Button onClick={onClose}>
          取消
        </Button>
        <Button onClick={handleSave} variant="contained">
          应用样式
        </Button>
      </DialogActions>
    </Dialog>
  );
}
