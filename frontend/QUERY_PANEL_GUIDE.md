# 查询面板使用指南

## 概述

查询面板是 GraphInsight 的核心功能之一，提供了强大的 Cypher 查询能力。

## 界面布局

```
┌─────────────────────────────────────┐
│ Cypher 查询                         │
├─────────────────────────────────────┤
│                                     │
│  [Cypher 编辑器]                    │
│  // 示例查询                        │
│  MATCH (n:Crop)-[r]->(m)           │
│  RETURN n, r, m                     │
│  LIMIT 50                           │
│                                     │
│  [执行查询 (Ctrl+Enter)]            │
├─────────────────────────────────────┤
│  查询结果统计                       │
│  ● 节点数量: 25                     │
│  ● 关系数量: 48                     │
│  ● 执行时间: 0.123s                 │
│  ● 总计: 73 项                      │
├─────────────────────────────────────┤
│  查询历史 (3)              [清除]   │执行查询
- `Ctrl+A`: 全选
- `Ctrl+C`: 复制
- `Ctrl+V`: 粘贴
- `Ctrl+Z`: 撤销
- `Ctrl+Y`: 重做

**示例查询**:

```cypher
// 获取所有作物节点
MATCH (n:Crop)
RETURN n
LIMIT 10

// 获取作物及其病害关系
MATCH (c:Crop)-[r:AFFECTED_BY]->(d:Disease)
RETURN c, r, d

// 获取特定作物的所有关系
MATCH (c:Crop {name: '水稻'})-[r]->(m)
RETURN c, r, m

// 复杂查询：查找防治方案
MATCH (c:Crop)-[:AFFECTED_BY]->(d:Disease)
MATCH (t:Technology)-[:PREVENTS]->(d)
RETURN c, d, t
LIMIT 20
```

### 2. 执行查询

**方式**:
1. 点击"执行查询"按钮
2. 使用键盘快捷键 Ctrl+Enter

**执行流程**:
1. 验证查询不为空
2. 显示加载状态（按钮变为"执行中..."）
3. 发送请求到后端 API
4. 接收并解析结果
5. 更新图数据和统计信息
6. 添加到查询历史

**加载状态**:
- 按钮显示旋转的进度指示器
- 按钮文本变为"执行中..."
- 按钮禁用，防止重复点击

### 3. 错误处理

**错误类型**:

1. **空查询**
   ```
   请输入 Cypher 查询
   ```

2. **语法错误**
   ```
   INVALID_QUERY: Invalid Cypher syntax near line 1
   ```

3. **数据库错误**
   ```
   DATABASE_UNAVAILABLE: Cannot connect to Neo4j database
   ```

4. **网络错误**
   ```
   执行查询时发生未知错误
   ```

**错误显示**:
- 红色警告框显示在编辑器下方
- 显示错误代码和详细消息
- 可点击关闭按钮手动关闭

### 4. 查询统计

**显示内容**:

| 指标 | 说明 | 颜色 |
|------|------|------|
| 节点数量 | 当前图中的节点总数 | 绿色 |
| 关系数量 | 当前图中的边总数 | 蓝色 |
| 执行时间 | 查询执行耗时（秒） | 蓝色 |
| 总计 | 节点 + 关系的总数 | 绿色 |

**更新时机**:
- 每次成功执行查询后自动更新
- 显示最新的查询结果统计

**空状态**:
- 未执行查询时显示提示："执行查询后显示统计信息"

### 5. 查询历史

**功能**:
- 自动保存最近 20 条查询
- 持久化到浏览器 localStorage
- 点击历史项重新执行查询

**显示信息**:
- 查询文本（超过 60 字符自动截断）
- 相对时间戳（刚刚、X分钟前、X小时前、X天前）
- 结果数量（节点 + 边）

**时间格式**:
- 1 分钟内: "刚刚"
- 1-60 分钟: "X 分钟前"
- 1-24 小时: "X 小时前"
- 1-7 天: "X 天前"
- 7 天以上: 显示日期（如 "2024/1/15"）

**操作**:
- 点击历史项：重新执行该查询
- 点击清除按钮：删除所有历史记录

**空状态**:
- 无历史时显示图标和提示："暂无查询历史"

## 使用技巧

### 1. 快速开始

使用默认示例查询：
```cypher
MATCH (n:Crop)-[r]->(m)
RETURN n, r, m
LIMIT 50
```

### 2. 限制结果数量

始终使用 LIMIT 限制结果：
```cypher
MATCH (n)
RETURN n
LIMIT 100  // 推荐：限制结果数量
```

### 3. 使用注释

添加注释说明查询意图：
```cypher
// 查找水稻的所有病害
MATCH (c:Crop {name: '水稻'})-[:AFFECTED_BY]->(d:Disease)
RETURN c, d
```

### 4. 分步查询

复杂查询分步执行：
```cypher
// 第一步：查看数据结构
MATCH (n)
RETURN DISTINCT labels(n), count(*)

