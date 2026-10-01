"""
admin_jobs.targets_hash 迁移/回滚（M5-A，设计 §16.3 v3.2.1 冻结契约）。

- 增加 targets_hash VARCHAR(64) NULL（保留历史 NULL 行，不参与唯一性）
- 唯一索引：
    CREATE UNIQUE INDEX uq_admin_jobs_targets_hash
      ON admin_jobs (job_type, kb_id, targets_hash)
      WHERE targets_hash IS NOT NULL;
  v3.2.1 收口：包含所有状态（failed/cancelled 同样占位唯一性），
  同 targets_hash 重提交一律原地 retry，不新建行。
- 支持 postgresql / sqlite 双方言；--dry-run 只打印计划不写库；
  --action rollback 先 drop 索引再 drop 列。

用法：
    python backend/admin/migrate_jobs_targets_hash.py --dry-run
    python backend/admin/migrate_jobs_targets_hash.py
    python backend/admin/migrate_jobs_targets_hash.py --action rollback
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import text

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import engine  # noqa: E402

load_dotenv(find_dotenv(), override=True)

TABLE = "admin_jobs"
COLUMN = "targets_hash"
INDEX_NAME = "uq_admin_jobs_targets_hash"


def _safe_db_url(raw: str) -> str:
    if "@" not in raw:
        return raw
    left, right = raw.split("@", 1)
    if "://" in left and ":" in left.split("://", 1)[1]:
        prefix, account = left.split("://", 1)
        username = account.split(":", 1)[0]
        return f"{prefix}://{username}:****@{right}"
    return raw


def _dialect_name() -> str:
    dialect = engine.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"unsupported dialect: {dialect}")
    return dialect


def _column_exists(conn) -> bool:
    dialect = _dialect_name()
    if dialect == "postgresql":
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :column LIMIT 1"
                ),
                {"table": TABLE, "column": COLUMN},
            ).scalar()
        )
    rows = conn.execute(text(f"PRAGMA table_info({TABLE})")).fetchall()
    return any(str(row[1]) == COLUMN for row in rows)


def _index_exists(conn) -> bool:
    dialect = _dialect_name()
    if dialect == "postgresql":
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = CURRENT_SCHEMA() "
                    "AND tablename = :table AND indexname = :index LIMIT 1"
                ),
                {"table": TABLE, "index": INDEX_NAME},
            ).scalar()
        )
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": INDEX_NAME},
    ).fetchall()
    return bool(rows)


def _build_plan(action: str) -> list[str]:
    if action == "migrate":
        return [
            f"ensure column {TABLE}.{COLUMN} (VARCHAR(64) NULL, 历史 NULL 行保留)",
            f"ensure unique partial index {INDEX_NAME} "
            f"ON (job_type, kb_id, {COLUMN}) WHERE {COLUMN} IS NOT NULL "
            "(v3.2.1 全状态)",
        ]
    if action == "rollback":
        return [f"drop index {INDEX_NAME}", f"drop column {TABLE}.{COLUMN}"]
    raise RuntimeError(f"unsupported action: {action}")


def _print_plan(action: str) -> None:
    print("-" * 60)
    print(f"计划动作: {action}")
    for step in _build_plan(action):
        print(f"- {step}")
    print("-" * 60)


def _run(action: str) -> None:
    with engine.begin() as conn:
        if action == "migrate":
            if not _column_exists(conn):
                conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(64) NULL"))
                print(f"✓ added column {TABLE}.{COLUMN}")
            else:
                print(f"✓ {TABLE}.{COLUMN} already exists")
            if not _index_exists(conn):
                conn.execute(
                    text(
                        f"CREATE UNIQUE INDEX {INDEX_NAME} ON {TABLE} (job_type, kb_id, {COLUMN}) "
                        f"WHERE {COLUMN} IS NOT NULL"
                    )
                )
                print(f"✓ ensured index {INDEX_NAME} (WHERE {COLUMN} IS NOT NULL)")
            else:
                print(f"✓ index {INDEX_NAME} already exists")
            return

        if action == "rollback":
            if _index_exists(conn):
                conn.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
                print(f"✓ dropped index {INDEX_NAME}")
            else:
                print(f"✓ {INDEX_NAME} already absent")
            if _column_exists(conn):
                conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"))
                print(f"✓ dropped column {TABLE}.{COLUMN}")
            else:
                print(f"✓ {TABLE}.{COLUMN} already absent")
            return

    raise RuntimeError(f"unsupported action: {action}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate or rollback admin_jobs.targets_hash (M5-A)")
    parser.add_argument(
        "--action",
        choices=("migrate", "rollback"),
        default="migrate",
        help="apply forward migration or rollback",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the migration plan without modifying the database",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("=" * 60)
    print("GraphInsight admin_jobs.targets_hash migration (M5-A)")
    print("=" * 60)
    print(f"数据库: {_safe_db_url(os.getenv('ADMIN_DATABASE_URL', '未配置'))}")
    print(f"方言: {_dialect_name()}")
    _print_plan(args.action)

    if args.dry_run:
        print("✓ dry-run completed, database not modified")
        return 0

    _run(args.action)
    print(f"✓ admin_jobs.targets_hash {args.action} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())