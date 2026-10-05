"""
管理系统数据模型
"""
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    Text,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.sql import func
from .database import Base


class AdminUser(Base):
    """管理员用户表 - 所有注册用户都是管理员"""
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    email = Column(String(100), unique=True, nullable=False, index=True)

    # 基础信息
    full_name = Column(String(100))
    avatar = Column(String(255))  # 头像URL
    phone = Column(String(20))
    department = Column(String(100))
    preferred_home_path = Column(String(64), default="/admin/dashboard")

    # 状态
    is_active = Column(Boolean, default=True)

    # 登录信息
    last_login = Column(DateTime(timezone=True))
    last_login_ip = Column(String(45))  # 支持IPv6
    login_count = Column(Integer, default=0)

    # 时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdminConfig(Base):
    """配置表"""
    __tablename__ = "admin_configs"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(50), nullable=False, index=True)
    key = Column(String(100), nullable=False)
    value = Column(Text, nullable=False)
    description = Column(Text)
    is_sensitive = Column(Boolean, default=False)
    is_encrypted = Column(Boolean, default=False)  # 新增：是否加密
    updated_by = Column(Integer, ForeignKey("admin_users.id"))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    version = Column(Integer, default=1)  # 新增：版本号

    __table_args__ = (
        UniqueConstraint("category", "key", name="uq_admin_config_category_key"),
        {'sqlite_autoincrement': True},
    )


class AdminLog(Base):
    """操作日志表"""
    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("admin_users.id"))
    operator_id = Column(Integer, ForeignKey("admin_users.id"), index=True)
    tenant_id = Column(String(100), index=True)
    project_id = Column(String(100), index=True)
    kb_id = Column(String(100), index=True)
    trace_id = Column(String(100), index=True)
    action = Column(String(100), nullable=False, index=True)
    resource = Column(String(100), index=True)
    resource_id = Column(String(100))  # 新增：资源ID
    details = Column(Text)
    ip_address = Column(String(50))
    user_agent = Column(String(500))  # 新增：User Agent
    status = Column(String(20), default="success", index=True)  # 新增：状态
    error_message = Column(Text)  # 新增：错误信息
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class AdminRole(Base):
    """角色表"""
    __tablename__ = "admin_roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(Text)
    is_system = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdminPermission(Base):
    """权限表"""
    __tablename__ = "admin_permissions"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), unique=True, nullable=False, index=True)
    resource_type = Column(String(50), nullable=False, index=True)
    action = Column(String(50), nullable=False, index=True)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminRolePermission(Base):
    """角色权限关联表"""
    __tablename__ = "admin_role_permissions"

    id = Column(Integer, primary_key=True, index=True)
    role_id = Column(Integer, ForeignKey("admin_roles.id", ondelete="CASCADE"), nullable=False)
    permission_id = Column(Integer, ForeignKey("admin_permissions.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_admin_role_permission"),
    )


class AdminUserRoleBinding(Base):
    """用户角色绑定（支持资源作用域）"""
    __tablename__ = "admin_user_role_bindings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id = Column(Integer, ForeignKey("admin_roles.id", ondelete="CASCADE"), nullable=False, index=True)
    scope_type = Column(String(20), nullable=False, default="global", index=True)  # global/tenant/project/kb
    tenant_id = Column(String(100), nullable=True, index=True)
    project_id = Column(String(100), nullable=True, index=True)
    kb_id = Column(String(100), nullable=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "role_id",
            "scope_type",
            "tenant_id",
            "project_id",
            "kb_id",
            name="uq_admin_user_role_scope",
        ),
        Index("idx_admin_user_role_expire", "user_id", "expires_at"),
    )


