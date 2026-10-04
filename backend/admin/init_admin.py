"""
初始化管理系统数据库
"""
import sys
import os
from getpass import getpass

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin.database import init_db, SessionLocal
from admin.models import AdminUser
from admin.auth import get_password_hash


def create_admin_user(username: str, password: str, email: str = None):
    """创建管理员用户"""
    db = SessionLocal()
    try:
        # 检查用户是否已存在
        existing_user = db.query(AdminUser).filter(AdminUser.username == username).first()
        if existing_user:
            print(f"用户 '{username}' 已存在")
            return False
        
        # 创建新用户
        user = AdminUser(
            username=username,
            password_hash=get_password_hash(password),
            email=email,
            is_active=True
        )
        db.add(user)
        db.commit()
        print(f"管理员用户 '{username}' 创建成功")
        return True
    except Exception as e:
        print(f"创建用户失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def main():
    """主函数"""
    print("=" * 50)
    print("GraphInsight 管理系统初始化")
    print("=" * 50)
    
    # 初始化数据库表
    print("\n初始化数据库表...")
    try:
        init_db()
        print("数据库表创建成功")
    except Exception as e:
        print(f"数据库表创建失败: {e}")
        print("\n请检查本地 Docker PostgreSQL 连接配置:")
        print("  - 优先使用 scripts/dev-backend.sh up")
        print("  - 或确认 ADMIN_DATABASE_URL 指向本地容器 127.0.0.1:5434")
        return
    
    # 创建管理员用户
    print("\n创建管理员用户")
    username = input("用户名 (默认: admin): ").strip() or "admin"
    password = getpass("密码: ")
    password_confirm = getpass("确认密码: ")
    
    if password != password_confirm:
        print("两次密码不一致")
        return
    
    if len(password) < 6:
        print("密码长度至少 6 位")
        return
    
    # 检查密码字节长度（bcrypt 限制）
    if len(password.encode('utf-8')) > 72:
        print(f"密码太长（{len(password.encode('utf-8'))} 字节），最多 72 字节")
        print("提示：请使用较短的密码")
        return
    
    email = input("邮箱 (可选): ").strip() or None
    
    if create_admin_user(username, password, email):
        print("\n" + "=" * 50)
        print("初始化完成！")
        print("=" * 50)
        print(f"\n登录信息:")
        print(f"  用户名: {username}")
        print(f"  密码: {'*' * len(password)}")
        print(f"\n访问管理后台:")
        print(f"  http://localhost:5174/admin/login")
    else:
        print("\n初始化失败")


if __name__ == "__main__":
    main()
