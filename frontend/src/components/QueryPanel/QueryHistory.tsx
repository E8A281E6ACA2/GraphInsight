import {
  Box,
  Typography,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  IconButton,
  Chip,
  Tooltip,
  Divider,
} from '@mui/material';
import {
  Delete as DeleteIcon,
  History as HistoryIcon,
  AccessTime as TimeIcon,
} from '@mui/icons-material';
import { useGraphStore } from '../../store/graphStore';
import { useCypher } from '../../hooks/useCypher';

export function QueryHistory() {
  const queryHistory = useGraphStore((state) => state.queryHistory);
  const clearQueryHistory = useGraphStore((state) => state.clearQueryHistory);
  const { execute } = useCypher();

  const handleReExecute = async (cypher: string) => {
    await execute(cypher);
  };

  const formatTimestamp = (timestamp: number) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return '刚刚';
    if (diffMins < 60) return `${diffMins} 分钟前`;
    if (diffHours < 24) return `${diffHours} 小时前`;
    if (diffDays < 7) return `${diffDays} 天前`;
    return date.toLocaleDateString('zh-CN');
  };

  const truncateQuery = (query: string, maxLength: number = 60) => {
    const singleLine = query.replace(/\s+/g, ' ').trim();
    if (singleLine.length <= maxLength) return singleLine;
    return singleLine.substring(0, maxLength) + '...';
  };

  if (queryHistory.length === 0) {
    return (
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          py: 4,
          color: 'text.secondary',
        }}
      >
        <HistoryIcon sx={{ fontSize: 48, mb: 1, opacity: 0.5 }} />
        <Typography variant="body2">暂无查询历史</Typography>
      </Box>
    );
  }

  return (
    <Box>
      {/* 标题和清除按钮 */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          mb: 1,
        }}
      >
        <Typography variant="subtitle2" color="text.secondary">
          查询历史 ({queryHistory.length})
        </Typography>
        <Tooltip title="清除所有历史">
          <IconButton size="small" onClick={clearQueryHistory}>
            <DeleteIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Box>

      <Divider sx={{ mb: 1 }} />

      {/* 历史列表 */}
      <List dense sx={{ maxHeight: 300, overflow: 'auto' }}>
        {queryHistory.map((item) => (
          <ListItem
            key={item.id}
            disablePadding
            sx={{
              mb: 0.5,
              border: 1,
              borderColor: 'divider',
              borderRadius: 1,
              bgcolor: 'background.paper',
            }}
          >
            <ListItemButton
              onClick={() => handleReExecute(item.cypher)}
              sx={{ py: 1 }}
            >
              <ListItemText
                primary={
                  <Typography variant="body2" sx={{ fontFamily: 'monospace', fontWeight: 'normal' }}>
                    {truncateQuery(item.cypher)}
                  </Typography>
                }
                secondary={
                  <Box
                    sx={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 1,
                      mt: 0.5,
                    }}
                  >
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                      <TimeIcon sx={{ fontSize: 12 }} />
                      <Typography variant="caption">
                        {formatTimestamp(item.timestamp)}
                      </Typography>
                    </Box>
                    <Chip
                      label={`${item.resultCount} 结果`}
                      size="small"
                      variant="outlined"
                      sx={{ height: 18, fontSize: '0.7rem' }}
                    />
                  </Box>
                }
                secondaryTypographyProps={{ component: 'div' }}
              />
            </ListItemButton>
          </ListItem>
        ))}
      </List>
    </Box>
  );
}