// 第二步：查看关系类型
MATCH ()-[r]->()
RETURN DISTINCT type(r), count(*)

// 第三步：执行具体查询
MATCH (c:Crop)-[r:AFFECTED_BY]->(d:Disease)
RETURN c, r, d
```

### 5. 使用历史

- 点击历史查询快速重新执行
- 修改历史查询创建新变体
- 定期清除不需要的历史

## 性能建议

### 1. 使用 LIMIT

```cypher
// 不推荐：可能返回大量数据
MATCH (n)
RETURN n

// 推荐：限制结果数量
MATCH (n)
RETURN n
LIMIT 100
```

### 2. 使用索引

```cypher
// 使用属性过滤（如果有索引）
MATCH (c:Crop {name: '水稻'})
RETURN c

// 避免全表扫描
MATCH (c:Crop)
WHERE c.name CONTAINS '稻'
RETURN c
```

### 3. 限制关系深度

```cypher
// 限制路径长度
MATCH path = (n)-[*1..3]->(m)
RETURN path
LIMIT 50

// 避免无限深度
MATCH path = (n)-[*]->(m)
RETURN path
```

## 常见问题

### Q: 为什么查询没有返回结果？

A: 可能的原因：
1. 数据库中没有匹配的数据
2. 查询条件太严格
3. 标签或属性名称拼写错误

### Q: 如何查看数据库中有哪些节点类型？

A: 使用以下查询：
```cypher
MATCH (n)
RETURN DISTINCT labels(n), count(*)
```

### Q: 如何查看所有关系类型？

A: 使用以下查询：
```cypher
MATCH ()-[r]->()
RETURN DISTINCT type(r), count(*)
```

### Q: 查询历史保存在哪里？

A: 查询历史保存在浏览器的 localStorage 中，清除浏览器数据会删除历史记录。

### Q: 为什么执行时间很长？

A: 可能的原因：
1. 查询返回大量数据（添加 LIMIT）
2. 查询复杂度高（优化查询逻辑）
3. 数据库性能问题（联系管理员）

## 键盘快捷键总结

| 快捷键 | 功能 |
|--------|------|
| Ctrl+Enter | 执行查询 |
| Ctrl+A | 全选 |
| Ctrl+C | 复制 |
| Ctrl+V | 粘贴 |
| Ctrl+Z | 撤销 |
| Ctrl+Y | 重做 |

## 相关文档

- [Cypher 查询语言参考](https://neo4j.com/docs/cypher-manual/)
- [Neo4j 图数据库文档](https://neo4j.com/docs/)
- [GraphInsight 用户指南](./UI_GUIDE.md)

│  ┌─────────────────────────────┐   │
│  │ MATCH (n:Crop) RETURN n     │   │
│  │ 5分钟前 | 10 结果           │   │
│  └─────────────────────────────┘   │
│  ┌─────────────────────────────┐   │
│  │ MATCH (n)-[r]->(m) RETURN...│   │
│  │ 1小时前 | 73 结果           │   │
│  └─────────────────────────────┘   │
└─────────────────────────────────────┘
```

## 功能详解

### 1. Cypher 编辑器

**特性**:
- 专业的代码编辑器（Monaco Editor）
- SQL 语法高亮
- 行号显示
- 自动换行
- 主题自适应（浅色/深色）

**快捷键**:
- `Ctrl+Enter` (Windows/Linux) 或 `Cmd+Enter` (Mac): 