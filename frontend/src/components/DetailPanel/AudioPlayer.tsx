import { useState, useRef, useEffect } from 'react';
import {
  Box,
  Typography,
  Card,
  CardContent,
  IconButton,
  Slider,
  LinearProgress,
} from '@mui/material';
import {
  PlayArrow as PlayIcon,
  Pause as PauseIcon,
  VolumeUp as VolumeIcon,
  VolumeOff as MuteIcon,
} from '@mui/icons-material';
import type { MediaResource } from '../../types/api';

interface AudioPlayerProps {
  audios: MediaResource[];
}

export function AudioPlayer({ audios }: AudioPlayerProps) {
  if (audios.length === 0) {
    return null;
  }

  return (
    <Box sx={{ mt: 2 }}>
      <Typography variant="subtitle2" fontWeight={600} gutterBottom>
        音频 ({audios.length})
      </Typography>

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {audios.map((audio) => (
          <AudioPlayerItem key={audio.filename} audio={audio} />
        ))}
      </Box>
    </Box>
  );
}

interface AudioPlayerItemProps {
  audio: MediaResource;
}

function AudioPlayerItem({ audio }: AudioPlayerItemProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [isMuted, setIsMuted] = useState(false);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    const audioElement = audioRef.current;
    if (!audioElement) return;

    const handleLoadedMetadata = () => {
      setDuration(audioElement.duration);
      setIsLoading(false);
    };

    const handleTimeUpdate = () => {
      setCurrentTime(audioElement.currentTime);
    };

    const handleEnded = () => {
      setIsPlaying(false);
      setCurrentTime(0);
    };

    const handleLoadStart = () => {
      setIsLoading(true);
    };

    audioElement.addEventListener('loadedmetadata', handleLoadedMetadata);
    audioElement.addEventListener('timeupdate', handleTimeUpdate);
    audioElement.addEventListener('ended', handleEnded);
    audioElement.addEventListener('loadstart', handleLoadStart);

    return () => {
      audioElement.removeEventListener('loadedmetadata', handleLoadedMetadata);
      audioElement.removeEventListener('timeupdate', handleTimeUpdate);
      audioElement.removeEventListener('ended', handleEnded);
      audioElement.removeEventListener('loadstart', handleLoadStart);
    };
  }, []);

  const handlePlayPause = () => {
    const audioElement = audioRef.current;
    if (!audioElement) return;

    if (isPlaying) {
      audioElement.pause();
    } else {
      audioElement.play();
    }
    setIsPlaying(!isPlaying);
  };

  const handleSeek = (_event: Event, value: number | number[]) => {
    const audioElement = audioRef.current;
    if (!audioElement) return;

    const newTime = value as number;
    audioElement.currentTime = newTime;
    setCurrentTime(newTime);
  };

  const handleVolumeChange = (_event: Event, value: number | number[]) => {
    const audioElement = audioRef.current;
    if (!audioElement) return;

    const newVolume = value as number;
    audioElement.volume = newVolume;
    setVolume(newVolume);
    setIsMuted(newVolume === 0);
  };

  const handleMuteToggle = () => {
    const audioElement = audioRef.current;
    if (!audioElement) return;

    if (isMuted) {
      audioElement.volume = volume || 0.5;
      setIsMuted(false);
    } else {
      audioElement.volume = 0;
      setIsMuted(true);
    }
  };

  return (
    <Card variant="outlined">
      <CardContent>
        {/* 音频文件名 */}
        <Typography variant="body2" gutterBottom noWrap>
          {audio.filename}
        </Typography>

        {/* 加载进度 */}
        {isLoading && <LinearProgress sx={{ mb: 2 }} />}

        {/* 播放控制 */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
          <IconButton onClick={handlePlayPause} size="small">
            {isPlaying ? <PauseIcon /> : <PlayIcon />}
          </IconButton>

          {/* 时间显示 */}
          <Typography variant="caption" color="text.secondary">
            {formatTime(currentTime)}
          </Typography>

          {/* 进度条 */}
          <Slider
            size="small"
            value={currentTime}
            max={duration || 100}
            onChange={handleSeek}
            sx={{ flex: 1 }}
          />

          {/* 总时长 */}
          <Typography variant="caption" color="text.secondary">
            {formatTime(duration)}
          </Typography>
        </Box>

        {/* 音量控制 */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <IconButton onClick={handleMuteToggle} size="small">
            {isMuted ? <MuteIcon fontSize="small" /> : <VolumeIcon fontSize="small" />}
          </IconButton>

          <Slider
            size="small"
            value={isMuted ? 0 : volume}
            max={1}
            step={0.1}
            onChange={handleVolumeChange}
            sx={{ width: 100 }}
          />
        </Box>

        {/* 隐藏的 audio 元素 */}
        <audio ref={audioRef} src={audio.url} preload="metadata" />
      </CardContent>
    </Card>
  );
}

// 格式化时间（秒 -> MM:SS）
function formatTime(seconds: number): string {
  if (!seconds || !isFinite(seconds)) return '00:00';
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}
