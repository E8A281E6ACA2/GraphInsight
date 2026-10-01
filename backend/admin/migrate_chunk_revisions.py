"""
chunk_revisions 表迁移/回滚（M5-A，设计 §3 + §15.1 冻结契约）。

幂等建表：
- 全量字段（§3）+ UNIQUE (kb_id, chunk_id, content_revision)
- 5 个查询索引（§3）+ current 部分唯一索引（§15.1）
- 支持 postgresql / sqlite 双方言；--dry-run 只打印计划不写库；
  --action rollback 按"索引随表 drop"原则整表删除（§15.1：无独立回滚面）。

用法：
    python backend/admin/migrate_chunk_revisions.py --dry-run
    python backend/admin/migrate_chunk_revisions.py
    python backend/admin/migrate_chunk_revisions.py --action rollback
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

TABLE = "chunk_revisions"

# 部分唯一索引：每 (kb_id, chunk_id) 至多一个 current 行（§15.1，数据库级强制）
CURRENT_UNIQUE_INDEX = "uq_chunk_revisions_current"

# 普通索引（§3）
PLAIN_INDEXES = (
    "idx_chunk_rev_kb_chunk_status",
    "idx_chunk_rev_kb_status_graph",
    "idx_chunk_rev_kb_status_vector",
    "idx_chunk_rev_kb_doc_graph",
    "idx_chunk_rev_kb_doc_vector",
)

_POSTGRES_DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        revision_id             BIGSERIAL PRIMARY KEY,
        kb_id                   VARCHAR(100) NOT NULL,
        tenant_id               VARCHAR(100) NOT NULL,
        project_id              VARCHAR(100) NOT NULL,
        doc_id                  VARCHAR(255) NOT NULL,
        chunk_id                VARCHAR(255) NOT NULL,
        source_content          TEXT NOT NULL,
        source_content_hash     VARCHAR(80) NOT NULL,
        content                 TEXT NOT NULL,
        content_hash            VARCHAR(80) NOT NULL,
        content_revision        INTEGER NOT NULL,
        revision_status         VARCHAR(20) NOT NULL DEFAULT 'current',
        graph_status            VARCHAR(20) NOT NULL DEFAULT 'pending',
        vector_status           VARCHAR(20) NOT NULL DEFAULT 'pending',
        graph_content_revision  INTEGER NULL,
        vector_content_revision INTEGER NULL,
        revision_source         VARCHAR(20) NOT NULL DEFAULT 'system_initial',
        source_version          VARCHAR(100) NULL,
        parser_version          VARCHAR(100) NULL,
        edited_by               INTEGER NULL,
        edited_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
        reason                  VARCHAR(500) NULL,
        trace_id                VARCHAR(100) NULL,
        CONSTRAINT uq_chunk_revisions_rev UNIQUE (kb_id, chunk_id, content_revision)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_chunk_status ON {TABLE} (kb_id, chunk_id, revision_status)",
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_status_graph ON {TABLE} (kb_id, revision_status, graph_status)",
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_status_vector ON {TABLE} (kb_id, revision_status, vector_status)",
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_doc_graph ON {TABLE} (kb_id, doc_id, revision_status, graph_status)",
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_doc_vector ON {TABLE} (kb_id, doc_id, revision_status, vector_status)",
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS {CURRENT_UNIQUE_INDEX}
      ON {TABLE} (kb_id, chunk_id)
      WHERE revision_status = 'current'
    """,
]

