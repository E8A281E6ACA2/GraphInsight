import { useState } from 'react';
import { Box, IconButton, Tooltip } from '@mui/material';
import {
  ChevronLeft as CollapseLeftIcon,
  ChevronRight as ExpandLeftIcon,
} from '@mui/icons-material';
import { TopBar } from './TopBar';
import { StatusBar } from './StatusBar';

interface AppLayoutProps {
  queryPanel: React.ReactNode;
  graphCanvas: React.ReactNode;
  detailPanel: React.ReactNode;
  onExportClick?: () => void;
}

export function AppLayout({ queryPanel, graphCanvas, detailPanel, onExportClick }: AppLayoutProps) {
  const [leftPanelWidth, setLeftPanelWidth] = useState(360);
  const [rightPanelWidth, setRightPanelWidth] = useState(360);
  const [isLeftCollapsed, setIsLeftCollapsed] = useState(false);
  const [isRightCollapsed] = useState(false); // 暂时不使用右侧折叠功能

  const minPanelWidth = 300;
  const maxPanelWidth = 460;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100vh', minWidth: 0, overflow: 'hidden' }}>
      {/* 顶部工具栏 */}
      <TopBar onExportClick={onExportClick} />

      {/* 主内容区域 */}
      <Box sx={{ display: 'flex', flex: 1, minHeight: 0, minWidth: 0, overflow: 'hidden' }}>
        {/* 左侧面板 - 问答面板 */}
        <Box
          sx={{
            width: isLeftCollapsed ? 0 : leftPanelWidth,
            minWidth: isLeftCollapsed ? 0 : minPanelWidth,
            maxWidth: isLeftCollapsed ? 0 : maxPanelWidth,
            flexShrink: 0,
            transition: 'width 0.2s ease',
            overflow: 'hidden',
            display: 'flex',
            flexDirection: 'column',
            bgcolor: 'background.paper',
            borderRight: 1,
            borderColor: 'divider',
            '@media (max-width: 1100px)': {
              width: isLeftCollapsed ? 0 : 320,
              minWidth: isLeftCollapsed ? 0 : 320,
            },
            '@media (max-width: 860px)': {
              display: isLeftCollapsed ? 'none' : 'flex',
              position: 'absolute',
              inset: '64px auto 40px 0',
              zIndex: 20,
              width: 340,
              minWidth: 340,
              boxShadow: 4,
            },
          }}
        >
          {queryPanel}
        </Box>

        {/* 左侧折叠按钮 */}
        {!isLeftCollapsed && (
          <Box
            sx={{
              width: 4,
              flexShrink: 0,
              cursor: 'col-resize',
              bgcolor: 'divider',
              '&:hover': {
                bgcolor: 'primary.main',
              },
            }}
            onMouseDown={(e) => {
              e.preventDefault();
              const startX = e.clientX;
              const startWidth = leftPanelWidth;

              const handleMouseMove = (e: MouseEvent) => {
                const newWidth = Math.max(
                  minPanelWidth,
                  Math.min(maxPanelWidth, startWidth + e.clientX - startX)
                );
                setLeftPanelWidth(newWidth);
              };

              const handleMouseUp = () => {
                document.removeEventListener('mousemove', handleMouseMove);
                document.removeEventListener('mouseup', handleMouseUp);
              };

              document.addEventListener('mousemove', handleMouseMove);
              document.addEventListener('mouseup', handleMouseUp);
            }}
          />
        )}

        <Box sx={{ position: 'relative', flexShrink: 0 }}>
          <Tooltip title={isLeftCollapsed ? '展开问答面板' : '折叠问答面板'}>
            <IconButton
              size="small"
              onClick={() => setIsLeftCollapsed(!isLeftCollapsed)}
              sx={{
                position: 'absolute',
                left: 8,
                top: 8,
                zIndex: 1000,
                bgcolor: 'background.paper',
                boxShadow: 1,
                '&:hover': {
                  bgcolor: 'background.paper',
                },
              }}
            >
              {isLeftCollapsed ? <ExpandLeftIcon /> : <CollapseLeftIcon />}
            </IconButton>
          </Tooltip>
        </Box>

        {/* 中间画布区域 */}
        <Box
          sx={{
            flex: 1,
            minWidth: 0,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            bgcolor: 'background.default',
          }}
        >
          {graphCanvas}
        </Box>

        {/* 右侧调整器 */}
        {!isRightCollapsed && (
          <Box
            sx={{
              width: 4,
              flexShrink: 0,
              cursor: 'col-resize',
              bgcolor: 'divider',
              '&:hover': {
                bgcolor: 'primary.main',
              },
            }}
            onMouseDown={(e) => {
              e.preventDefault();
              const startX = e.clientX;
              const startWidth = rightPanelWidth;

              const handleMouseMove = (e: MouseEvent) => {
                const newWidth = Math.max(
                  minPanelWidth,
                  Math.min(maxPanelWidth, startWidth - (e.clientX - startX))
                );
                setRightPanelWidth(newWidth);
              };

              const handleMouseUp = () => {
                document.removeEventListener('mousemove', handleMouseMove);
                document.removeEventListener('mouseup', handleMouseUp);
              };

              document.addEventListener('mousemove', handleMouseMove);
              document.addEventListener('mouseup', handleMouseUp);
            }}
          />
        )}

        {/* 右侧面板 - 详情面板 */}
        <Box
          data-testid="workspace-right-panel"
          sx={{
            width: isRightCollapsed ? 0 : rightPanelWidth,
            minWidth: isRightCollapsed ? 0 : minPanelWidth,
            maxWidth: isRightCollapsed ? 0 : maxPanelWidth,
            flexShrink: 0,
            transition: 'width 0.2s ease',
            overflow: 'hidden',
            display: 'flex',
            flexDirection: 'column',
            bgcolor: 'background.paper',
            borderLeft: 1,
            borderColor: 'divider',
            '@media (max-width: 1280px)': {
              width: isRightCollapsed ? 0 : 340,
              minWidth: isRightCollapsed ? 0 : 340,
              maxWidth: isRightCollapsed ? 0 : 380,
              display: 'flex',
            },
            '@media (max-width: 1080px)': {
              width: isRightCollapsed ? 0 : 320,
              minWidth: isRightCollapsed ? 0 : 320,
              maxWidth: isRightCollapsed ? 0 : 340,
            },
          }}
        >
          {detailPanel}
        </Box>
      </Box>

      {/* 底部状态栏 */}
      <StatusBar />
    </Box>
  );
}
