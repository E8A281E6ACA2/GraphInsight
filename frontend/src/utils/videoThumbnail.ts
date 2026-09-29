/**
 * 视频缩略图生成工具
 */

// 缓存生成的缩略图
const thumbnailCache = new Map<string, string>();

/**
 * 从视频URL生成缩略图
 */
export async function generateVideoThumbnail(videoUrl: string): Promise<string> {
  // 检查缓存
  if (thumbnailCache.has(videoUrl)) {
    return thumbnailCache.get(videoUrl)!;
  }

  try {
    // 创建视频元素
    const video = document.createElement('video');
    video.crossOrigin = 'anonymous';
    video.muted = true;
    video.preload = 'metadata';
    
    return new Promise((resolve, reject) => {
      video.onloadedmetadata = () => {
        // 设置到视频的第1秒（或5%位置，更早获取帧）
        video.currentTime = Math.min(0.5, video.duration * 0.05);
      };

      video.onseeked = () => {
        try {
          // 创建canvas来捕获视频帧
          const canvas = document.createElement('canvas');
          const ctx = canvas.getContext('2d');
          
          if (!ctx) {
            reject(new Error('Cannot get canvas context'));
            return;
          }

          // 设置canvas尺寸
          canvas.width = 90;
          canvas.height = 90;

          // 计算视频的宽高比，保持比例
          const videoAspect = video.videoWidth / video.videoHeight;
          let drawWidth = canvas.width;
          let drawHeight = canvas.height;
          let offsetX = 0;
          let offsetY = 0;

          if (videoAspect > 1) {
            // 视频更宽，以高度为准
            drawHeight = canvas.height;
            drawWidth = drawHeight * videoAspect;
            offsetX = (canvas.width - drawWidth) / 2;
          } else {
            // 视频更高，以宽度为准
            drawWidth = canvas.width;
            drawHeight = drawWidth / videoAspect;
            offsetY = (canvas.height - drawHeight) / 2;
          }

          // 绘制黑色背景
          ctx.fillStyle = '#000000';
          ctx.fillRect(0, 0, canvas.width, canvas.height);

          // 绘制视频帧
          ctx.drawImage(video, offsetX, offsetY, drawWidth, drawHeight);

          // 添加半透明播放图标叠加
          ctx.fillStyle = 'rgba(0, 0, 0, 0.4)';
          ctx.fillRect(0, 0, canvas.width, canvas.height);

          // 绘制播放按钮
          ctx.fillStyle = 'rgba(255, 255, 255, 0.95)';
          ctx.beginPath();
          ctx.arc(45, 45, 18, 0, 2 * Math.PI);
          ctx.fill();

          // 播放按钮边框
          ctx.strokeStyle = 'rgba(0, 0, 0, 0.2)';
          ctx.lineWidth = 1;
          ctx.stroke();

          // 播放三角形
          ctx.fillStyle = '#1976d2';
          ctx.beginPath();
          ctx.moveTo(38, 35);
          ctx.lineTo(38, 55);
          ctx.lineTo(58, 45);
          ctx.closePath();
          ctx.fill();

          // 转换为data URL
          const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
          
          // 缓存结果
          thumbnailCache.set(videoUrl, dataUrl);
          
          resolve(dataUrl);
        } catch (error) {
          reject(error);
        }
      };

      video.onerror = () => {
        // 如果视频加载失败，返回默认图标
        const fallbackSvg = `data:image/svg+xml;base64,${btoa(`
          <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80">
            <rect width="80" height="80" fill="#1976d2" rx="8"/>
            <rect x="0" y="15" width="80" height="2" fill="#ffffff" opacity="0.3"/>
            <rect x="0" y="25" width="80" height="2" fill="#ffffff" opacity="0.3"/>
            <rect x="0" y="35" width="80" height="2" fill="#ffffff" opacity="0.3"/>
            <rect x="0" y="45" width="80" height="2" fill="#ffffff" opacity="0.3"/>
            <rect x="0" y="55" width="80" height="2" fill="#ffffff" opacity="0.3"/>
            <rect x="0" y="65" width="80" height="2" fill="#ffffff" opacity="0.3"/>
            <circle cx="40" cy="40" r="15" fill="rgba(255,255,255,0.9)"/>
            <polygon points="35,30 35,50 50,40" fill="#1976d2"/>
            <text x="40" y="72" text-anchor="middle" fill="white" font-size="8" font-family="Arial">VIDEO</text>
          </svg>
        `)}`;
        
        thumbnailCache.set(videoUrl, fallbackSvg);
        resolve(fallbackSvg);
      };

      // 开始加载视频
      video.src = videoUrl;
      video.load();
    });
  } catch (error) {
    console.error('Error generating video thumbnail:', error);
    
    // 返回默认图标
    const fallbackSvg = `data:image/svg+xml;base64,${btoa(`
      <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80">
        <rect width="80" height="80" fill="#1976d2" rx="8"/>
        <circle cx="40" cy="40" r="15" fill="rgba(255,255,255,0.9)"/>
        <polygon points="35,30 35,50 50,40" fill="#1976d2"/>
        <text x="40" y="72" text-anchor="middle" fill="white" font-size="8">VIDEO</text>
      </svg>
    `)}`;
    
    return fallbackSvg;
  }
}

/**
 * 清除缩略图缓存
 */
export function clearThumbnailCache(): void {
  thumbnailCache.clear();
}