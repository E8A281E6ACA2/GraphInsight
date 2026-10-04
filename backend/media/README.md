# 多媒体文件存储目录

此目录用于存储知识图谱节点关联的多媒体文件。

## 支持的文件类型

### 图片
- JPG/JPEG
- PNG
- GIF
- WebP
- SVG

### 视频
- MP4
- WebM
- OGG
- AVI
- MOV

### 音频
- MP3
- WAV
- OGG
- FLAC
- M4A

## 使用方法

1. 将多媒体文件放置在此目录
2. 在 Neo4j 节点属性中引用文件名，例如：
   ```cypher
   CREATE (n:Crop {
     name: "水稻",
     images: ["rice_plant.jpg", "rice_field.jpg"],
     videos: ["rice_growth.mp4"],
     audios: ["expert_intro.mp3"]
   })
   ```
3. 前端通过 `/api/media/{filename}` 访问文件；统一后端模式下该入口由 Go 对外提供，Python 仅在兼容直调模式下可选挂载同一路径。
