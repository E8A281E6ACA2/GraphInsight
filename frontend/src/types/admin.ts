/**
 * 管理系统类型定义
 * 适配新的标准化 API
 */

// ============================================================
// 标准化响应类型
// ============================================================

/**
 * 标准化 API 响应
 */
export interface ApiResponse<T = unknown> {
  code: number;
  message: string;
  data?: T;
  timestamp: string;
  trace_id?: string;
}

/**
 * 错误详情
 */
export interface ErrorDetail {
  error_code: string;
  error_type: string;
  details?: unknown;
}

/**
 * 错误响应
 */
export interface ApiErrorResponse {
  code: number;
  message: string;
  error: ErrorDetail;
  timestamp: string;
}

/**
 * 分页数据
 */
export interface PaginatedData<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

/**
 * 分页响应
 */
export type PaginatedResponse<T> = ApiResponse<PaginatedData<T>>;

// ============================================================
// 认证相关类型
// ============================================================

/**
 * 登录请求
 */
export interface LoginRequest {
  username: string;
  password: string;
}

/**
 * 用户信息
 */
export interface UserInfo {
  id: number;
  username: string;
  email?: string;
  preferred_home_path?: '/admin/dashboard' | '/workspace';
  is_active: boolean;
  created_at: string;
  last_login?: string;
  login_count: number;
}

/**
 * 登录响应数据
 */
export interface LoginResponseData {
  token: string;
  expires_in: number;
  user: UserInfo;
}

/**
 * 修改密码请求
 */
export interface ChangePasswordRequest {
  old_password: string;
  new_password: string;
}

// ============================================================
// 配置相关类型
// ============================================================

/**
 * 配置分类类型
 */
export type ConfigCategory = 'neo4j' | 'ai_service' | 'nl2cypher' | 'retrieval' | 'embedding' | 'vector_store' | 'document_parser';

/**
 * 配置项
 */
export interface ConfigItem {
  id: number;
  category: string;
  key: string;
  value: string;
  description?: string;
  is_sensitive: boolean;
  is_encrypted: boolean;
  updated_by?: number;
  updated_at: string;
  version: number;
}

/**
 * 配置创建请求
 */
export interface ConfigCreateRequest {
  category: string;
  key: string;
  value: string;
  description?: string;
  is_sensitive: boolean;
}

/**
 * 配置更新请求
 */
export interface ConfigUpdateRequest {
  value: string;
  description?: string;
}

/**
 * 配置查询参数
 */
export interface ConfigQueryParams {
  category?: string;
  key?: string;
  is_sensitive?: boolean;
  page?: number;
  page_size?: number;
}

/**
 * 批量更新配置请求
 */
export interface ConfigBatchUpdateRequest {
  configs: Array<{
    category: string;
    key: string;
    value: string;
  }>;
}

/**
 * OpenAI 配置
 */
export interface OpenAIConfig {
  base_url: string;
  api_key: string;
  model: string;
  max_tokens: number;
  temperature: number;
}

/**
 * NL2Cypher 配置
 */
export interface NL2CypherConfig {
  enabled: boolean;
  cache_size: number;
  max_limit: number;
}

/**
 * Neo4j 配置
 */
export interface Neo4jConfig {
  uri: string;
  username: string;
  password: string;
  database: string;
}

export interface ConnectionTestCheck {
  name: string;
  success: boolean;
  message: string;
  latency_ms?: number;
}

export interface ConnectionTestResult {
  success: boolean;
  message: string;
  saved?: boolean;
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
  endpoint?: string | null;
  latency_ms?: number;
  checked_at?: string;
  checks?: ConnectionTestCheck[];
}

export interface ModelCatalogItem {
  provider: string;
  model: string;
  label: string;
  enabled: boolean;
  supports_reasoning: boolean;
  supported_profiles: Array<'fast' | 'balanced' | 'deep' | string>;
  default_profile: 'fast' | 'balanced' | 'deep' | string;
  context_window?: number;
  max_output_tokens?: number;
  suggested_max_tokens?: number;
  token_limit_source?: string;
}

export interface ModelCatalogResponse {
  models: string[];
  catalog: ModelCatalogItem[];
  source?: 'remote' | 'configured' | 'current' | string;
  source_message?: string;
  scenario_profiles?: {
    docqa?: 'fast' | 'balanced' | 'deep' | string;
    deep_research?: 'fast' | 'balanced' | 'deep' | string;
  };
}

