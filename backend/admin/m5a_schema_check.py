"""
M5-A 迁移结构与索引校验（审计补充项：schema/index 结构校验）。

migrate 之后不能只看 "CREATE 语句跑成功了"，必须回读数据库实际结构，逐项核对
冻结契约（设计 §3 / §15.1 / §16.3）：列存在性、类型、可空性、主键、UNIQUE 约束、
普通索引的列顺序、部分唯一索引的列顺序与 WHERE 谓词。

被两个迁移脚本（migrate 后自动执行，失败即非零退出）与活栈检查脚本共同复用，
避免校验逻辑与迁移 DDL 分家后各自漂移。

运行：python backend/admin/m5a_schema_check.py            # 双脚本校验
      python backend/admin/m5a_schema_check.py chunk_revisions
"""
from __future__ import annotations

import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import inspect, text  # noqa: E402

TABLE = "chunk_revisions"
CURRENT_UNIQUE_INDEX = "uq_chunk_revisions_current"
UNIQUE_CONSTRAINT = "uq_chunk_revisions_rev"

JOBS_TABLE = "admin_jobs"
JOBS_COLUMN = "targets_hash"
JOBS_INDEX = "uq_admin_jobs_targets_hash"

# PG information_schema 回读为 "character varying"，sqlite PRAGMA 回读为 "VARCHAR"，
# 两者都算符合契约；类型断言必须按回读文本的别名集合判定，不能只认字面 varchar。
VARCHAR_TYPES = ("varchar", "character varying")

# 列规格：(name, 允许类型集合, 是否可空)
# 类型取数据库回读的规范化文本（PG 为 information_schema data_type，sqlite 为 PRAGMA type 大写）
COLUMN_SPECS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("revision_id", ("integer", "bigint", "int", "int8", "serial", "int4"), False),
    ("kb_id", VARCHAR_TYPES, False),
    ("tenant_id", VARCHAR_TYPES, False),
    ("project_id", VARCHAR_TYPES, False),
    ("doc_id", VARCHAR_TYPES, False),
    ("chunk_id", VARCHAR_TYPES, False),
    ("source_content", ("text",), False),
    ("source_content_hash", VARCHAR_TYPES, False),
    ("content", ("text",), False),
    ("content_hash", VARCHAR_TYPES, False),
    ("content_revision", ("integer", "int", "int4"), False),
    ("revision_status", VARCHAR_TYPES, False),
    ("graph_status", VARCHAR_TYPES, False),
    ("vector_status", VARCHAR_TYPES, False),
    ("graph_content_revision", ("integer", "int", "int4"), True),
    ("vector_content_revision", ("integer", "int", "int4"), True),
    ("revision_source", VARCHAR_TYPES, False),
    ("source_version", VARCHAR_TYPES, True),
    ("parser_version", VARCHAR_TYPES, True),
    ("edited_by", ("integer", "int", "int4"), True),
    ("edited_at", ("timestamp", "timestamp with time zone", "timestamp without time zone", "timestamptz", "datetime"), False),
    ("reason", VARCHAR_TYPES, True),
    ("trace_id", VARCHAR_TYPES, True),
)

# 索引规格：name -> (是否唯一, 列顺序, 谓词必须包含的归一化片段)
# 片段匹配前先去掉空格：PG 回读为 "((revision_status)::text = 'current'::text)"，
# sqlite 为 "revision_status = 'current'"，两者都含 "revision_status" 与 "'current'"。
INDEX_SPECS: dict[str, tuple[bool, tuple[str, ...], tuple[str, ...]]] = {
    "idx_chunk_rev_kb_chunk_status": (False, ("kb_id", "chunk_id", "revision_status"), ()),
    "idx_chunk_rev_kb_status_graph": (False, ("kb_id", "revision_status", "graph_status"), ()),
    "idx_chunk_rev_kb_status_vector": (False, ("kb_id", "revision_status", "vector_status"), ()),
    "idx_chunk_rev_kb_doc_graph": (False, ("kb_id", "doc_id", "revision_status", "graph_status"), ()),
    "idx_chunk_rev_kb_doc_vector": (False, ("kb_id", "doc_id", "revision_status", "vector_status"), ()),
    CURRENT_UNIQUE_INDEX: (True, ("kb_id", "chunk_id"), ("revision_status", "'current'")),
}


def _dialect(conn) -> str:
    return str(conn.engine.dialect.name)


def _norm_type(raw: str) -> str:
    value = str(raw or "").strip().lower()
    return value.split("(")[0].strip()


