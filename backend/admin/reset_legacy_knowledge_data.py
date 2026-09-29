"""
全新初始化前的旧知识数据受控清理（一次性运维动作）

契约：docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md §2.11、§5
口径：本项目按全新知识库初始化——不迁移旧文档、不生成注册表映射、
不把旧图谱/旧向量挂到新 KB。部署前若发现旧知识数据，先 dry-run 统计，
再用显式确认 token 定向清理。

清理范围（仅知识数据）：
1. 文档文件:   DOCUMENT_STORAGE_PATH（含 .trash 与回退目录 backend/documents）
2. 解析产物:   PARSED_DOCUMENT_STORAGE_PATH
3. Neo4j:      source='document_ingest' 或带 doc_id 的节点/关系
4. Milvus:     配置的向量 collection（仅当 VECTOR_STORE_ENABLED）

明确不触碰：admin 用户、角色、权限、绑定、配置、任务、QA trace 等任何管理面数据。

用法：
    python backend/admin/reset_legacy_knowledge_data.py --dry-run
    python backend/admin/reset_legacy_knowledge_data.py --confirm RESET_LEGACY_KNOWLEDGE_DATA
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

load_dotenv(find_dotenv(), override=True)

from config import get_settings  # noqa: E402

settings = get_settings()

CONFIRM_TOKEN = "RESET_LEGACY_KNOWLEDGE_DATA"
SUPPORTED_EXTS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".docx", ".pdf"}


@dataclass
class CategoryReport:
    name: str
    scanned: int = 0
    deleted: int = 0
    failed: int = 0
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.name}: scanned={self.scanned} deleted={self.deleted} failed={self.failed}"


def _iter_document_files(root: Path, report: CategoryReport) -> list:
    files: list = []
    if not root.exists():
        report.notes.append(f"目录不存在，跳过: {root}")
        return files
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
            files.append(path)
    report.scanned = len(files)
    return files


def clean_document_files(dry_run: bool) -> CategoryReport:
    report = CategoryReport("文档文件")
    roots = [Path(settings.document_storage_path)]
    fallback = backend_dir / "documents"
    if fallback.exists() and fallback not in roots:
        roots.append(fallback)
    for root in roots:
        for path in _iter_document_files(root, report):
            report.scanned += 0  # scanned 已在 _iter_document_files 统计
            try:
                if not dry_run:
                    path.unlink()
                report.deleted += 1
            except OSError as exc:
                report.failed += 1
                report.notes.append(f"删除失败 {path}: {exc}")
        # .trash 与空目录整体移除
        trash = root / ".trash"
        if trash.exists():
            file_count = sum(1 for item in trash.rglob("*") if item.is_file())
            report.scanned += file_count
            if not dry_run:
                shutil.rmtree(trash, ignore_errors=True)
            report.deleted += file_count
        if dry_run:
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
    return report


def clean_parsed_artifacts(dry_run: bool) -> CategoryReport:
    report = CategoryReport("解析产物")
    root = Path(settings.parsed_document_storage_path)
    if not root.exists():
        report.notes.append(f"目录不存在，跳过: {root}")
        return report
    for path in root.iterdir():
        if path.name.lower() == "readme.md" and path.is_file():
            report.notes.append("保留 README.md")
            continue
        if path.is_dir():
            report.scanned += sum(1 for item in path.rglob("*") if item.is_file()) + 1
        else:
            report.scanned += 1
        try:
            if not dry_run:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            report.deleted += 1
        except OSError as exc:
            report.failed += 1
            report.notes.append(f"删除失败 {path}: {exc}")
    return report


def clean_neo4j(dry_run: bool) -> CategoryReport:
    report = CategoryReport("Neo4j 图谱(document_ingest)")
    try:
        from neo4j import GraphDatabase
    except ImportError:
        report.notes.append("neo4j driver 未安装，跳过")
        return report
    if not getattr(settings, "neo4j_uri", ""):
        report.notes.append("未配置 Neo4j，跳过")
        return report
    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=10,
        )
        with driver.session(database=getattr(settings, "neo4j_database", None) or None) as session:
            count_row = session.run(
                "MATCH (n) WHERE n.source = 'document_ingest' OR n.doc_id IS NOT NULL "
                "RETURN count(n) AS c"
            ).single()
            report.scanned = int(count_row["c"]) if count_row else 0
            rel_row = session.run(
                "MATCH (:Entity)-[r]->(:Entity) WHERE r.doc_id IS NOT NULL RETURN count(r) AS c"
            ).single()
            report.notes.append(f"待删除关系数: {int(rel_row['c']) if rel_row else 0}")
            if not dry_run and report.scanned:
                session.run(
                    "MATCH (:Entity)-[r]->(:Entity) WHERE r.doc_id IS NOT NULL DELETE r"
                )
                session.run(
                    "MATCH (n) WHERE n.source = 'document_ingest' OR n.doc_id IS NOT NULL "
                    "DETACH DELETE n"
                )
            report.deleted = report.scanned if not dry_run else 0
        driver.close()
    except Exception as exc:  # noqa: BLE001 - 连接失败必须显式报告
        report.failed += 1
        report.notes.append(f"Neo4j 清理失败: {exc}")
    return report


def clean_milvus(dry_run: bool) -> CategoryReport:
    report = CategoryReport("Milvus 向量")
    enabled = str(getattr(settings, "vector_store_enabled", "false")).lower() in {"1", "true", "yes", "on"}
    if not enabled:
        report.notes.append("VECTOR_STORE_ENABLED 未开启，跳过")
        return report
    try:
        from pymilvus import MilvusClient
    except ImportError:
        report.notes.append("pymilvus 未安装，跳过")
        return report
    collection = getattr(settings, "milvus_collection", "graphinsight_chunks")
    try:
        client = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or "", db_name=settings.milvus_db_name)
        if not client.has_collection(collection):
            report.notes.append(f"collection 不存在，跳过: {collection}")
            return report
        stats = client.get_collection_stats(collection)
        report.scanned = int(stats.get("row_count", 0))
        if not dry_run:
            client.drop_collection(collection)
            report.deleted = report.scanned
    except Exception as exc:  # noqa: BLE001
        report.failed += 1
        report.notes.append(f"Milvus 清理失败: {exc}")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-time controlled cleanup of legacy knowledge data")
    parser.add_argument("--dry-run", action="store_true", help="只统计并报告，不删除")
    parser.add_argument("--confirm", default="", help=f"执行删除必须传 {CONFIRM_TOKEN}")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dry_run = args.dry_run
    print("=" * 60)
    print("GraphInsight 旧知识数据受控清理（全新初始化专用）")
    print("=" * 60)
    print(f"文档目录: {settings.document_storage_path}")
    print(f"解析产物: {settings.parsed_document_storage_path}")
    print(f"Neo4j: {settings.neo4j_uri}")
    print(f"Milvus: {settings.milvus_uri}/{settings.milvus_collection}")
    if not dry_run and args.confirm != CONFIRM_TOKEN:
        print("✗ 拒绝执行：删除操作必须显式传 --confirm RESET_LEGACY_KNOWLEDGE_DATA")
        return 2

    reports = [
        clean_document_files(dry_run),
        clean_parsed_artifacts(dry_run),
        clean_neo4j(dry_run),
        clean_milvus(dry_run),
    ]
    mode = "DRY-RUN（未删除任何数据）" if dry_run else "已执行删除"
    print("-" * 60)
    print(f"模式: {mode}")
    for report in reports:
        print(report.summary())
        for note in report.notes:
            print(f"  - {note}")
    total_failed = sum(report.failed for report in reports)
    if total_failed:
        print(f"✗ 存在 {total_failed} 项失败，请检查后重试")
        return 1
    print("✓ 完成" if not dry_run else "✓ dry-run 完成，确认无误后使用 --confirm 执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