// ============================================================
// 监控相关类型
// ============================================================

/**
 * 系统统计
 */
export interface SystemStats {
  cpu_percent: number;
  memory_percent: number;
  memory_used_mb: number;
  memory_total_mb: number;
  disk_percent: number;
  disk_used_gb: number;
  disk_total_gb: number;
  uptime_seconds: number;
  timestamp: string;
}

export interface ApiPathMetric {
  path: string;
  total: number;
  failed: number;
  error_rate: number;
}

export interface PerformanceMetricsData {
  avg_response_time_ms: number;
  p50_response_time_ms: number;
  p95_response_time_ms: number;
  p99_response_time_ms: number;
  requests_per_second: number;
  error_rate: number;
  total_requests: number;
  failed_requests: number;
  window_seconds: number;
  top_paths: ApiPathMetric[];
  timestamp: string;
}

export interface JobSloMetrics {
  window_minutes: number;
  total_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  cancelled_jobs: number;
  running_jobs: number;
  pending_jobs: number;
  timeout_failed_jobs: number;
  success_rate: number;
  timeout_rate: number;
  p95_duration_ms: number;
  p99_duration_ms: number;
  timestamp: string;
}

export interface QATypeMetric {
  qa_type: string;
  total: number;
  failed: number;
  success_rate: number;
  citation_rate: number;
  avg_citations: number;
  p95_latency_ms: number;
}

export interface QAQualityMetrics {
  window_seconds: number;
  total_requests: number;
  failed_requests: number;
  success_rate: number;
  failure_rate: number;
  citation_rate: number;
  avg_citations: number;
  avg_latency_ms: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  p99_latency_ms: number;
  by_type: QATypeMetric[];
  timestamp: string;
}

export type QATraceType = 'docqa' | 'deep_research';
export type QATraceStatus = 'success' | 'failed';

export interface QATraceItem {
  id: number;
  trace_id?: string;
  qa_type: QATraceType;
  status: QATraceStatus;
  question: string;
  operator_id?: number;
  model?: string;
  reasoning_profile?: 'fast' | 'balanced' | 'deep' | string;
  top_k?: number;
  latency_ms?: number;
  retrieval_count: number;
  citation_count: number;
  answer_preview?: string;
  error_message?: string;
  created_at: string;
}

export interface QATraceDetail extends QATraceItem {
  retrieval_snapshot?: unknown;
  generation_snapshot?: unknown;
  response_snapshot?: unknown;
}

export interface RetrievalDiagnosticsModeSummary {
  hit_count: number;
  duration_ms: number;
  source_counts: Record<string, number>;
  skipped_sources: string[];
}

export interface RetrievalDiagnosticsResult {
  query: string;
  top_k: number;
  modes: string[];
  runs: Record<string, {
    items: Array<Record<string, unknown>>;
    trace: Record<string, unknown>;
  }>;
  summary?: {
    modes?: Record<string, RetrievalDiagnosticsModeSummary>;
    best_mode?: string;
    slowest_mode?: string;
    skipped_sources?: Record<string, string[]>;
    recommendations?: string[];
  };
  health?: Record<string, unknown>;
}

export interface QATraceGenerationSnapshot {
  mode?: string;
  model?: string;
  base_url?: string;
  reasoning_profile?: 'fast' | 'balanced' | 'deep' | string;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    source?: string;
  };
}

export interface QATraceQueryParams {
  qa_type?: QATraceType;
  status?: QATraceStatus;
  trace_id?: string;
  operator_id?: number;
  keyword?: string;
  page?: number;
  page_size?: number;
}

export interface QACostModelBreakdown {
  model: string;
  qa_type: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost: number;
  avg_latency_ms: number;
  success_rate: number;
}

export interface QACostSummary {
  window_hours: number;
  total_calls: number;
  success_calls: number;
  failed_calls: number;
  success_rate: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost: number;
  currency: string;
  pricing_source: string;
  models: QACostModelBreakdown[];
}

export interface SloTargetItem {
  value: number | string;
  target: string;
}

export interface SloSnapshot {
  api: PerformanceMetricsData;
  jobs: JobSloMetrics;
  slo: {
    api_error_rate: SloTargetItem;
    job_success_rate: SloTargetItem;
    job_timeout_rate: SloTargetItem;
    job_p95_duration_ms: SloTargetItem;
  };
  timestamp: string;
}

export interface AlertItem {
  type: string;
  severity: 'info' | 'warning' | 'error';
  message: string;
}

