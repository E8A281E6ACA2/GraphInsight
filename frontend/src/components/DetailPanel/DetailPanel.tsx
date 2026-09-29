import { Box, Typography, Divider } from '@mui/material';
import { useGraphStore } from '../../store/graphStore';
import { useNodeDetail } from '../../hooks/useNodeDetail';
import { NodeDetail } from './NodeDetail';
import { MediaGallery } from './MediaGallery';
import { VideoPlayer } from './VideoPlayer';
import { AudioPlayer } from './AudioPlayer';

export function DetailPanel() {
  const selectedNodeId = useGraphStore((state) => state.selectedNodeId);
  const { nodeDetail, isLoading, error } = useNodeDetail(selectedNodeId);

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflow: 'hidden',
      }}
    >
      {/* 标题 */}
      <Box sx={{ p: 2, pb: 1 }}>
        <Typography variant="h6" fontWeight={600}>
          节点详情
        </Typography>
      </Box>

      <Divider />

      {/* 主内容区域 - 可滚动 */}
      <Box
        sx={{
          flex: 1,
          overflow: 'auto',
        }}
      >
        {/* 节点基本信息 */}
        <NodeDetail nodeDetail={nodeDetail} isLoading={isLoading} error={error} />

        {/* 多媒体内容 */}
        {nodeDetail && !isLoading && !error && (
          <Box sx={{ px: 2, pb: 2 }}>
            {/* 图片画廊 */}
            {nodeDetail.media.images.length > 0 && (
              <>
                <Divider sx={{ my: 2 }} />
                <MediaGallery images={nodeDetail.media.images} />
              </>
            )}

            {/* 视频播放器 */}
            {nodeDetail.media.videos.length > 0 && (
              <>
                <Divider sx={{ my: 2 }} />
                <VideoPlayer videos={nodeDetail.media.videos} />
              </>
            )}

            {/* 音频播放器 */}
            {nodeDetail.media.audios.length > 0 && (
              <>
                <Divider sx={{ my: 2 }} />
                <AudioPlayer audios={nodeDetail.media.audios} />
              </>
            )}
          </Box>
        )}
      </Box>
    </Box>
  );
}