class AdminJob(Base):
    """后台任务表"""
    __tablename__ = "admin_jobs"

    id = Column(Integer, primary_key=True, index=True)
    job_type = Column(String(50), nullable=False, index=True)  # build_graph/clear_kb/reindex/reindex_chunks
    status = Column(String(20), nullable=False, default="pending", index=True)
    tenant_id = Column(String(100), nullable=True, index=True)
    project_id = Column(String(100), nullable=True, index=True)
    kb_id = Column(String(100), nullable=True, index=True)
    payload = Column(Text)
    result = Column(Text)
    error_message = Column(Text)
    # §16.3 幂等去重键（admin/migrate_jobs_targets_hash.py 建的列 + 部分唯一索引
    # uq_admin_jobs_targets_hash）。这里补的是 ORM 与已迁移库结构的对齐，不是新增迁移：
    # 缺这一列时 ORM 读出的行没有 targets_hash，任务中心无法判断"同批 targets 已在排队"。
    # 不写 index=True：§16.3 唯一的索引是那条部分唯一索引（迁移脚本用原生 SQL 建），
    # ORM 再声明单列索引会让 create_all 出的库多出一个已迁移库没有的 ix_admin_jobs_targets_hash，
    # 而 SQLite 的 ALTER TABLE ... DROP COLUMN 在列仍被索引引用时直接报错，把迁移回滚堵死。
    targets_hash = Column(String(64), nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    max_retries = Column(Integer, nullable=False, default=3)
    requested_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)
    trace_id = Column(String(100), nullable=True, index=True)
    claimed_by = Column(String(100), nullable=True, index=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class KnowledgeBase(Base):
    """知识库（一等资源，契约 §4.1：tenant -> project -> KnowledgeBase）"""
    __tablename__ = "knowledge_bases"

    id = Column(String(64), primary_key=True)  # 服务端 UUID，稳定且不可复用
    tenant_id = Column(String(100), nullable=False, index=True)
    project_id = Column(String(100), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(100))
    description = Column(Text)
    status = Column(String(20), nullable=False, default="active", index=True)  # active/archived/deleting
    storage_prefix = Column(String(255), nullable=False)  # 系统生成的安全前缀，禁止路径逃逸
    parser_profile = Column(Text)  # JSON 快照
    retrieval_profile = Column(Text)  # JSON 快照
    created_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # SQLAlchemy Declarative 保留 metadata 属性，列名仍为 metadata
    extra_metadata = Column("metadata", Text)

    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "name", name="uq_knowledge_base_scope_name"),
        Index("idx_knowledge_base_project_status", "tenant_id", "project_id", "status"),
    )


class KnowledgeBaseDocument(Base):
    """知识库文档元数据（契约 §4.2：文件系统的权威内容 + 本表的权威元数据）"""
    __tablename__ = "knowledge_base_documents"

    id = Column(Integer, primary_key=True, index=True)
    doc_id = Column(String(64), nullable=False, unique=True, index=True)  # 服务端 UUID，与路径解耦
    kb_id = Column(String(100), nullable=False, index=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    project_id = Column(String(100), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    relative_path = Column(String(500), nullable=False)  # 仅允许 storage_prefix 下相对路径
    source_type = Column(String(20), nullable=False, default="upload", index=True)  # upload/url/connector/import
    source_uri = Column(String(500))
    mime_type = Column(String(120))
    size = Column(BigInteger, nullable=False, default=0)
    sha256 = Column(String(64), nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="uploaded", index=True)  # uploaded/parsing/indexed/failed/archived
    parser_provider = Column(String(50))
    parser_version = Column(String(50))
    graph_status = Column(String(20), nullable=False, default="pending", index=True)  # pending/stale/indexed/failed
    vector_status = Column(String(20), nullable=False, default="pending", index=True)
    error_summary = Column(Text)
    created_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("kb_id", "sha256", "version", name="uq_kb_document_content_version"),
        Index("idx_kb_document_kb_status", "kb_id", "status"),
    )


class AdminQATrace(Base):
    """问答链路追踪表"""
    __tablename__ = "admin_qa_traces"

    id = Column(Integer, primary_key=True, index=True)
    trace_id = Column(String(100), nullable=True, index=True)
    qa_type = Column(String(50), nullable=False, index=True)  # docqa/deep_research
    status = Column(String(20), nullable=False, default="success", index=True)
    question = Column(Text, nullable=False)
    operator_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)
    tenant_id = Column(String(100), nullable=True, index=True)
    project_id = Column(String(100), nullable=True, index=True)
    kb_id = Column(String(100), nullable=True, index=True)
    model = Column(String(120), nullable=True)
    top_k = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    retrieval_count = Column(Integer, nullable=False, default=0)
    citation_count = Column(Integer, nullable=False, default=0)
    answer_preview = Column(Text, nullable=True)
    retrieval_snapshot = Column(Text, nullable=True)
    generation_snapshot = Column(Text, nullable=True)
    response_snapshot = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    __table_args__ = (
        Index("idx_admin_qa_trace_type_created", "qa_type", "created_at"),
        Index("idx_admin_qa_trace_status_created", "status", "created_at"),
    )