def _pg_columns(conn, table: str) -> dict[str, tuple[str, bool]]:
    rows = conn.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = CURRENT_SCHEMA()"
        ),
        {"t": table},
    ).fetchall()
    return {str(r[0]): (_norm_type(r[1]), str(r[2]).upper() == "YES") for r in rows}


def _sqlite_columns(conn, table: str) -> dict[str, tuple[str, bool]]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    # row = (cid, name, type, notnull, dflt_value, pk)
    return {str(r[1]): (_norm_type(r[2]), not bool(r[3]) and not bool(r[5])) for r in rows}


def _table_columns(conn, table: str) -> dict[str, tuple[str, bool]]:
    return _pg_columns(conn, table) if _dialect(conn) == "postgresql" else _sqlite_columns(conn, table)


_PG_INDEX_SQL = """
SELECT c.relname AS indexname,
       i.indisunique AS is_unique,
       (
           SELECT array_agg(a.attname ORDER BY k.ord)
           FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_attribute a
             ON a.attrelid = i.indrelid AND a.attnum = k.attnum
       ) AS cols,
       COALESCE(pg_get_expr(i.indpred, i.indrelid), '') AS predicate
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname = :t AND n.nspname = CURRENT_SCHEMA()
"""


def _pg_indexes(conn, table: str) -> dict[str, tuple[bool, list[str], str]]:
    """PG 侧不解析 indexdef 文本：直接读 pg_index/pg_attribute 的权威列序与谓词。

    文本解析在部分索引上必然出错（indexdef 的列括号后还跟着 WHERE 子句括号），
    真机 PostgreSQL 已验证；表达式索引列（attnum=0）无法命名，按缺列处理。
    """
    rows = conn.execute(text(_PG_INDEX_SQL), {"t": table}).fetchall()
    result: dict[str, tuple[bool, list[str], str]] = {}
    for name, is_unique, cols, predicate in rows:
        columns = [str(col).lower() for col in (cols or []) if col is not None]
        result[str(name)] = (bool(is_unique), columns, str(predicate or "").lower())
    return result


def _sqlite_indexes(conn, table: str) -> dict[str, tuple[bool, list[str], str]]:
    rows = conn.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=:t"),
        {"t": table},
    ).fetchall()
    result: dict[str, tuple[bool, list[str], str]] = {}
    for name, sql_text in rows:
        index_name = str(name)
        definition = str(sql_text or "")
        columns = [str(c[2]).strip().strip('"').lower() for c in _sqlite_index_cols(conn, index_name) if c[2] is not None]
        if definition:
            lowered = definition.lower()
            unique = "unique index" in lowered
            predicate = lowered.split("where", 1)[1].strip() if "where" in lowered else ""
            result[index_name] = (unique, columns, predicate)
        elif columns:
            # 隐式索引（UNIQUE 约束/主键自动索引）：sql 为空，但列序可回读，唯一性恒为真
            result[index_name] = (True, columns, "")
    return result


def _sqlite_index_cols(conn, index_name: str) -> list:
    safe = str(index_name).replace("'", "''")
    return conn.execute(text(f"PRAGMA index_info('{safe}')")).fetchall()


def _index_map(conn, table: str) -> dict[str, tuple[bool, list[str], str]]:
    return _pg_indexes(conn, table) if _dialect(conn) == "postgresql" else _sqlite_indexes(conn, table)


def _sqlite_table_sql(conn, table: str) -> str:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}
    ).fetchone()
    return str(row[0] or "") if row else ""


def _pg_unique_constraint(conn, table: str, name: str, columns: tuple[str, ...]) -> tuple[bool, str]:
    """按 pg_constraint 核对表级 UNIQUE 约束名与列顺序（不依赖同名索引的文本形态）。"""
    row = conn.execute(
        text(
            """
            SELECT con.conname,
                   (
                       SELECT array_agg(a.attname ORDER BY k.ord)
                       FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                   ) AS cols
            FROM pg_constraint con
            JOIN pg_class t ON t.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE t.relname = :t AND n.nspname = CURRENT_SCHEMA()
              AND con.contype = 'u' AND con.conname = :name
            """
        ),
        {"t": table, "name": name},
    ).fetchone()
    if row is None:
        return False, "constraint missing"
    actual = tuple(str(col).lower() for col in (row[1] or []) if col is not None)
    return actual == tuple(c.lower() for c in columns), f"expected {columns}, got {actual}"


