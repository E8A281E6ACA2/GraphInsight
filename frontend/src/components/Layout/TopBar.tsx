import { AppBar, Toolbar, Typography, IconButton, Box, Tooltip, Button } from '@mui/material';
import {
  Brightness4 as DarkModeIcon,
  Brightness7 as LightModeIcon,
  FileDownload as ExportIcon,
  Help as HelpIcon,
  Dashboard as DashboardIcon,
} from '@mui/icons-material';
import { useNavigate } from 'react-router-dom';
import { useGraphStore } from '../../store/graphStore';
import { KnowledgeBaseSelector } from '../Workspace/KnowledgeBaseSelector';
import logoSvg from '../../assets/images/logo.svg';

interface TopBarProps {
  onExportClick?: () => void;
}

export function TopBar({ onExportClick }: TopBarProps) {
  const navigate = useNavigate();
  const isDarkMode = useGraphStore((state) => state.isDarkMode);
  const toggleTheme = useGraphStore((state) => state.toggleTheme);

  return (
    <AppBar position="static" elevation={1}>
      <Toolbar>
        {/* Logo 和标题 */}
        <Box sx={{ display: 'flex', alignItems: 'center', flexGrow: 1, minWidth: 0 }}>
          <img
            src={logoSvg}
            alt="GraphInsight Logo"
            style={{ height: 32, marginRight: 8 }}
          />
          <Typography variant="h6" component="div" sx={{ fontWeight: 600, flexShrink: 0 }}>
            GraphInsight
          </Typography>
          <Typography
            variant="body2"
            sx={{
              ml: 1.5,
              opacity: 0.8,
              display: { xs: 'none', md: 'block' },
              fontSize: '0.875rem',
              fontWeight: 400,
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            农业多模态知识图谱可视化分析系统
          </Typography>
        </Box>

        {/* 操作按钮 */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0, minWidth: 0 }}>
          {/* workspace 知识库选择器（M4-R1 步骤 3：所有业务调用按 activeKbId 作用域发起） */}
          <KnowledgeBaseSelector />
          <Button
            variant="outlined"
            color="inherit"
            startIcon={<DashboardIcon />}
            onClick={() => navigate('/admin/dashboard')}
            sx={{
              display: { xs: 'none', sm: 'inline-flex' },
              borderColor: 'rgba(15, 31, 45, 0.24)',
              color: 'text.primary',
              bgcolor: 'background.paper',
              '&:hover': {
                borderColor: 'primary.main',
                bgcolor: 'rgba(27, 127, 121, 0.08)',
              },
            }}
          >
            管理后台
          </Button>
          <Tooltip title="管理后台">
            <IconButton
              color="inherit"
              size="large"
              onClick={() => navigate('/admin/dashboard')}
              sx={{ display: { xs: 'inline-flex', sm: 'none' } }}
            >
              <DashboardIcon />
            </IconButton>
          </Tooltip>
          <Tooltip title="导出">
            <IconButton color="inherit" size="large" onClick={onExportClick}>
              <ExportIcon />
            </IconButton>
          </Tooltip>

          <Tooltip title={isDarkMode ? '切换到浅色模式' : '切换到深色模式'}>
            <IconButton color="inherit" onClick={toggleTheme} size="large">
              {isDarkMode ? <LightModeIcon /> : <DarkModeIcon />}
            </IconButton>
          </Tooltip>

          <Tooltip title="帮助">
            <IconButton color="inherit" size="large">
              <HelpIcon />
            </IconButton>
          </Tooltip>
        </Box>
      </Toolbar>
    </AppBar>
  );
}
