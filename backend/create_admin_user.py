"""
快速创建管理员用户（非交互式）
"""
import sys
sys.path.insert(0, '.')

from admin.database import init_db, SessionLocal
from admin.models import AdminUser
from admin.auth import get_password_hash

# 初始化数据库
print("初始化数据库...")
init_db()
print("数据库表创建成功")

# 创建管理员用户
username = "admin"
password = "admin123"  # 默认密码，请在首次登录后修改
email = "admin@example.com"

db = SessionLocal()
try:
    # 检查用户是否已存在
    existing = db.query(AdminUser).filter(AdminUser.username == username).first()
    if existing:
        print(f"用户 '{username}' 已存在，跳过创建")
    else:
        user = AdminUser(
            username=username,
            password_hash=get_password_hash(password),
            email=email,
            is_active=True
        )
        db.add(user)
        db.commit()
        print(f"管理员用户创建成功")
        print(f"   用户名: {username}")
        print(f"   密码: {password}")
        print(f"   邮箱: {email}")
        print(f"\n警告: 请在首次登录后修改密码！")
except Exception as e:
    print(f"创建用户失败: {e}")
    db.rollback()
finally:
    db.close()
