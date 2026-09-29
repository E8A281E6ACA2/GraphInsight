"""
多媒体文件服务
"""
import os
from typing import Optional
from fastapi.responses import FileResponse
from fastapi import HTTPException
from config import get_settings

settings = get_settings()


class MediaService:
    """多媒体文件服务类"""
    
    def __init__(self):
        self.media_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            settings.media_storage_path
        )
        os.makedirs(self.media_dir, exist_ok=True)
    
    def get_file_path(self, filename: str) -> str:
        """
        获取文件完整路径
        
        Args:
            filename: 文件名
        
        Returns:
            文件完整路径
        """
        return os.path.join(self.media_dir, filename)
    
    def file_exists(self, filename: str) -> bool:
        """
        检查文件是否存在
        
        Args:
            filename: 文件名
        
        Returns:
            文件是否存在
        """
        file_path = self.get_file_path(filename)
        return os.path.isfile(file_path)
    
    def get_content_type(self, filename: str) -> str:
        """
        根据文件扩展名获取 Content-Type
        
        Args:
            filename: 文件名
        
        Returns:
            Content-Type
        """
        ext = filename.lower().split('.')[-1]
        
        content_types = {
            # 图片
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
            'gif': 'image/gif',
            'webp': 'image/webp',
            'svg': 'image/svg+xml',
            
            # 视频
            'mp4': 'video/mp4',
            'webm': 'video/webm',
            'ogg': 'video/ogg',
            'avi': 'video/x-msvideo',
            'mov': 'video/quicktime',
            
            # 音频
            'mp3': 'audio/mpeg',
            'wav': 'audio/wav',
            'ogg': 'audio/ogg',
            'flac': 'audio/flac',
            'm4a': 'audio/mp4',
            
            # 文档
            'pdf': 'application/pdf',
            'doc': 'application/msword',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        }
        
        return content_types.get(ext, 'application/octet-stream')
    
    def serve_file(self, filename: str) -> FileResponse:
        """
        提供文件服务
        
        Args:
            filename: 文件名
        
        Returns:
            FileResponse
        
        Raises:
            HTTPException: 文件不存在时抛出
        """
        if not self.file_exists(filename):
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "File not found",
                    "code": "FILE_NOT_FOUND",
                    "message": f"Media file '{filename}' does not exist"
                }
            )
        
        file_path = self.get_file_path(filename)
        content_type = self.get_content_type(filename)
        
        return FileResponse(
            path=file_path,
            media_type=content_type,
            filename=filename
        )


# 全局媒体服务实例
_media_service: Optional[MediaService] = None


def get_media_service() -> MediaService:
    """获取媒体服务单例"""
    global _media_service
    if _media_service is None:
        _media_service = MediaService()
    return _media_service
