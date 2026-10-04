# 前端资源管理指南

## 目录结构

```
src/assets/
├── images/          # 图片资源
│   ├── logo.svg     # 应用Logo
│   ├── icons/       # 图标文件
│   └── backgrounds/ # 背景图片
├── fonts/           # 字体文件
├── videos/          # 本地视频文件
├── audios/          # 本地音频文件
└── data/            # 静态数据文件
```

## 使用方式

### 1. 在组件中引用图片
```tsx
import logo from '@/assets/images/logo.svg';

function Header() {
  return <img src={logo} alt="GraphInsight Logo" />;
}
```

### 2. 在CSS中引用
```css
.header {
  background-image: url('@/assets/images/background.jpg');
}
```

### 3. 动态引用
```tsx
const logoUrl = new URL('@/assets/images/logo.svg', import.meta.url).href;
```

## 最佳实践

### 应该放在 `src/assets/` 的资源：
- **应用Logo和品牌图片**
- **UI图标和装饰图片**
- **默认头像、占位图**
- **本地字体文件**
- **静态配置数据**

### 不应该放在 `src/assets/` 的资源：
- **用户上传的内容** → 后端 `media/` 目录
- **动态生成的图片** → 临时存储或CDN
- **大型视频文件** → 专门的媒体服务器
- **第三方CDN资源** → 直接使用URL

## 构建过程

1. **开发时**: Vite直接服务 `src/assets/` 中的文件
2. **构建时**: Vite优化并复制到 `dist/assets/`
3. **部署时**: `dist/assets/` 部署到生产环境

## 知识图谱项目的资源策略

### 静态UI资源 → `src/assets/`
- 应用Logo
- 导航图标
- 默认节点图标
- 加载动画

### 动态内容资源 → 后端 `media/` + 代理
- 节点图片/视频
- 用户上传内容
- 外部链接资源

### 示例配置
```tsx
// 静态Logo
import appLogo from '@/assets/images/logo.svg';
import { buildProxyMediaUrl } from '@/utils/apiBase';

// 动态节点图片
const nodeImage = buildProxyMediaUrl(imageUrl);
```