export interface AlertCheckResult {
  alerts: AlertItem[];
  alert_count: number;
  sent: boolean;
  delivery_error?: string;
  webhook_configured: boolean;
  snapshot: SloSnapshot;
  timestamp: string;
}

/**
 * 数据库状态
 */
export interface DatabaseStatus {
  connected: boolean;
  database: string;
  tables_count?: number;
  message?: string;
  error?: string;
}

/**
 * Neo4j 状态
 */
export interface Neo4jStatus {
  connected: boolean;
  uri: string;
  database: string;
  nodes_count?: number;
  relationships_count?: number;
  message?: string;
  error?: string;
}

/**
 * 健康状态
 */
/**
 * OpenAI 状态
 */
export interface OpenAIStatus {
  configured: boolean;
  message: string;
  error?: string;
}

/**
 * AI服务状态
 */
export interface AIServiceStatus {
  connected: boolean;
  service_name: string;
  model?: string;
  api_key_configured: boolean;
  error?: string;
}

/**
 * 健康状态
 */
export interface HealthStatus {
  status: 'healthy' | 'degraded' | 'unhealthy';
  timestamp: string;
  database: DatabaseStatus;
  neo4j: Neo4jStatus;
  milvus?: DependencyStatus;
  python_backend?: DependencyStatus;
  ai_service?: AIServiceStatus;
  openai?: OpenAIStatus;
  system?: SystemStats;
  checks: Record<string, boolean>;
}

// ============================================================
// 日志相关类型
// ============================================================

/**
 * 日志项
 */
export interface LogItem {
  id: number;
  user_id?: number;
  operator_id?: number;
  tenant_id?: string;
  trace_id?: string;
  username?: string;
  action: string;
  resource?: string;
  resource_id?: string;
  details?: string;
  ip_address?: string;
  user_agent?: string;
  status: 'success' | 'failed';
  error_message?: string;
  created_at: string;
}

/**
 * 日志详情
 */
export interface LogDetail {
  id: number;
  user_id?: number;
  username?: string;
  action: string;
  resource?: string;
  resource_id?: string;
  details?: Record<string, unknown>;
  ip_address?: string;
  user_agent?: string;
  status: 'success' | 'failed';
  error_message?: string;
  created_at: string;
}

/**
 * 日志查询参数
 */
export interface LogQueryParams {
  user_id?: number;
  action?: string;
  resource?: string;
  status?: 'success' | 'failed';
  trace_id?: string;
  start_date?: string;
  end_date?: string;
  ip_address?: string;
  page?: number;
  page_size?: number;
}

/**
 * 日志统计
 */
export interface LogStats {
  total_logs: number;
  success_count: number;
  failed_count: number;
  success_rate: number;
  action_stats: Record<string, number>;
  user_stats: Record<string, number>;
  hourly_stats: Record<string, number>;
}

export interface DependencyStatus {
  connected: boolean;
  enabled: boolean;
  base_url?: string;
  message?: string;
  error?: string;
}

// ============================================================
// RBAC 相关类型
// ============================================================

export type ScopeType = 'global' | 'tenant' | 'project' | 'kb';

export interface RoleItem {
  id: number;
  name: string;
  description?: string;
  is_system: boolean;
  created_at: string;
  updated_at?: string;
}

export interface PermissionItem {
  id: number;
  code: string;
  resource_type: string;
  action: string;
  description?: string;
  created_at: string;
}

export interface BindingItem {
  id: number;
  user_id: number;
  username?: string;
  email?: string;
  role_id: number;
  role_name: string;
  scope_type: ScopeType;
  tenant_id?: string;
  project_id?: string;
  kb_id?: string;
  expires_at?: string;
  created_by?: number;
  created_at: string;
}

export interface BindingCreateRequest {
  user_id: number;
  role_name: string;
  scope_type: ScopeType;
  tenant_id?: string;
  project_id?: string;
  kb_id?: string;
  expires_at?: string;
}

export interface AdminUserItem {
  id: number;
  username: string;
  email?: string;
  full_name?: string;
  phone?: string;
  department?: string;
  avatar?: string;
  is_active: boolean;
  last_login?: string;
  last_login_ip?: string;
  login_count: number;
  created_at: string;
  updated_at?: string;
}

export interface AdminUserCreateRequest {
  username: string;
  email: string;
  password: string;
  full_name?: string;
  phone?: string;
  department?: string;
}

