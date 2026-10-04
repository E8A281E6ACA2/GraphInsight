"""
初始化 PostgreSQL 数据库表
"""
import sys
import os

# 添加当前目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from admin.database import init_db, engine
from admin.models import Base

def main():
    """初始化数据库"""
    print("=" * 60)
    print("初始化 PostgreSQL 数据库")
    print("=" * 60)
    
    try:
        # 测试连接
        print("测试数据库连接...")
        with engine.connect() as conn:
            print("数据库连接成功")
        
        # 创建所有表
        print("\n创建数据库表...")
        Base.metadata.create_all(bind=engine)
        print("数据库表创建成功")
        
        # 列出创建的表
        print("\n已创建的表：")
        for table in Base.metadata.sorted_tables:
            print(f"  - {table.name}")
        
        print("\n" + "=" * 60)
        print("数据库初始化完成！")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n错误: {str(e)}")
        print("\n可能的原因：")
        print("1. PostgreSQL 服务未启动")
        print("2. 数据库连接信息错误")
        print("3. 数据库不存在")
        print("4. 用户权限不足")
        print("\n请优先检查本地 Docker PostgreSQL 是否已启动，以及 ADMIN_DATABASE_URL 是否指向 127.0.0.1:5434")
        sys.exit(1)

if __name__ == "__main__":
    main()
