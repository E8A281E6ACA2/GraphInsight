import { Box, Typography, Chip, Paper } from '@mui/material';
import {
  Circle as NodeIcon,
  TrendingFlat as EdgeIcon,
  Timer as TimerIcon,
  CheckCircle as SuccessIcon,
} from '@mui/icons-material';
import { useGraphStore } from '../../store/graphStore';

export function QueryStats() {
  const graphData = useGraphStore((state) => state.graphData);
  const lastQueryStats = useGraphStore((state) => state.lastQueryStats);

  const nodeCount = graphData?.nodes.length || 0;
  const edgeCount = graphData?.edges.length || 0;
  const executionTime = lastQueryStats?.executionTime || 0;

  const hasData = graphData !== null;

  return (
    <Paper
      elevation={0}
      sx={{
        p: 2,
        border: 1,
        borderColor: 'divider',
        borderRadius: 1,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 2 }}>
        <SuccessIcon color={hasData ? 'success' : 'disabled'} fontSize="small" />
        <Typography variant="subtitle2">查询结果统计</Typography>
      </Box>

      {hasData ? (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
          {/* 节点数量 */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <NodeIcon sx={{ fontSize: 20, color: 'primary.main' }} />
            <Typography variant="body2" sx={{ flex: 1 }}>
              节点数量
            </Typography>
            <Chip
              label={nodeCount}
              size="small"
              color="primary"
              sx={{ minWidth: 60, fontWeight: 600 }}
            />
          </Box>

          {/* 边数量 */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <EdgeIcon sx={{ fontSize: 20, color: 'secondary.main' }} />
            <Typography variant="body2" sx={{ flex: 1 }}>
              关系数量
            </Typography>
            <Chip
              label={edgeCount}
              size="small"
              color="secondary"
              sx={{ minWidth: 60, fontWeight: 600 }}
            />
          </Box>

          {/* 执行时间 */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <TimerIcon sx={{ fontSize: 20, color: 'info.main' }} />
            <Typography variant="body2" sx={{ flex: 1 }}>
              执行时间
            </Typography>
            <Chip
              label={`${executionTime.toFixed(3)}s`}
              size="small"
              color="info"
              sx={{ minWidth: 60, fontWeight: 600 }}
            />
          </Box>

          {/* 总计 */}
          <Box
            sx={{
              mt: 1,
              pt: 1.5,
              borderTop: 1,
              borderColor: 'divider',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <Typography variant="body2" fontWeight={600}>
              总计
            </Typography>
            <Chip
              label={`${nodeCount + edgeCount} 项`}
              size="small"
              color="success"
              variant="outlined"
              sx={{ fontWeight: 600 }}
            />
          </Box>
        </Box>
      ) : (
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            py: 2,
            color: 'text.secondary',
          }}
        >
          <Typography variant="body2">执行查询后显示统计信息</Typography>
        </Box>
      )}
    </Paper>
  );
}
