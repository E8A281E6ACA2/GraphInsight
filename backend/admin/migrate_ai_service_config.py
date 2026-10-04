#!/usr/bin/env python3
"""
迁移AI服务配置
将 openai 配置重命名为 ai_service
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin.database import SessionLocal
from sqlalchemy import text

def migrate_ai_config():
    """迁移AI配置"""
    db = SessionLocal()
    
    try:
        print("开始迁移AI服务配置...")
        
        # 1. 重命名 openai → ai_service
        print("\n1. 重命名配置分类...")
        result = db.execute(text("""
            UPDATE admin_configs 
            SET category = 'ai_service' 
            WHERE category = 'openai'
        """))
        db.commit()
        print(f"   更新了 {result.rowcount} 条配置")
        
        # 2. 添加新字段
        print("\n2. 添加新配置字段...")
        new_configs = [
            ("ai_service", "provider", "openai", "AI服务提供商", False),
            ("ai_service", "enabled", "true", "是否启用AI服务", False),
            ("ai_service", "docqa_reasoning_profile", "balanced", "文档问答默认推理档位", False),
            ("ai_service", "deep_research_reasoning_profile", "deep", "深度调研默认推理档位", False),
            ("ai_service", "model_probe_reasoning_profile", "fast", "模型连通性测试默认推理档位", False),
            ("ai_service", "graph_extract_reasoning_profile", "fast", "图谱抽取默认推理档位", False),
            ("ai_service", "graph_extract_complex_reasoning_profile", "balanced", "复杂图谱抽取默认推理档位", False),
        ]
        
        for category, key, value, description, is_sensitive in new_configs:
            # 检查是否已存在
            existing = db.execute(text("""
                SELECT id FROM admin_configs 
                WHERE category = :category AND key = :key
            """), {"category": category, "key": key}).fetchone()
            
            if not existing:
                db.execute(text("""
                    INSERT INTO admin_configs (category, key, value, description, is_sensitive)
                    VALUES (:category, :key, :value, :description, :is_sensitive)
                """), {
                    "category": category,
                    "key": key,
                    "value": value,
                    "description": description,
                    "is_sensitive": is_sensitive
                })
                print(f"   添加配置: {category}.{key}")
            else:
                print(f"   跳过已存在: {category}.{key}")
        
        db.commit()
        
        # 3. 显示当前配置
        print("\n3. 当前AI服务配置:")
        configs = db.execute(text("""
            SELECT key, value, description 
            FROM admin_configs 
            WHERE category = 'ai_service'
            ORDER BY key
        """)).fetchall()
        
        for config in configs:
            value = config.value if config.key != 'api_key' else '***'
            print(f"   {config.key}: {value}")
        
        print("\n迁移完成!")
        
    except Exception as e:
        print(f"迁移失败: {e}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    migrate_ai_config()