_SQLITE_DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        revision_id             INTEGER PRIMARY KEY AUTOINCREMENT,
        kb_id                   VARCHAR(100) NOT NULL,
        tenant_id               VARCHAR(100) NOT NULL,
        project_id              VARCHAR(100) NOT NULL,
        doc_id                  VARCHAR(255) NOT NULL,
        chunk_id                VARCHAR(255) NOT NULL,
        source_content          TEXT NOT NULL,
        source_content_hash     VARCHAR(80) NOT NULL,
        content                 TEXT NOT NULL,
        content_hash            VARCHAR(80) NOT NULL,
        content_revision        INTEGER NOT NULL,
        revision_status         VARCHAR(20) NOT NULL DEFAULT 'current',
        graph_status            VARCHAR(20) NOT NULL DEFAULT 'pending',
        vector_status           VARCHAR(20) NOT NULL DEFAULT 'pending',
        graph_content_revision  INTEGER NULL,
        vector_content_revision INTEGER NULL,
        revision_source         VARCHAR(20) NOT NULL DEFAULT 'system_initial',
        source_version          VARCHAR(100) NULL,
        parser_version          VARCHAR(100) NULL,
        edited_by               INTEGER NULL,
        edited_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reason                  VARCHAR(500) NULL,
        trace_id                VARCHAR(100) NULL,
        CONSTRAINT uq_chunk_revisions_rev UNIQUE (kb_id, chunk_id, content_revision)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_chunk_status ON {TABLE} (kb_id, chunk_id, revision_status)",
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_status_graph ON {TABLE} (kb_id, revision_status, graph_status)",
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_status_vector ON {TABLE} (kb_id, revision_status, vector_status)",
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_doc_graph ON {TABLE} (kb_id, doc_id, revision_status, graph_status)",
    f"CREATE INDEX IF NOT EXISTS idx_chunk_rev_kb_doc_vector ON {TABLE} (kb_id, doc_id, revision_status, vector_status)",
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS {CURRENT_UNIQUE_INDEX}
      ON {TABLE} (kb_id, chunk_id)
      WHERE revision_status = 'current'
    """,
]


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


def _table_exists(conn) -> bool:
    dialect = _dialect_name()
    if dialect == "postgresql":
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = :name AND table_schema = CURRENT_SCHEMA()"
                ),
                {"name": TABLE},
            ).scalar()
        )
    rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"), {"name": TABLE}).fetchall()
    return bool(rows)


def _index_exists(conn, index_name: str) -> bool:
    dialect = _dialect_name()
    if dialect == "postgresql":
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = CURRENT_SCHEMA() AND indexname = :name"
                ),
                {"name": index_name},
            ).scalar()
        )
    rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='index' AND name=:name"), {"name": index_name}).fetchall()
    return bool(rows)


def _build_plan(action: str) -> list[str]:
    if action == "migrate":
        plan = [f"ensure table {TABLE}"]
        plan += [f"ensure index {name}" for name in PLAIN_INDEXES]
        plan += [f"ensure unique partial index {CURRENT_UNIQUE_INDEX} (WHERE revision_status='current')"]
        return plan
    if action == "rollback":
        return [f"drop table {TABLE} (indexes drop with it, §15.1)"]
    raise RuntimeError(f"unsupported action: {action}")


def _print_plan(action: str) -> None:
    print("-" * 60)
    print(f"计划动作: {action}")
    for step in _build_plan(action):
        print(f"- {step}")
    print("-" * 60)


def _run(action: str) -> None:
    ddl = _POSTGRES_DDL if _dialect_name() == "postgresql" else _SQLITE_DDL
    with engine.begin() as conn:
        if action == "migrate":
            if _table_exists(conn):
                print(f"✓ {TABLE} already exists")
            else:
                for statement in ddl:
                    conn.execute(text(statement))
                print(f"✓ {TABLE} table is ready")
            missing = [name for name in PLAIN_INDEXES if not _index_exists(conn, name)]
            if not _index_exists(conn, CURRENT_UNIQUE_INDEX):
                missing.append(CURRENT_UNIQUE_INDEX)
            for name in missing:
                for statement in ddl:
                    if name in statement:
                        conn.execute(text(statement))
                        break
            print(f"✓ {len(PLAIN_INDEXES) + 1} indexes ensured")
            return

        if action == "rollback":
            conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
            print(f"✓ {TABLE} table rollback completed")
            return

    raise RuntimeError(f"unsupported action: {action}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate or rollback chunk_revisions table (M5-A)")
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
    print("GraphInsight chunk_revisions table migration (M5-A)")
    print("=" * 60)
    print(f"数据库: {_safe_db_url(os.getenv('ADMIN_DATABASE_URL', '未配置'))}")
    print(f"方言: {_dialect_name()}")
    _print_plan(args.action)

    if args.dry_run:
        print("✓ dry-run completed, database not modified")
        return 0

    _run(args.action)
    print(f"✓ chunk_revisions {args.action} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())