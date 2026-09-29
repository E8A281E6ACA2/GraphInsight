"""
Neo4j 结果解析工具
"""
from typing import Dict, Any, List


def parse_node(node) -> Dict[str, Any]:
    """
    解析 Neo4j 节点为标准格式
    
    Args:
        node: Neo4j 节点对象
    
    Returns:
        标准化的节点字典
    """
    return {
        "id": str(node.id),
        "labels": list(node.labels),
        "properties": dict(node)
    }


def parse_relationship(rel) -> Dict[str, Any]:
    """
    解析 Neo4j 关系为标准格式
    
    Args:
        rel: Neo4j 关系对象
    
    Returns:
        标准化的关系字典
    """
    return {
        "id": str(rel.id),
        "source": str(rel.start_node.id),
        "target": str(rel.end_node.id),
        "type": rel.type,
        "properties": dict(rel)
    }


def extract_media_resources(properties: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    从节点属性中提取多媒体资源
    支持中英文属性名
    
    Args:
        properties: 节点属性字典
    
    Returns:
        包含多媒体资源的字典
    """
    media = {
        "images": [],
        "videos": [],
        "audios": []
    }
    
    # 定义属性名映射（支持中英文）
    image_keys = ["images", "image", "imageUrl", "图片", "图像", "照片"]
    video_keys = ["videos", "video", "videoUrl", "视频", "影片"]
    audio_keys = ["audios", "audio", "audioUrl", "音频", "音乐", "声音"]
    
    # 提取图片
    for key in image_keys:
        if key in properties:
            value = properties[key]
            if isinstance(value, list):
                # 过滤掉空值
                valid_images = [v for v in value if v and isinstance(v, str) and v.strip()]
                media["images"].extend(valid_images)
            elif isinstance(value, str) and value.strip():
                media["images"].append(value)
    
    # 提取视频
    for key in video_keys:
        if key in properties:
            value = properties[key]
            if isinstance(value, list):
                # 过滤掉空值
                valid_videos = [v for v in value if v and isinstance(v, str) and v.strip()]
                media["videos"].extend(valid_videos)
            elif isinstance(value, str) and value.strip():
                media["videos"].append(value)
    
    # 提取音频
    for key in audio_keys:
        if key in properties:
            value = properties[key]
            if isinstance(value, list):
                # 过滤掉空值
                valid_audios = [v for v in value if v and isinstance(v, str) and v.strip()]
                media["audios"].extend(valid_audios)
            elif isinstance(value, str) and value.strip():
                media["audios"].append(value)
    
    # 去重
    media["images"] = list(dict.fromkeys(media["images"]))  # 保持顺序去重
    media["videos"] = list(dict.fromkeys(media["videos"]))
    media["audios"] = list(dict.fromkeys(media["audios"]))
    
    return media
