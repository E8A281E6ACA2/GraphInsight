"""
为审计与问答追踪表补充知识库作用域列

- admin_logs:            project_id, kb_id
- admin_qa_traces:       tenant_id, project_id, kb_id

契约：docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md §2.1/§2.10、§4 M1
幂等：列/索引存在即跳过；rollback 删除索引与列（不动存量数据）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import inspect, text

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import engine

load_dotenv(find_dotenv(), override=True)

# table -> [(column, type), ...]
SCOPE_COLUMNS = {
    "admin_logs": [
        ("project_id", "VARCHAR(100)"),
        ("kb_id", "VARCHAR(100)"),
    ],
    "admin_qa_traces": [
        ("tenant_id", "VARCHAR(100)"),
        ("project_id", "VARCHAR(100)"),
        ("kb_id", "VARCHAR(100)"),
    ],
}

# 索引名 -> (table, columns)
SCOPE_INDEXES = {
    "idx_admin_logs_project_id": ("admin_logs", ["project_id"]),
    "idx_admin_logs_kb_id": ("admin_logs", ["kb_id"]),
    "idx_admin_logs_scope_created": ("admin_logs", ["tenant_id", "project_id", "kb_id", "created_at"]),
    "idx_admin_qa_trace_tenant_id": ("admin_qa_traces", ["tenant_id"]),
    "idx_admin_qa_trace_project_id": ("admin_qa_traces", ["project_id"]),
    "idx_admin_qa_trace_kb_id": ("admin_qa_traces", ["kb_id"]),
    "idx_admin_qa_trace_scope_created": ("admin_qa_traces", ["tenant_id", "project_id", "kb_id", "created_at"]),
    # 手册 §6.2：任务与绑定表的组合作用域索引（表存在时创建）
    "idx_admin_jobs_scope_status": ("admin_jobs", ["tenant_id", "project_id", "kb_id", "status"]),
    "idx_admin_bindings_scope": ("admin_user_role_bindings", ["scope_type", "tenant_id", "project_id", "kb_id"]),
}


def _safe_db_url(raw: str) -> str:
    if "@" not in raw:
        return raw
    left, right = raw.split("@", 1)
    if "://" in left and ":" in left.split("://", 1)[1]:
        prefix, account = left.split("://", 1)
        username = account.split(":", 1)[0]
        return f"{prefix}://{username}:****@{right}"
    return raw


def _print_plan(action: str) -> None:
    print("-" * 60)
    print(f"计划动作: {action}")
    for table, columns in SCOPE_COLUMNS.items():
        print(f"- {table}: add {', '.join(name for name, _ in columns)}")
    for index_name, (table, cols) in SCOPE_INDEXES.items():
        print(f"- create index {index_name} on {table}({', '.join(cols)})")
    print("- 不迁移、不删除任何存量数据")
    print("-" * 60)


def _run(action: str) -> None:
    # 关键：inspect() 会使用独立的池连接。若在持有 DDL 锁的事务内调用 inspector，
    # PostgreSQL 上会自我死锁（本机真机验证踩过）。因此必须在开事务前预取结构快照。
    inspector = inspect(engine)
    tables_snapshot = set(inspector.get_table_names())
    columns_snapshot = {
        table: ({c["name"] for c in inspector.get_columns(table)} if table in tables_snapshot else set())
        for table in SCOPE_COLUMNS
    }

    with engine.begin() as conn:
        if action == "migrate":
            for table, columns in SCOPE_COLUMNS.items():
                if table not in tables_snapshot:
                    print(f"⚠ 表 {table} 不存在，跳过（先运行基础表迁移）")
                    continue
                existing = columns_snapshot[table]
                for column_name, column_type in columns:
                    if column_name in existing:
                        print(f"✓ {table}.{column_name} 已存在，跳过")
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}"))
                    print(f"✓ {table}.{column_name} 已添加")
            for index_name, (table, cols) in SCOPE_INDEXES.items():
                if table not in tables_snapshot:
                    continue
                column_sql = ", ".join(cols)
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column_sql})"))
                print(f"✓ index {index_name} ready")
            return
        if action == "rollback":
            for index_name in SCOPE_INDEXES:
                conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
                print(f"✓ index {index_name} dropped")
            for table, columns in SCOPE_COLUMNS.items():
                if table not in tables_snapshot:
                    continue
                existing = columns_snapshot[table]
                for column_name, _ in columns:
                    if column_name not in existing:
                        continue
                    if engine.dialect.name == "sqlite":
                        # SQLite 3.35+ 支持 DROP COLUMN，旧版本需重建表；此处直接尝试并报告
                        try:
                            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column_name}"))
                        except Exception as exc:  # noqa: BLE001 - 明确报告而不是吞掉
                            print(f"⚠ {table}.{column_name} 回滚失败（SQLite 版本过旧时需手工重建表）: {exc}")
                            continue
                    else:
                        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column_name}"))
                    print(f"✓ {table}.{column_name} dropped")
            return
    raise RuntimeError(f"unsupported action: {action}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add knowledge-base scope columns to audit/QA trace tables")
    parser.add_argument("--action", choices=("migrate", "rollback"), default="migrate")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("=" * 60)
    print("GraphInsight audit scope columns migration")
    print("=" * 60)
    print(f"数据库: {_safe_db_url(os.getenv('ADMIN_DATABASE_URL', '未配置'))}")
    print(f"方言: {engine.dialect.name}")
    _print_plan(args.action)
    if args.dry_run:
        print("✓ dry-run completed, database not modified")
        return 0
    _run(args.action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
