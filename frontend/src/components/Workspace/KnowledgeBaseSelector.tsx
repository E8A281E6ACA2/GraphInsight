import { useEffect, useState } from 'react';
import {
  Box,
  CircularProgress,
  IconButton,
  MenuItem,
  Select,
  Tooltip,
  Typography,
} from '@mui/material';
import { Refresh as RefreshIcon } from '@mui/icons-material';
import type { SelectChangeEvent } from '@mui/material';
import { listWorkspaceKnowledgeBases } from '../../services/kbService';
import type { WorkspaceKnowledgeBase } from '../../types/api';
import { useGraphStore } from '../../store/graphStore';

type CatalogState = 'loading' | 'empty' | 'error' | 'success';

// workspace 知识库选择器（M4-R1 步骤 3）：
// 挂载时拉取"当前用户可访问的 active KB"目录（GET /api/knowledge-bases），
// 选择后写入 graphStore.activeKbId，后续所有业务调用按该作用域发起。
export function KnowledgeBaseSelector() {
  const [knowledgeBases, setKnowledgeBases] = useState<WorkspaceKnowledgeBase[]>([]);
  const [state, setState] = useState<CatalogState>('loading');
  const [reloadKey, setReloadKey] = useState(0);
  const activeKbId = useGraphStore((state) => state.activeKbId);
  const setActiveKbId = useGraphStore((state) => state.setActiveKbId);

  useEffect(() => {
    let cancelled = false;
    setState('loading');
    listWorkspaceKnowledgeBases()
      .then((items) => {
        if (cancelled) return;
        setKnowledgeBases(items);
        setState(items.length === 0 ? 'empty' : 'success');
        // 持久化的 activeKbId 可能指向已归档/已删除/不在授权集合内的 KB：
        // 目录里没有就清空重选；仅一个 KB 时自动选中，减少操作成本。
        const stillValid = items.some((item) => item.kb_id === activeKbId);
        if (!stillValid) {
          setActiveKbId(items.length === 1 ? items[0].kb_id : null);
        }
      })
      .catch(() => {
        if (!cancelled) setState('error');
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey]);

  const handleChange = (event: SelectChangeEvent<string>) => {
    setActiveKbId(event.target.value || null);
  };

  if (state === 'loading') {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1 }} data-testid="kb-selector-loading">
        <CircularProgress size={16} />
        <Typography variant="body2" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>
          加载知识库...
        </Typography>
      </Box>
    );
  }

  if (state === 'empty') {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1 }} data-testid="kb-selector-empty">
        <Typography variant="body2" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>
          暂无可访问知识库
        </Typography>
        <Tooltip title="重新加载">
          <IconButton size="small" onClick={() => setReloadKey((key) => key + 1)}>
            <RefreshIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Box>
    );
  }

  if (state === 'error') {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1 }} data-testid="kb-selector-error">
        <Typography variant="body2" sx={{ color: 'error.main', whiteSpace: 'nowrap' }}>
          知识库目录加载失败
        </Typography>
        <Tooltip title="重试">
          <IconButton size="small" onClick={() => setReloadKey((key) => key + 1)}>
            <RefreshIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Box>
    );
  }

  return (
    <Box sx={{ display: 'flex', alignItems: 'center', minWidth: 0 }} data-testid="knowledge-base-selector">
      <Typography variant="body2" sx={{ mr: 1, color: 'text.secondary', whiteSpace: 'nowrap' }}>
        知识库
      </Typography>
      <Select
        size="small"
        value={activeKbId && knowledgeBases.some((item) => item.kb_id === activeKbId) ? activeKbId : ''}
        onChange={handleChange}
        displayEmpty
        aria-label="选择知识库"
        sx={{
          minWidth: 180,
          maxWidth: 280,
          height: 36,
          bgcolor: 'background.paper',
          borderRadius: 1,
          '& .MuiSelect-select': {
            pr: 1.5,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          },
        }}
        renderValue={(value) =>
          value ? (
            <Box sx={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {knowledgeBases.find((item) => item.kb_id === value)?.name ?? String(value)}
            </Box>
          ) : (
            <Typography variant="body2" sx={{ color: 'text.secondary', fontStyle: 'italic' }}>
              请选择知识库
            </Typography>
          )
        }
      >
        {knowledgeBases.map((item) => (
          <MenuItem key={item.kb_id} value={item.kb_id} data-testid={`kb-option-${item.kb_id}`}>
            <Box sx={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {item.name}
              <Typography variant="caption" sx={{ ml: 1, color: 'text.secondary' }}>
                {item.project_id}
              </Typography>
            </Box>
          </MenuItem>
        ))}
      </Select>
    </Box>
  );
}
