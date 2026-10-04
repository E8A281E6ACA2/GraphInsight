"""
用户 CRUD 操作
"""
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, desc, asc
from typing import Optional, List, Tuple, Any
from datetime import datetime

from ..models import AdminUser
from ..schemas.auth import UserCreate, UserUpdate
from core.security import get_password_hash, verify_password
from core.logger import logger


class UserCRUD:
    """用户 CRUD 操作"""
    
    def get_by_id(self, db: Session, user_id: int) -> Optional[AdminUser]:
        """根据ID获取用户"""
        return db.query(AdminUser).filter(AdminUser.id == user_id).first()
    
    def get_by_username(self, db: Session, username: str) -> Optional[AdminUser]:
        """根据用户名获取用户"""
        return db.query(AdminUser).filter(AdminUser.username == username).first()
    
    def get_by_email(self, db: Session, email: str) -> Optional[AdminUser]:
        """根据邮箱获取用户"""
        return db.query(AdminUser).filter(AdminUser.email == email).first()

    def get_by_ids(self, db: Session, user_ids: List[int]) -> List[AdminUser]:
        """根据 ID 列表获取用户"""
        if not user_ids:
            return []
        return db.query(AdminUser).filter(AdminUser.id.in_(user_ids)).all()
    
    def get_multi(
        self, 
        db: Session, 
        skip: int = 0, 
        limit: int = 100,
        search: str = None,
        is_active: bool = None,
        department: str = None,
        order_by: str = "created_at",
        order_desc: bool = True
    ) -> Tuple[List[AdminUser], int]:
        """获取用户列表（分页、搜索、过滤）"""
        # 基础查询
        query = db.query(AdminUser)
        
        # 搜索条件
        if search:
            search_filter = or_(
                AdminUser.username.ilike(f"%{search}%"),
                AdminUser.email.ilike(f"%{search}%"),
                AdminUser.full_name.ilike(f"%{search}%")
            )
            query = query.filter(search_filter)
        
        # 状态过滤
        if is_active is not None:
            query = query.filter(AdminUser.is_active == is_active)
        
        # 部门过滤
        if department:
            query = query.filter(AdminUser.department == department)
        
        # 总数统计
        total = query.count()
        
        # 排序
        if hasattr(AdminUser, order_by):
            order_column = getattr(AdminUser, order_by)
            if order_desc:
                query = query.order_by(desc(order_column))
            else:
                query = query.order_by(asc(order_column))
        
        # 分页
        users = query.offset(skip).limit(limit).all()
        
        return users, total
    
    def get_departments(self, db: Session) -> List[str]:
        """获取所有部门列表"""
        result = db.query(AdminUser.department).filter(
            AdminUser.department.isnot(None),
            AdminUser.department != ""
        ).distinct().all()
        return [dept[0] for dept in result if dept[0]]
    
    def create(
        self,
        db: Session,
        user_create: Optional[UserCreate] = None,
        **kwargs: Any,
    ) -> AdminUser:
        """创建用户（兼容 UserCreate 模型和关键字参数两种调用方式）"""
        if user_create is not None:
            username = user_create.username
            email = user_create.email
            password_hash = get_password_hash(user_create.password)
            is_active = True
        else:
            username = kwargs.get("username")
            email = kwargs.get("email")
            password_hash = kwargs.get("password_hash")
            raw_password = kwargs.get("password")
            is_active = kwargs.get("is_active", True)

            if not username:
                raise ValueError("缺少用户名")
            if not password_hash:
                if raw_password:
                    password_hash = get_password_hash(raw_password)
                else:
                    raise ValueError("缺少密码或密码哈希")

        # 检查用户名是否已存在
        if self.get_by_username(db, username):
            raise ValueError(f"用户名 {username} 已存在")

        # 检查邮箱是否已存在
        if email and self.get_by_email(db, email):
            raise ValueError(f"邮箱 {email} 已存在")

        db_user = AdminUser(
            username=username,
            email=email,
            password_hash=password_hash,
            is_active=is_active,
            full_name=kwargs.get("full_name"),
            phone=kwargs.get("phone"),
            department=kwargs.get("department"),
            avatar=kwargs.get("avatar"),
        )
        db.add(db_user)
        db.commit()
        db.refresh(db_user)

        logger.info(f"创建用户成功: {username}")
        return db_user

    def reset_password(self, db: Session, user_id: int, new_password: str) -> Optional[AdminUser]:
        """管理员重置用户密码"""
        db_user = self.get_by_id(db, user_id)
        if not db_user:
            return None
        db_user.password_hash = get_password_hash(new_password)
        db_user.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(db_user)
        logger.info(f"重置用户密码成功: {db_user.username}")
        return db_user
    
    def update(self, db: Session, user_id: int, user_update: UserUpdate) -> Optional[AdminUser]:
        """更新用户"""
        db_user = self.get_by_id(db, user_id)
        if not db_user:
            return None
        
        # 更新字段
        update_data = user_update.dict(exclude_unset=True, exclude={"password"})
        
        # 如果更新密码，需要加密
        if hasattr(user_update, 'password') and user_update.password:
            update_data["password_hash"] = get_password_hash(user_update.password)
        
        # 更新时间戳
        update_data["updated_at"] = datetime.utcnow()
        
        for field, value in update_data.items():
            setattr(db_user, field, value)
        
        db.commit()
        db.refresh(db_user)
        
        logger.info(f"更新用户成功: {db_user.username}")
        return db_user
    
    def delete(self, db: Session, user_id: int, soft_delete: bool = True) -> bool:
        """删除用户（支持软删除）"""
        db_user = self.get_by_id(db, user_id)
        if not db_user:
            return False
        
        if soft_delete:
            # 软删除：设置为非活跃状态
            db_user.is_active = False
            db_user.updated_at = datetime.utcnow()
            db.commit()
            logger.info(f"软删除用户成功: {db_user.username}")
        else:
            # 硬删除：从数据库中删除
            username = db_user.username
            db.delete(db_user)
            db.commit()
            logger.info(f"硬删除用户成功: {username}")
        
        return True
    
    def batch_delete(self, db: Session, user_ids: List[int], soft_delete: bool = True) -> int:
        """批量删除用户"""
        count = 0
        for user_id in user_ids:
            if self.delete(db, user_id, soft_delete):
                count += 1
        return count

    def batch_update_status(
        self,
        db: Session,
        user_ids: List[int],
        is_active: bool,
        exclude_user_id: Optional[int] = None,
    ) -> dict:
        """批量更新用户状态"""
        normalized_ids = sorted({uid for uid in user_ids if isinstance(uid, int) and uid > 0})
        if not normalized_ids:
            return {"updated_ids": [], "not_found_ids": [], "skipped_self_ids": []}

        users = self.get_by_ids(db, normalized_ids)
        users_by_id = {user.id: user for user in users}
        updated_ids: List[int] = []
        not_found_ids: List[int] = []
        skipped_self_ids: List[int] = []

        for user_id in normalized_ids:
            if exclude_user_id is not None and user_id == exclude_user_id:
                skipped_self_ids.append(user_id)
                continue

            user = users_by_id.get(user_id)
            if user is None:
                not_found_ids.append(user_id)
                continue

            user.is_active = is_active
            user.updated_at = datetime.utcnow()
            updated_ids.append(user_id)

        db.commit()
        return {
            "updated_ids": updated_ids,
            "not_found_ids": not_found_ids,
            "skipped_self_ids": skipped_self_ids,
        }

    def batch_delete_users(
        self,
        db: Session,
        user_ids: List[int],
        soft_delete: bool = True,
        exclude_user_id: Optional[int] = None,
    ) -> dict:
        """批量删除用户（软删优先）"""
        normalized_ids = sorted({uid for uid in user_ids if isinstance(uid, int) and uid > 0})
        if not normalized_ids:
            return {"deleted_ids": [], "not_found_ids": [], "skipped_self_ids": []}

        users = self.get_by_ids(db, normalized_ids)
        users_by_id = {user.id: user for user in users}
        deleted_ids: List[int] = []
        not_found_ids: List[int] = []
        skipped_self_ids: List[int] = []

        for user_id in normalized_ids:
            if exclude_user_id is not None and user_id == exclude_user_id:
                skipped_self_ids.append(user_id)
                continue

            user = users_by_id.get(user_id)
            if user is None:
                not_found_ids.append(user_id)
                continue

            if soft_delete:
                user.is_active = False
                user.updated_at = datetime.utcnow()
            else:
                db.delete(user)
            deleted_ids.append(user_id)

        db.commit()
        return {
            "deleted_ids": deleted_ids,
            "not_found_ids": not_found_ids,
            "skipped_self_ids": skipped_self_ids,
        }

    def batch_reset_password(
        self,
        db: Session,
        user_ids: List[int],
        new_password: str,
        exclude_user_id: Optional[int] = None,
    ) -> dict:
        """批量重置用户密码"""
        normalized_ids = sorted({uid for uid in user_ids if isinstance(uid, int) and uid > 0})
        if not normalized_ids:
            return {"reset_ids": [], "not_found_ids": [], "skipped_self_ids": []}

        users = self.get_by_ids(db, normalized_ids)
        users_by_id = {user.id: user for user in users}
        reset_ids: List[int] = []
        not_found_ids: List[int] = []
        skipped_self_ids: List[int] = []
        password_hash = get_password_hash(new_password)

        for user_id in normalized_ids:
            if exclude_user_id is not None and user_id == exclude_user_id:
                skipped_self_ids.append(user_id)
                continue

            user = users_by_id.get(user_id)
            if user is None:
                not_found_ids.append(user_id)
                continue

            user.password_hash = password_hash
            user.updated_at = datetime.utcnow()
            reset_ids.append(user_id)

        db.commit()
        return {
            "reset_ids": reset_ids,
            "not_found_ids": not_found_ids,
            "skipped_self_ids": skipped_self_ids,
        }
    
    def toggle_status(self, db: Session, user_id: int) -> Optional[AdminUser]:
        """切换用户状态"""
        db_user = self.get_by_id(db, user_id)
        if not db_user:
            return None
        
        db_user.is_active = not db_user.is_active
        db_user.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(db_user)
        
        status = "激活" if db_user.is_active else "禁用"
        logger.info(f"{status}用户成功: {db_user.username}")
        return db_user
    
    def authenticate(self, db: Session, username: str, password: str) -> Optional[AdminUser]:
        """用户认证"""
        user = self.get_by_username(db, username) or self.get_by_email(db, username)
        if not user:
            return None
        
        if not verify_password(password, user.password_hash):
            return None
        
        # 更新登录信息
        user.last_login = datetime.utcnow()
        user.login_count = (user.login_count or 0) + 1
        db.commit()
        
        return user
    
    def update_login_info(self, db: Session, user_id: int, ip_address: str = None):
        """更新登录信息"""
        user = self.get_by_id(db, user_id)
        if user:
            user.last_login = datetime.utcnow()
            user.login_count = (user.login_count or 0) + 1
            if ip_address:
                user.last_login_ip = ip_address
            db.commit()
    
    def update_last_login(self, db: Session, user_id: int):
        """更新最后登录时间（别名方法，用于兼容）"""
        self.update_login_info(db, user_id)
    
    def get_count(self, db: Session) -> int:
        """获取用户总数"""
        return db.query(func.count(AdminUser.id)).scalar()


# 创建全局实例
user_crud = UserCRUD()
