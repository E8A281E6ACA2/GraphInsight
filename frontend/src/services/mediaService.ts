import { api } from './api';
import { API_BASE_URL } from '../utils/apiBase';

// 获取媒体文件 URL
export function getMediaUrl(filename: string): string {
  const baseUrl = api.defaults.baseURL || API_BASE_URL;
  return `${baseUrl}/api/media/${filename}`;
}

// 预加载图片
export function preloadImage(url: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve();
    img.onerror = reject;
    img.src = url;
  });
}

// 批量预加载图片
export async function preloadImages(urls: string[]): Promise<void> {
  await Promise.all(urls.map(url => preloadImage(url)));
}

// 检查媒体文件是否存在
export async function checkMediaExists(filename: string): Promise<boolean> {
  try {
    await api.head(`/api/media/${filename}`);
    return true;
  } catch {
    return false;
  }
}