export interface AdminUserUpdateRequest {
  email?: string;
  full_name?: string;
  phone?: string;
  department?: string;
  avatar?: string;
  is_active?: boolean;
}

export interface AdminUserResetPasswordRequest {
  new_password: string;
}

export interface AdminUserBatchStatusRequest {
  user_ids: number[];
  is_active: boolean;
}

export interface AdminUserBatchDeleteRequest {
  user_ids: number[];
  soft_delete?: boolean;
}

export interface AdminUserBatchResetPasswordRequest {
  user_ids: number[];
  new_password: string;
}

export interface AdminUserBatchStatusResult {
  updated_count: number;
  updated_ids: number[];
  not_found_ids: number[];
  skipped_self_ids: number[];
}

export interface AdminUserBatchDeleteResult {
  deleted_count: number;
  deleted_ids: number[];
  not_found_ids: number[];
  skipped_self_ids: number[];
}

export interface AdminUserBatchResetPasswordResult {
  reset_count: number;
  reset_ids: number[];
  not_found_ids: number[];
  skipped_self_ids: number[];
}

// ============================================================
// 任务中心相关类型
// ============================================================

export type JobType = 'build_graph' | 'clear_kb' | 'reindex';
export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface JobCreateRequest {
  tenant_id?: string;
  project_id?: string;
  kb_id?: string;
  payload?: Record<string, unknown>;
  max_retries?: number;
}

export interface JobItem {
  id: number;
  job_type: JobType;
  status: JobStatus;
  tenant_id?: string;
  project_id?: string;
  kb_id?: string;
  payload: Record<string, unknown>;
  result?: Record<string, unknown>;
  progress?: number;
  error_message?: string;
  retry_count: number;
  max_retries: number;
  requested_by?: number;
  trace_id?: string;
  started_at?: string;
  finished_at?: string;
  created_at: string;
  updated_at?: string;
}

export interface JobQueryParams {
  job_type?: JobType;
  status?: JobStatus;
  tenant_id?: string;
  project_id?: string;
  kb_id?: string;
  page?: number;
  page_size?: number;
}

export interface JobLogItem {
  id: number;
  action: string;
  status: 'success' | 'failed';
  error_message?: string;
  trace_id?: string;
  operator_id?: number;
  created_at: string;
  details?: Record<string, unknown>;
}

// ============================================================
// 错误码常量
// ============================================================

export const ErrorCode = {
  // 通用错误 (1xxx)
  UNKNOWN_ERROR: '1000',
  INVALID_REQUEST: '1001',
  NOT_FOUND: '1002',
  
  // 认证错误 (2xxx)
  UNAUTHORIZED: '2001',
  TOKEN_EXPIRED: '2002',
  INVALID_TOKEN: '2003',
  INVALID_CREDENTIALS: '2004',
  USER_DISABLED: '2005',
  
  // 业务错误 (3xxx)
  BUSINESS_ERROR: '3000',
  RESOURCE_NOT_FOUND: '3001',
  RESOURCE_ALREADY_EXISTS: '3002',
  
  // 验证错误 (4xxx)
  VALIDATION_ERROR: '4001',
  MISSING_PARAMETER: '4002',
  INVALID_PARAMETER: '4003',
  
  // 系统错误 (5xxx)
  INTERNAL_ERROR: '5000',
  DATABASE_ERROR: '5001',
  SERVICE_UNAVAILABLE: '5003',
  
  // 限流错误 (6xxx)
  RATE_LIMIT_EXCEEDED: '6001',
} as const;

export type ErrorCodeType = typeof ErrorCode[keyof typeof ErrorCode];

// ============================================================
// 个人设置相关类型
// ============================================================

/**
 * 个人资料信息
 */
export interface ProfileInfo {
  id: number;
  username: string;
  email: string;
  full_name?: string;
  phone?: string;
  avatar?: string;
  preferred_home_path?: '/admin/dashboard' | '/workspace';
  is_active: boolean;
  created_at: string;
  last_login?: string;
  login_count: number;
  last_login_ip?: string;
}

/**
 * 个人资料更新请求
 */
export interface ProfileUpdateRequest {
  email?: string;
  full_name?: string;
  phone?: string;
  preferred_home_path?: '/admin/dashboard' | '/workspace';
}

/**
 * 修改密码请求（个人设置）
 */
export interface PasswordChangeRequest {
  old_password: string;
  new_password: string;
  confirm_password: string;
}
