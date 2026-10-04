import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Typography,
  Alert,
  Box,
  List,
  ListItem,
  ListItemIcon,
  ListItemText,
} from '@mui/material';
import {
  Warning as WarningIcon,
  TipsAndUpdates as TipIcon,
} from '@mui/icons-material';

interface PerformanceWarningDialogProps {
  open: boolean;
  nodeCount: number;
  onContinue: () => void;
  onCancel: () => void;
}

export function PerformanceWarningDialog({
  open,
  nodeCount,
  onContinue,
  onCancel,
}: PerformanceWarningDialogProps) {
  return (
    <Dialog open={open} onClose={onCancel} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <WarningIcon color="warning" />
        性能警告
      </DialogTitle>

      <DialogContent>
        <Alert severity="warning" sx={{ mb: 2 }}>
          查询返回了 <strong>{nodeCount}</strong> 个节点，这可能会影响渲染性能和交互响应速度。
        </Alert>

        <Typography variant="body2" color="text.secondary" gutterBottom>
          建议的优化方法：
        </Typography>

        <List dense>
          <ListItem>
            <ListItemIcon>
              <TipIcon fontSize="small" color="primary" />
            </ListItemIcon>
            <ListItemText
              primary="使用 LIMIT 限制结果数量"
              secondary="优先使用自动发现查询，并为探索型查询设置合理的返回上限"
            />
          </ListItem>
          <ListItem>
            <ListItemIcon>
              <TipIcon fontSize="small" color="primary" />
            </ListItemIcon>
            <ListItemText
              primary="添加更具体的过滤条件"
              secondary="结合节点类型、业务字段或关键词逐步缩小结果范围"
            />
          </ListItem>
          <ListItem>
            <ListItemIcon>
              <TipIcon fontSize="small" color="primary" />
            </ListItemIcon>
            <ListItemText
              primary="使用节点类型过滤器"
              secondary="在左侧过滤面板中选择特定的节点类型"
            />
          </ListItem>
          <ListItem>
            <ListItemIcon>
              <TipIcon fontSize="small" color="primary" />
            </ListItemIcon>
            <ListItemText
              primary="分批查询和展开"
              secondary="先查询少量节点，然后通过双击展开感兴趣的节点"
            />
          </ListItem>
        </List>

        <Box sx={{ mt: 2, p: 2, bgcolor: 'background.default', borderRadius: 1 }}>
          <Typography variant="caption" color="text.secondary">
            提示：对于大型图谱，建议节点数量控制在 500 以内以获得最佳性能。
          </Typography>
        </Box>
      </DialogContent>

      <DialogActions>
        <Button onClick={onCancel} color="inherit">
          取消加载
        </Button>
        <Button onClick={onContinue} variant="contained" color="warning">
          仍然加载
        </Button>
      </DialogActions>
    </Dialog>
  );
}