def check_chunk_revisions_structure(conn) -> list[tuple[str, bool, str]]:
    """返回逐项校验结果 (检查名, 是否通过, 详情)。"""
    results: list[tuple[str, bool, str]] = []
    dialect = _dialect(conn)
    columns = _table_columns(conn, TABLE)
    results.append(("表 chunk_revisions 存在", bool(columns), f"dialect={dialect}"))

    for name, allowed, nullable in COLUMN_SPECS:
        if name not in columns:
            results.append((f"列 {name} 存在", False, "missing"))
            continue
        actual_type, actual_nullable = columns[name]
        results.append(
            (
                f"列 {name} 类型",
                actual_type in allowed,
                f"expected one of {allowed}, got {actual_type}",
            )
        )
        results.append(
            (
                f"列 {name} 可空性",
                actual_nullable == nullable,
                f"expected nullable={nullable}, got nullable={actual_nullable}",
            )
        )

    indexes = _index_map(conn, TABLE)
    if dialect == "sqlite":
        table_sql = _sqlite_table_sql(conn, TABLE).lower()
        constraint_ok = (
            "unique (kb_id, chunk_id, content_revision)" in table_sql.replace("`", "").replace("  ", " ")
            or "unique(kb_id, chunk_id, content_revision)" in table_sql
        )
        results.append((f"UNIQUE 约束 {UNIQUE_CONSTRAINT}", constraint_ok, table_sql[:200]))
    else:
        constraint_ok, constraint_detail = _pg_unique_constraint(
            conn, TABLE, UNIQUE_CONSTRAINT, ("kb_id", "chunk_id", "content_revision")
        )
        results.append((f"UNIQUE 约束 {UNIQUE_CONSTRAINT}", constraint_ok, constraint_detail))

    for name, (unique, expected_cols, fragments) in INDEX_SPECS.items():
        entry = indexes.get(name)
        if entry is None:
            results.append((f"索引 {name} 存在", False, "missing"))
            continue
        actual_unique, actual_cols, actual_predicate = entry
        results.append((f"索引 {name} 唯一性", actual_unique == unique, f"expected unique={unique}, got {actual_unique}"))
        results.append(
            (
                f"索引 {name} 列顺序",
                tuple(actual_cols) == tuple(expected_cols),
                f"expected {expected_cols}, got {tuple(actual_cols)}",
            )
        )
        if fragments:
            squeezed = actual_predicate.replace(" ", "")
            missing = [f for f in fragments if f.replace(" ", "") not in squeezed]
            results.append((f"索引 {name} 部分谓词", not missing, f"expected fragments {fragments}, got '{actual_predicate}'"))
    return results


def check_targets_hash_structure(conn) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    columns = _table_columns(conn, JOBS_TABLE)
    results.append((f"表 {JOBS_TABLE} 存在", bool(columns), ""))
    entry = columns.get(JOBS_COLUMN)
    results.append((f"列 {JOBS_TABLE}.{JOBS_COLUMN} 存在", entry is not None, "missing"))
    if entry is not None:
        results.append(("列 targets_hash 类型 varchar", entry[0] in VARCHAR_TYPES, f"got {entry[0]}"))
        results.append(("列 targets_hash 可空", entry[1] is True, f"got nullable={entry[1]}"))
    indexes = _index_map(conn, JOBS_TABLE)
    index_entry = indexes.get(JOBS_INDEX)
    results.append((f"索引 {JOBS_INDEX} 存在", index_entry is not None, "missing"))
    if index_entry is not None:
        unique, cols, predicate = index_entry
        results.append(("索引 targets_hash 唯一性", unique is True, f"got unique={unique}"))
        results.append(
            (
                "索引 targets_hash 列顺序",
                tuple(cols) == ("job_type", "kb_id", "targets_hash"),
                f"got {tuple(cols)}",
            )
        )
        squeezed = predicate.replace(" ", "")
        results.append(
            (
                "索引 targets_hash 部分谓词",
                "targets_hash" in squeezed and "isnotnull" in squeezed,
                f"got '{predicate}'",
            )
        )
    return results


def print_results(title: str, results: list[tuple[str, bool, str]]) -> bool:
    print(f"[schema-check] {title}")
    failed = 0
    for name, ok, detail in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}" + ("" if ok or not detail else f" ({detail})"))
        if not ok:
            failed += 1
    return failed == 0


def main() -> int:
    from admin.database import engine  # noqa: E402

    scope = sys.argv[1] if len(sys.argv) > 1 else "both"
    ok = True
    with engine.connect() as conn:
        if scope in ("both", "chunk_revisions"):
            ok = print_results("chunk_revisions", check_chunk_revisions_structure(conn)) and ok
        if scope in ("both", "targets_hash"):
            ok = print_results("admin_jobs.targets_hash", check_targets_hash_structure(conn)) and ok
    if not ok:
        print("✗ schema structure validation FAILED")
        return 1
    print("✓ schema structure validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
