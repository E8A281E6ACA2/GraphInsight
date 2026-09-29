import { useState } from 'react';
import {
  Box,
  Typography,
  Card,
  CardMedia,
  CardContent,
  IconButton,
  Collapse,
} from '@mui/material';
import {
  PlayArrow as PlayIcon,
  ExpandMore as ExpandIcon,
} from '@mui/icons-material';
import type { MediaResource } from '../../types/api';

interface VideoPlayerProps {
  videos: MediaResource[];
}

export function VideoPlayer({ videos }: VideoPlayerProps) {
  const [expandedVideo, setExpandedVideo] = useState<string | null>(null);

  const handleToggle = (filename: string) => {
    setExpandedVideo(expandedVideo === filename ? null : filename);
  };

  if (videos.length === 0) {
    return null;
  }

  return (
    <Box sx={{ mt: 2 }}>
      <Typography variant="subtitle2" fontWeight={600} gutterBottom>
        视频 ({videos.length})
      </Typography>

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {videos.map((video) => (
          <Card key={video.filename} variant="outlined">
            {/* 缩略图 */}
            <Box sx={{ position: 'relative' }}>
              {video.thumbnail ? (
                <CardMedia
                  component="img"
                  height="140"
                  image={video.thumbnail}
                  alt={video.filename}
                  sx={{ cursor: 'pointer' }}
                  onClick={() => handleToggle(video.filename)}
                />
              ) : (
                <Box
                  sx={{
                    height: 140,
                    bgcolor: 'action.hover',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    cursor: 'pointer',
                  }}
                  onClick={() => handleToggle(video.filename)}
                >
                  <PlayIcon sx={{ fontSize: 48, color: 'text.secondary' }} />
                </Box>
              )}

              {/* 播放按钮覆盖层 */}
              <IconButton
                sx={{
                  position: 'absolute',
                  top: '50%',
                  left: '50%',
                  transform: 'translate(-50%, -50%)',
                  bgcolor: 'rgba(0, 0, 0, 0.6)',
                  '&:hover': {
                    bgcolor: 'rgba(0, 0, 0, 0.8)',
                  },
                }}
                onClick={() => handleToggle(video.filename)}
              >
                <PlayIcon sx={{ color: 'white', fontSize: 32 }} />
              </IconButton>
            </Box>

            {/* 视频信息 */}
            <CardContent>
              <Box
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                }}
              >
                <Typography variant="body2" noWrap>
                  {video.filename}
                </Typography>
                {video.duration && (
                  <Typography variant="caption" color="text.secondary">
                    {formatDuration(video.duration)}
                  </Typography>
                )}
              </Box>

              {/* 展开按钮 */}
              <IconButton
                size="small"
                onClick={() => handleToggle(video.filename)}
                sx={{
                  transform:
                    expandedVideo === video.filename
                      ? 'rotate(180deg)'
                      : 'rotate(0deg)',
                  transition: 'transform 0.2s',
                }}
              >
                <ExpandIcon />
              </IconButton>
            </CardContent>

            {/* 视频播放器 */}
            <Collapse in={expandedVideo === video.filename}>
              <Box sx={{ p: 2, pt: 0 }}>
                <video
                  controls
                  style={{
                    width: '100%',
                    maxHeight: 400,
                    borderRadius: 4,
                  }}
                  src={video.url}
                >
                  您的浏览器不支持视频播放。
                </video>
              </Box>
            </Collapse>
          </Card>
        ))}
      </Box>
    </Box>
  );
}

// 格式化时长（秒 -> MM:SS）
function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}
