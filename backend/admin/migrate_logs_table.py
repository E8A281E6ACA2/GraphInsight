"""
数据库迁移脚本：更新 admin_logs 表结构
"""
import os
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from sqlalchemy import text
from admin.database import engine
from dotenv import load_dotenv, find_dotenv

# 加载环境变量
load_dotenv(find_dotenv(), override=True)


def check_column_exists(table_name: str, column_name: str) -> bool:
    """检查列是否存在"""
    with engine.connect() as conn:
        query = text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = :table_name 
            AND column_name = :column_name
        """)
        result = conn.execute(query, {"table_name": table_name, "column_name": column_name})
        return result.fetchone() is not None


def migrate_logs_table():
    """更新 admin_logs 表结构"""
    print("开始数据库迁移...")
    
    try:
        # 需要添加的列
        columns_to_add = [
            ("resource_id", "VARCHAR(100)", None),
            ("user_agent", "VARCHAR(500)", None),
            ("status", "VARCHAR(20)", "'success'"),
            ("error_message", "TEXT", None),
        ]
        
        added_columns = []
        
        for column_name, column_type, default_value in columns_to_add:
            if check_column_exists("admin_logs", column_name):
                print(f"✓ {column_name} 列已存在，跳过")
                continue
            
            print(f"添加 {column_name} 列...")
            with engine.connect() as conn:
                # 构建 SQL
                sql = f"ALTER TABLE admin_logs ADD COLUMN {column_name} {column_type}"
                if default_value:
                    sql += f" DEFAULT {default_value}"
                
                conn.execute(text(sql))
                conn.commit()
                print(f"✓ 成功添加 {column_name} 列")
                added_columns.append(column_name)
        
        if added_columns:
            print(f"\n✓ 成功添加 {len(added_columns)} 个列: {', '.join(added_columns)}")
        else:
            print("\n✓ 所有列都已存在，无需迁移")
        
        print("\n✓ 数据库迁移完成!")
        return True
        
    except Exception as e:
        print(f"\n✗ 迁移失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def verify_migration():
    """验证迁移结果"""
    print("\n验证迁移结果...")
    
    try:
        with engine.connect() as conn:
            # 查询表结构
            result = conn.execute(text("""
                SELECT column_name, data_type, column_default
                FROM information_schema.columns
                WHERE table_name = 'admin_logs'
                ORDER BY ordinal_position
            """))
            
            print("\nadmin_logs 表结构:")
            print("-" * 80)
            print(f"{'字段名':<20} {'类型':<25} {'默认值':<30}")
            print("-" * 80)
            for row in result:
                print(f"{row[0]:<20} {row[1]:<25} {str(row[2] or ''):<30}")
            print("-" * 80)
            
            # 测试插入
            print("\n测试插入数据...")
            conn.execute(text("""
                INSERT INTO admin_logs (
                    action, resource, resource_id, details, 
                    ip_address, user_agent, status, error_message
                ) VALUES (
                    'test', 'test', '1', 'test details',
                    '127.0.0.1', 'test-agent', 'success', NULL
                )
            """))
            conn.commit()
            print("✓ 插入测试数据成功")
            
            # 删除测试数据
            conn.execute(text("""
                DELETE FROM admin_logs WHERE action = 'test'
            """))
            conn.commit()
            print("✓ 删除测试数据成功")
            
        return True
        
    except Exception as e:
        print(f"\n✗ 验证失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("=" * 80)
    print("数据库迁移工具 - 更新 admin_logs 表")
    print("=" * 80)
    print()
    
    # 显示数据库连接信息
    db_url = os.getenv("ADMIN_DATABASE_URL", "未配置")
    if "@" in db_url:
        parts = db_url.split("@")
        user_part = parts[0].split("://")[1].split(":")[0]
        db_url_display = f"postgresql://{user_part}:****@{parts[1]}"
    else:
        db_url_display = db_url
    
    print(f"数据库: {db_url_display}")
    print()
    
    # 执行迁移
    if migrate_logs_table():
        verify_migration()
        print("\n✓ 所有操作完成!")
        sys.exit(0)
    else:
        print("\n✗ 迁移失败，请检查错误信息")
        sys.exit(1)
