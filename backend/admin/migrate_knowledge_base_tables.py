"""
创建知识库目录表（knowledge_bases / knowledge_base_documents）

契约：docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md §4 M1、§5
口径：按全新知识库初始化，不创建 default KB，不迁移旧数据。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import Base, engine
from admin.models import KnowledgeBase, KnowledgeBaseDocument  # noqa: F401 - ensure model registration

load_dotenv(find_dotenv(), override=True)

TABLES = [KnowledgeBase.__table__, KnowledgeBaseDocument.__table__]


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
    if action == "migrate":
        print("- ensure table knowledge_bases")
        print("- ensure table knowledge_base_documents")
        print("- 不创建 default KB，不迁移旧知识数据")
    else:
        print("- drop table knowledge_base_documents")
        print("- drop table knowledge_bases")
    print("-" * 60)


def _run(action: str) -> None:
    if action == "migrate":
        Base.metadata.create_all(bind=engine, tables=TABLES)
        print("✓ knowledge_bases / knowledge_base_documents tables are ready")
        return
    if action == "rollback":
        KnowledgeBaseDocument.__table__.drop(bind=engine, checkfirst=True)
        KnowledgeBase.__table__.drop(bind=engine, checkfirst=True)
        print("✓ knowledge base tables rollback completed")
        return
    raise RuntimeError(f"unsupported action: {action}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate or rollback knowledge base catalog tables")
    parser.add_argument("--action", choices=("migrate", "rollback"), default="migrate")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("=" * 60)
    print("GraphInsight knowledge base tables migration")
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
