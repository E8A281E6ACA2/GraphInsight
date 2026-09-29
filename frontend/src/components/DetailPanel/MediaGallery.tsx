import { useState } from 'react';
import {
  Box,
  Typography,
  ImageList,
  ImageListItem,
  Dialog,
  IconButton,
  Paper,
} from '@mui/material';
import {
  Close as CloseIcon,
  ZoomIn as ZoomInIcon,
  ZoomOut as ZoomOutIcon,
  Fullscreen as FullscreenIcon,
} from '@mui/icons-material';
import type { MediaResource } from '../../types/api';

interface MediaGalleryProps {
  images: MediaResource[];
}

export function MediaGallery({ images }: MediaGalleryProps) {
  const [selectedImage, setSelectedImage] = useState<MediaResource | null>(null);
  const [zoom, setZoom] = useState(1);

  const handleImageClick = (image: MediaResource) => {
    setSelectedImage(image);
    setZoom(1);
  };

  const handleClose = () => {
    setSelectedImage(null);
    setZoom(1);
  };

  const handleZoomIn = () => {
    setZoom((prev) => Math.min(prev + 0.25, 3));
  };

  const handleZoomOut = () => {
    setZoom((prev) => Math.max(prev - 0.25, 0.5));
  };

  const handleFullscreen = () => {
    if (selectedImage) {
      const img = document.getElementById('lightbox-image') as HTMLImageElement;
      if (img && img.requestFullscreen) {
        img.requestFullscreen();
      }
    }
  };

  if (images.length === 0) {
    return null;
  }

  return (
    <Box sx={{ mt: 2 }}>
      <Typography variant="subtitle2" fontWeight={600} gutterBottom>
        图片 ({images.length})
      </Typography>

      <ImageList
        sx={{ width: '100%', maxHeight: 400 }}
        cols={2}
        rowHeight={150}
        gap={8}
      >
        {images.map((image) => (
          <ImageListItem
            key={image.filename}
            sx={{
              cursor: 'pointer',
              '&:hover': {
                opacity: 0.8,
              },
            }}
            onClick={() => handleImageClick(image)}
          >
            <img
              src={image.thumbnail || image.url}
              alt={image.filename}
              loading="lazy"
              style={{
                width: '100%',
                height: '100%',
                objectFit: 'cover',
                borderRadius: 4,
              }}
            />
          </ImageListItem>
        ))}
      </ImageList>

      {/* 灯箱/模态框 */}
      <Dialog
        open={!!selectedImage}
        onClose={handleClose}
        maxWidth="lg"
        fullWidth
        PaperProps={{
          sx: {
            bgcolor: 'rgba(0, 0, 0, 0.9)',
            boxShadow: 'none',
          },
        }}
      >
        <Box
          sx={{
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            minHeight: 400,
            p: 2,
          }}
        >
          {/* 控制按钮 */}
          <Paper
            elevation={3}
            sx={{
              position: 'absolute',
              top: 16,
              right: 16,
              display: 'flex',
              gap: 1,
              p: 1,
              zIndex: 1,
            }}
          >
            <IconButton size="small" onClick={handleZoomIn}>
              <ZoomInIcon />
            </IconButton>
            <IconButton size="small" onClick={handleZoomOut}>
              <ZoomOutIcon />
            </IconButton>
            <IconButton size="small" onClick={handleFullscreen}>
              <FullscreenIcon />
            </IconButton>
            <IconButton size="small" onClick={handleClose}>
              <CloseIcon />
            </IconButton>
          </Paper>

          {/* 图片 */}
          {selectedImage && (
            <img
              id="lightbox-image"
              src={selectedImage.url}
              alt={selectedImage.filename}
              style={{
                maxWidth: '100%',
                maxHeight: '80vh',
                objectFit: 'contain',
                transform: `scale(${zoom})`,
                transition: 'transform 0.2s ease',
              }}
            />
          )}
        </Box>
      </Dialog>
    </Box>
  );
}
