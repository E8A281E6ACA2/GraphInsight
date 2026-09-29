import { Box, Typography, Chip, Divider } from '@mui/material';
import {
  Circle as NodeIcon,
  TrendingFlat as EdgeIcon,
  Timer as TimerIcon,
} from '@mui/icons-material';
import { useGraphStore } from '../../store/graphStore';

export function StatusBar() {
  const graphData = useGraphStore((state) => state.graphData);
  const lastQueryStats = useGraphStore((state) => state.lastQueryStats);

  const nodeCount = graphData?.nodes.length || 0;
  const edgeCount = graphData?.edges.length || 0;
  const executionTime = lastQueryStats?.executionTime || 0;

  return (
    <Box
      sx={{
        height: 40,
        display: 'flex',
        alignItems: 'center',
        px: 2,
        gap: 2,
        borderTop: 1,
        borderColor: 'divider',
        bgcolor: 'background.paper',
      }}
    >
      {/* 节点数量 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
        <NodeIcon sx={{ fontSize: 16, color: 'primary.main' }} />
        <Typography variant="body2" color="text.secondary">
          节点:
        </Typography>
        <Chip label={nodeCount} size="small" color="primary" variant="outlined" />
      </Box>

      <Divider orientation="vertical" flexItem />

      {/* 边数量 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
        <EdgeIcon sx={{ fontSize: 16, color: 'secondary.main' }} />
        <Typography variant="body2" color="text.secondary">
          关系:
        </Typography>
        <Chip label={edgeCount} size="small" color="secondary" variant="outlined" />
      </Box>

      <Divider orientation="vertical" flexItem />

      {/* 执行时间 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
        <TimerIcon sx={{ fontSize: 16, color: 'info.main' }} />
        <Typography variant="body2" color="text.secondary">
          执行时间:
        </Typography>
        <Chip
          label={`${executionTime.toFixed(3)}s`}
          size="small"
          color="info"
          variant="outlined"
        />
      </Box>

      {/* 右侧填充 */}
      <Box sx={{ flexGrow: 1 }} />

      {/* 状态信息 */}
      {graphData && (
        <Typography variant="caption" color="text.secondary">
          就绪
        </Typography>
      )}
    </Box>
  );
}
