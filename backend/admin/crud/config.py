"""
配置 CRUD 操作
"""
from typing import Optional, List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from ..models import AdminConfig
from ..schemas.config import ConfigCreate, ConfigUpdate, ConfigQuery
from core import DatabaseException, NotFoundException


class ConfigCRUD:
    """配置 CRUD 操作类"""
    
    def get_by_id(self, db: Session, config_id: int) -> Optional[AdminConfig]:
        """根据 ID 获取配置"""
        try:
            return db.query(AdminConfig).filter(AdminConfig.id == config_id).first()
        except Exception as e:
            raise DatabaseException(f"查询配置失败: {str(e)}")
    
    def get_by_key(
        self,
        db: Session,
        category: str,
        key: str
    ) -> Optional[AdminConfig]:
        """根据分类和键获取配置"""
        try:
            return db.query(AdminConfig).filter(
                AdminConfig.category == category,
                AdminConfig.key == key
            ).first()
        except Exception as e:
            raise DatabaseException(f"查询配置失败: {str(e)}")
    
    def get_list(
        self,
        db: Session,
        query: ConfigQuery
    ) -> Tuple[List[AdminConfig], int]:
        """获取配置列表（分页）"""
        try:
            # 构建查询
            db_query = db.query(AdminConfig)
            
            # 过滤条件
            if query.category:
                db_query = db_query.filter(AdminConfig.category == query.category)
            if query.key:
                db_query = db_query.filter(AdminConfig.key.like(f"%{query.key}%"))
            if query.is_sensitive is not None:
                db_query = db_query.filter(AdminConfig.is_sensitive == query.is_sensitive)
            
            # 总数
            total = db_query.count()
            
            # 分页
            offset = (query.page - 1) * query.page_size
            items = db_query.order_by(
                AdminConfig.category,
                AdminConfig.key
            ).offset(offset).limit(query.page_size).all()
            
            return items, total
        except Exception as e:
            raise DatabaseException(f"查询配置列表失败: {str(e)}")
    
    def get_by_category(self, db: Session, category: str) -> List[AdminConfig]:
        """获取指定分类的所有配置"""
        try:
            return db.query(AdminConfig).filter(
                AdminConfig.category == category
            ).order_by(AdminConfig.key).all()
        except Exception as e:
            raise DatabaseException(f"查询配置失败: {str(e)}")
    
    def create(
        self,
        db: Session,
        config_create: ConfigCreate,
        user_id: Optional[int] = None
    ) -> AdminConfig:
        """创建配置"""
        try:
            # 检查是否已存在
            existing = self.get_by_key(db, config_create.category, config_create.key)
            if existing:
                raise DatabaseException(f"配置已存在: {config_create.category}.{config_create.key}")
            
            db_config = AdminConfig(
                category=config_create.category,
                key=config_create.key,
                value=config_create.value,
                description=config_create.description,
                is_sensitive=config_create.is_sensitive,
                is_encrypted=False,
                updated_by=user_id,
                version=1
            )
            db.add(db_config)
            db.commit()
            db.refresh(db_config)
            return db_config
        except Exception as e:
            db.rollback()
            raise DatabaseException(f"创建配置失败: {str(e)}")
    
    def update(
        self,
        db: Session,
        category: str,
        key: str,
        config_update: ConfigUpdate,
        user_id: int
    ) -> Optional[AdminConfig]:
        """更新配置"""
        try:
            db_config = self.get_by_key(db, category, key)
            if not db_config:
                return None
            
            db_config.value = config_update.value
            if config_update.description is not None:
                db_config.description = config_update.description
            db_config.updated_by = user_id
            db_config.version += 1
            
            db.commit()
            db.refresh(db_config)
            return db_config
        except Exception as e:
            db.rollback()
            raise DatabaseException(f"更新配置失败: {str(e)}")
    
    def delete(self, db: Session, category: str, key: str) -> bool:
        """删除配置"""
        try:
            db_config = self.get_by_key(db, category, key)
            if not db_config:
                return False
            
            db.delete(db_config)
            db.commit()
            return True
        except Exception as e:
            db.rollback()
            raise DatabaseException(f"删除配置失败: {str(e)}")
    
    def batch_update(
        self,
        db: Session,
        configs: List[dict],
        user_id: int
    ) -> int:
        """批量更新配置"""
        try:
            updated_count = 0
            for config_data in configs:
                category = config_data.get("category")
                key = config_data.get("key")
                value = config_data.get("value")
                
                if not all([category, key, value is not None]):
                    continue
                
                db_config = self.get_by_key(db, category, key)
                if db_config:
                    db_config.value = value
                    db_config.updated_by = user_id
                    db_config.version += 1
                    updated_count += 1
            
            db.commit()
            return updated_count
        except Exception as e:
            db.rollback()
            raise DatabaseException(f"批量更新配置失败: {str(e)}")
    
    def get_count(self, db: Session) -> int:
        """获取配置总数"""
        try:
            return db.query(func.count(AdminConfig.id)).scalar()
        except Exception as e:
            raise DatabaseException(f"查询配置数量失败: {str(e)}")


# 创建全局实例
config_crud = ConfigCRUD()
