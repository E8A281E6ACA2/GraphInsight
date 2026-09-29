package adminstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"

	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/scope"
)

type Client struct {
	db                  *sql.DB
	rbacEnabled         bool
	failOpenWhenUnbound bool
}

type permissionBinding struct {
	PermissionCode string
	RoleName       string
	ScopeType      string
	TenantID       string
	ProjectID      string
	KBID           string
}

type RbacBinding struct {
	ID        int        `json:"id"`
	UserID    int        `json:"user_id"`
	Username  *string    `json:"username"`
	Email     *string    `json:"email"`
	RoleID    int        `json:"role_id"`
	RoleName  string     `json:"role_name"`
	ScopeType string     `json:"scope_type"`
	TenantID  *string    `json:"tenant_id"`
	ProjectID *string    `json:"project_id"`
	KBID      *string    `json:"kb_id"`
	ExpiresAt *time.Time `json:"expires_at"`
	CreatedBy *int       `json:"created_by"`
	CreatedAt time.Time  `json:"created_at"`
}

type RbacRole struct {
	ID          int        `json:"id"`
	Name        string     `json:"name"`
	Description *string    `json:"description,omitempty"`
	IsSystem    bool       `json:"is_system"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   *time.Time `json:"updated_at,omitempty"`
}

type RbacPermission struct {
	ID           int       `json:"id"`
	Code         string    `json:"code"`
	ResourceType string    `json:"resource_type"`
	Action       string    `json:"action"`
	Description  *string   `json:"description,omitempty"`
	CreatedAt    time.Time `json:"created_at"`
}

type UserItem struct {
	ID                int        `json:"id"`
	Username          string     `json:"username"`
	Email             string     `json:"email"`
	FullName          *string    `json:"full_name"`
	Phone             *string    `json:"phone"`
	Department        *string    `json:"department"`
	Avatar            *string    `json:"avatar"`
	PreferredHomePath *string    `json:"preferred_home_path,omitempty"`
	IsActive          bool       `json:"is_active"`
	LastLogin         *time.Time `json:"last_login"`
	LastLoginIP       *string    `json:"last_login_ip"`
	LoginCount        int        `json:"login_count"`
	CreatedAt         time.Time  `json:"created_at"`
	UpdatedAt         *time.Time `json:"updated_at"`
}

type ProfileStats struct {
	TotalLogins     int        `json:"total_logins"`
	RecentLogins30D int        `json:"recent_logins_30d"`
	TotalOperations int        `json:"total_operations"`
	LastLogin       *time.Time `json:"last_login"`
	LastLoginIP     *string    `json:"last_login_ip"`
	AccountCreated  time.Time  `json:"account_created"`
}

type UserListQuery struct {
	Page       int
	PageSize   int
	Search     string
	IsActive   *bool
	Department string
	OrderBy    string
	OrderDesc  bool
}

type UserListResult struct {
	Items []UserItem
	Total int
}

func New(cfg config.Config) (*Client, error) {
	rawURL := strings.TrimSpace(cfg.AdminDatabaseURL)
	if rawURL == "" {
		return nil, fmt.Errorf("admin database url is empty")
	}
	db, err := sql.Open("pgx", rawURL)
	if err != nil {
		return nil, fmt.Errorf("open admin database failed: %w", err)
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(30 * time.Minute)

	return &Client{
		db:                  db,
		rbacEnabled:         cfg.RBACEnable,
		failOpenWhenUnbound: cfg.RBACFailOpenWhenUnbound,
	}, nil
}

func (c *Client) CheckHealth(ctx context.Context) error {
	if c == nil || c.db == nil {
		return errors.New("admin store is not initialized")
	}
	return c.db.PingContext(ctx)
}

func (c *Client) Close() error {
	if c == nil || c.db == nil {
		return nil
	}
	return c.db.Close()
}

func (c *Client) ListRbacBindings(ctx context.Context, userID *int) ([]RbacBinding, error) {
	if c == nil || c.db == nil {
		return nil, errors.New("admin store is not initialized")
	}

	query := `
		SELECT
			b.id,
			b.user_id,
			u.username,
			u.email,
			b.role_id,
			r.name,
			COALESCE(b.scope_type, 'global'),
			b.tenant_id,
			b.project_id,
			b.kb_id,
			b.expires_at,
			b.created_by,
			b.created_at
		FROM admin_user_role_bindings b
		JOIN admin_roles r ON r.id = b.role_id
		JOIN admin_users u ON u.id = b.user_id
	`
	args := []interface{}{}
	if userID != nil {
		query += " WHERE b.user_id = $1"
		args = append(args, *userID)
	}
	query += " ORDER BY b.created_at DESC"

	rows, err := c.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("query rbac bindings failed: %w", err)
	}
	defer rows.Close()

	bindings := []RbacBinding{}
	for rows.Next() {
		var binding RbacBinding
		var username sql.NullString
		var email sql.NullString
		var tenantID sql.NullString
		var projectID sql.NullString
		var kbID sql.NullString
		var expiresAt sql.NullTime
		var createdBy sql.NullInt64
		if err := rows.Scan(
			&binding.ID,
			&binding.UserID,
			&username,
			&email,
			&binding.RoleID,
			&binding.RoleName,
			&binding.ScopeType,
			&tenantID,
			&projectID,
			&kbID,
			&expiresAt,
			&createdBy,
			&binding.CreatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan rbac binding failed: %w", err)
		}
		binding.Username = stringPtrFromNull(username)
		binding.Email = stringPtrFromNull(email)
		binding.TenantID = stringPtrFromNull(tenantID)
		binding.ProjectID = stringPtrFromNull(projectID)
		binding.KBID = stringPtrFromNull(kbID)
		if expiresAt.Valid {
			value := expiresAt.Time
			binding.ExpiresAt = &value
		}
		if createdBy.Valid {
			value := int(createdBy.Int64)
			binding.CreatedBy = &value
		}
		bindings = append(bindings, binding)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate rbac bindings failed: %w", err)
	}
	return bindings, nil
}

func (c *Client) ListRbacRoles(ctx context.Context) ([]RbacRole, error) {
	if c == nil || c.db == nil {
		return nil, errors.New("admin store is not initialized")
	}
	rows, err := c.db.QueryContext(ctx, `
		SELECT
			id,
			name,
			description,
			is_system,
			created_at,
			updated_at
		FROM admin_roles
		ORDER BY name ASC
	`)
	if err != nil {
		return nil, fmt.Errorf("query rbac roles failed: %w", err)
	}
	defer rows.Close()

	roles := []RbacRole{}
	for rows.Next() {
		var role RbacRole
		var description sql.NullString
		var updatedAt sql.NullTime
		if err := rows.Scan(
			&role.ID,
			&role.Name,
			&description,
			&role.IsSystem,
			&role.CreatedAt,
			&updatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan rbac role failed: %w", err)
		}
		role.Description = stringPtrFromNull(description)
		if updatedAt.Valid {
			value := updatedAt.Time
			role.UpdatedAt = &value
		}
		roles = append(roles, role)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate rbac roles failed: %w", err)
	}
	return roles, nil
}

func (c *Client) ListRbacPermissions(ctx context.Context) ([]RbacPermission, error) {
	if c == nil || c.db == nil {
		return nil, errors.New("admin store is not initialized")
	}
	rows, err := c.db.QueryContext(ctx, `
		SELECT
			id,
			code,
			resource_type,
			action,
			description,
			created_at
		FROM admin_permissions
		ORDER BY code ASC
	`)
	if err != nil {
		return nil, fmt.Errorf("query rbac permissions failed: %w", err)
	}
	defer rows.Close()

	permissions := []RbacPermission{}
	for rows.Next() {
		var permission RbacPermission
		var description sql.NullString
		if err := rows.Scan(
			&permission.ID,
			&permission.Code,
			&permission.ResourceType,
			&permission.Action,
			&description,
			&permission.CreatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan rbac permission failed: %w", err)
		}
		permission.Description = stringPtrFromNull(description)
		permissions = append(permissions, permission)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate rbac permissions failed: %w", err)
	}
	return permissions, nil
}

func (c *Client) ListUsers(ctx context.Context, query UserListQuery) (UserListResult, error) {
	if c == nil || c.db == nil {
		return UserListResult{}, errors.New("admin store is not initialized")
	}
	page := query.Page
	if page < 1 {
		page = 1
	}
	pageSize := query.PageSize
	if pageSize < 1 {
		pageSize = 20
	}
	if pageSize > 200 {
		pageSize = 200
	}
	orderBy := adminUserOrderColumn(query.OrderBy)
	orderDirection := "DESC"
	if !query.OrderDesc {
		orderDirection = "ASC"
	}

	where, args := buildUserListWhere(query)
	countSQL := "SELECT COUNT(*) FROM admin_users" + where
	var total int
	if err := c.db.QueryRowContext(ctx, countSQL, args...).Scan(&total); err != nil {
		return UserListResult{}, fmt.Errorf("count admin users failed: %w", err)
	}

	offset := (page - 1) * pageSize
	listArgs := append([]interface{}{}, args...)
	limitIndex := len(listArgs) + 1
	offsetIndex := len(listArgs) + 2
	listArgs = append(listArgs, pageSize, offset)
	listSQL := fmt.Sprintf(`
		SELECT
			id,
			username,
			email,
			full_name,
			phone,
			department,
			avatar,
			preferred_home_path,
			is_active,
			last_login,
			last_login_ip,
			COALESCE(login_count, 0),
			created_at,
			updated_at
		FROM admin_users
		%s
		ORDER BY %s %s
		LIMIT $%d OFFSET $%d
	`, where, orderBy, orderDirection, limitIndex, offsetIndex)

	rows, err := c.db.QueryContext(ctx, listSQL, listArgs...)
	if err != nil {
		return UserListResult{}, fmt.Errorf("query admin users failed: %w", err)
	}
	defer rows.Close()

	items := []UserItem{}
	for rows.Next() {
		item, err := scanUserItem(rows)
		if err != nil {
			return UserListResult{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return UserListResult{}, fmt.Errorf("iterate admin users failed: %w", err)
	}
	return UserListResult{Items: items, Total: total}, nil
}

func (c *Client) GetActiveUserBySubject(ctx context.Context, subject string) (UserItem, error) {
	if c == nil || c.db == nil {
		return UserItem{}, errors.New("admin store is not initialized")
	}
	subject = strings.TrimSpace(subject)
	if subject == "" {
		return UserItem{}, authz.ErrUnauthorized
	}
	row := c.db.QueryRowContext(ctx, `
		SELECT
			id,
			username,
			email,
			full_name,
			phone,
			department,
			avatar,
			preferred_home_path,
			is_active,
			last_login,
			last_login_ip,
			COALESCE(login_count, 0),
			created_at,
			updated_at
		FROM admin_users
		WHERE (email = $1 OR username = $1) AND is_active = TRUE
		ORDER BY id ASC
		LIMIT 1
	`, subject)
	item, err := scanUserItem(row)
	if errors.Is(err, sql.ErrNoRows) {
		return UserItem{}, authz.ErrUnauthorized
	}
	if err != nil {
		return UserItem{}, err
	}
	return item, nil
}

func (c *Client) GetProfileStatsBySubject(ctx context.Context, subject string) (ProfileStats, error) {
	if c == nil || c.db == nil {
		return ProfileStats{}, errors.New("admin store is not initialized")
	}
	subject = strings.TrimSpace(subject)
	if subject == "" {
		return ProfileStats{}, authz.ErrUnauthorized
	}
	row := c.db.QueryRowContext(ctx, `
		SELECT
			COALESCE(u.login_count, 0),
			u.last_login,
			u.last_login_ip,
			u.created_at,
			(
				SELECT COUNT(*)
				FROM admin_logs l
				WHERE l.user_id = u.id
				  AND l.action = 'login'
				  AND l.created_at >= NOW() - INTERVAL '30 days'
			),
			(
				SELECT COUNT(*)
				FROM admin_logs l
				WHERE l.user_id = u.id
			)
		FROM admin_users u
		WHERE (u.email = $1 OR u.username = $1) AND u.is_active = TRUE
		ORDER BY u.id ASC
		LIMIT 1
	`, subject)

	var stats ProfileStats
	var lastLogin sql.NullTime
	var lastLoginIP sql.NullString
	if err := row.Scan(
		&stats.TotalLogins,
		&lastLogin,
		&lastLoginIP,
		&stats.AccountCreated,
		&stats.RecentLogins30D,
		&stats.TotalOperations,
	); errors.Is(err, sql.ErrNoRows) {
		return ProfileStats{}, authz.ErrUnauthorized
	} else if err != nil {
		return ProfileStats{}, fmt.Errorf("query profile stats failed: %w", err)
	}
	if lastLogin.Valid {
		value := lastLogin.Time
		stats.LastLogin = &value
	}
	stats.LastLoginIP = stringPtrFromNull(lastLoginIP)
	return stats, nil
}

func (c *Client) CheckPermission(ctx context.Context, subject string, permission string, scope map[string]string) (authz.CheckResult, error) {
	if c == nil || c.db == nil {
		return authz.CheckResult{}, errors.New("admin store is not initialized")
	}
	subject = strings.TrimSpace(subject)
	permission = strings.TrimSpace(permission)
	if subject == "" {
		return authz.CheckResult{}, authz.ErrUnauthorized
	}
	if permission == "" {
		return authz.CheckResult{Allowed: true, Reason: "permission_not_required"}, nil
	}

	user, err := c.findActiveUser(ctx, subject)
	if err != nil {
		return authz.CheckResult{}, err
	}
	if !c.rbacEnabled {
		return authz.CheckResult{
			Allowed: true,
			Reason:  "rbac_disabled",
			UserID:  user.ID,
			User:    user.Username,
			Email:   user.Email,
			Scope:   normalizeScope(scope),
		}, nil
	}

	bindings, err := c.userPermissionBindings(ctx, user.ID)
	if err != nil {
		return authz.CheckResult{}, err
	}
	result := evaluatePermissionBindings(bindings, permission, scope, c.failOpenWhenUnbound)
	result.UserID = user.ID
	result.User = user.Username
	result.Email = user.Email
	result.Scope = normalizeScope(scope)
	return result, nil
}

func stringPtrFromNull(value sql.NullString) *string {
	if !value.Valid {
		return nil
	}
	normalized := value.String
	return &normalized
}

type userRowScanner interface {
	Scan(dest ...interface{}) error
}

func scanUserItem(scanner userRowScanner) (UserItem, error) {
	var item UserItem
	var fullName sql.NullString
	var phone sql.NullString
	var department sql.NullString
	var avatar sql.NullString
	var preferredHomePath sql.NullString
	var lastLogin sql.NullTime
	var lastLoginIP sql.NullString
	var updatedAt sql.NullTime
	if err := scanner.Scan(
		&item.ID,
		&item.Username,
		&item.Email,
		&fullName,
		&phone,
		&department,
		&avatar,
		&preferredHomePath,
		&item.IsActive,
		&lastLogin,
		&lastLoginIP,
		&item.LoginCount,
		&item.CreatedAt,
		&updatedAt,
	); err != nil {
		return UserItem{}, fmt.Errorf("scan admin user failed: %w", err)
	}
	item.FullName = stringPtrFromNull(fullName)
	item.Phone = stringPtrFromNull(phone)
	item.Department = stringPtrFromNull(department)
	item.Avatar = stringPtrFromNull(avatar)
	item.PreferredHomePath = stringPtrFromNull(preferredHomePath)
	if lastLogin.Valid {
		value := lastLogin.Time
		item.LastLogin = &value
	}
	item.LastLoginIP = stringPtrFromNull(lastLoginIP)
	if updatedAt.Valid {
		value := updatedAt.Time
		item.UpdatedAt = &value
	}
	return item, nil
}

func buildUserListWhere(query UserListQuery) (string, []interface{}) {
	clauses := []string{}
	args := []interface{}{}
	if search := strings.TrimSpace(query.Search); search != "" {
		args = append(args, "%"+search+"%")
		idx := len(args)
		clauses = append(clauses, fmt.Sprintf("(username ILIKE $%d OR email ILIKE $%d OR full_name ILIKE $%d)", idx, idx, idx))
	}
	if query.IsActive != nil {
		args = append(args, *query.IsActive)
		clauses = append(clauses, fmt.Sprintf("is_active = $%d", len(args)))
	}
	if department := strings.TrimSpace(query.Department); department != "" {
		args = append(args, department)
		clauses = append(clauses, fmt.Sprintf("department = $%d", len(args)))
	}
	if len(clauses) == 0 {
		return "", args
	}
	return " WHERE " + strings.Join(clauses, " AND "), args
}

func adminUserOrderColumn(orderBy string) string {
	switch strings.TrimSpace(orderBy) {
	case "id":
		return "id"
	case "username":
		return "username"
	case "email":
		return "email"
	case "full_name":
		return "full_name"
	case "phone":
		return "phone"
	case "department":
		return "department"
	case "is_active":
		return "is_active"
	case "last_login":
		return "last_login"
	case "last_login_ip":
		return "last_login_ip"
	case "login_count":
		return "login_count"
	case "updated_at":
		return "updated_at"
	case "created_at":
		return "created_at"
	default:
		return "created_at"
	}
}

type adminUser struct {
	ID       int
	Username string
	Email    string
}

func (c *Client) findActiveUser(ctx context.Context, subject string) (adminUser, error) {
	var user adminUser
	var active bool
	err := c.db.QueryRowContext(ctx, `
		SELECT id, username, email, is_active
		FROM admin_users
		WHERE email = $1 OR username = $1
		ORDER BY id ASC
		LIMIT 1
	`, subject).Scan(&user.ID, &user.Username, &user.Email, &active)
	if errors.Is(err, sql.ErrNoRows) {
		return adminUser{}, authz.ErrUnauthorized
	}
	if err != nil {
		return adminUser{}, fmt.Errorf("query admin user failed: %w", err)
	}
	if !active {
		return adminUser{}, authz.ErrUnauthorized
	}
	return user, nil
}

func (c *Client) userPermissionBindings(ctx context.Context, userID int) ([]permissionBinding, error) {
	rows, err := c.db.QueryContext(ctx, `
		SELECT
			p.code,
			r.name,
			COALESCE(b.scope_type, 'global'),
			COALESCE(b.tenant_id, ''),
			COALESCE(b.project_id, ''),
			COALESCE(b.kb_id, '')
		FROM admin_permissions p
		JOIN admin_role_permissions rp ON rp.permission_id = p.id
		JOIN admin_roles r ON r.id = rp.role_id
		JOIN admin_user_role_bindings b ON b.role_id = r.id
		WHERE b.user_id = $1
		  AND (b.expires_at IS NULL OR b.expires_at > NOW())
	`, userID)
	if err != nil {
		return nil, fmt.Errorf("query user permission bindings failed: %w", err)
	}
	defer rows.Close()

	bindings := []permissionBinding{}
	for rows.Next() {
		var binding permissionBinding
		if err := rows.Scan(
			&binding.PermissionCode,
			&binding.RoleName,
			&binding.ScopeType,
			&binding.TenantID,
			&binding.ProjectID,
			&binding.KBID,
		); err != nil {
			return nil, fmt.Errorf("scan user permission binding failed: %w", err)
		}
		bindings = append(bindings, binding)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate user permission bindings failed: %w", err)
	}
	return bindings, nil
}

func evaluatePermissionBindings(bindings []permissionBinding, permission string, scope map[string]string, failOpenWhenUnbound bool) authz.CheckResult {
	if len(bindings) == 0 {
		if failOpenWhenUnbound {
			return authz.CheckResult{Allowed: true, Reason: "legacy_allow_no_binding"}
		}
		return authz.CheckResult{Allowed: false, Reason: "no_binding"}
	}

	permissionBindings := make([]permissionBinding, 0, len(bindings))
	for _, binding := range bindings {
		if binding.PermissionCode == permission {
			permissionBindings = append(permissionBindings, binding)
		}
	}
	if len(permissionBindings) == 0 {
		return authz.CheckResult{Allowed: false, Reason: "permission_missing"}
	}
	if !hasAnyScope(scope) {
		return authz.CheckResult{Allowed: true, Reason: "allowed_without_scope"}
	}
	for _, binding := range permissionBindings {
		if scopeMatches(binding, scope) {
			return authz.CheckResult{Allowed: true, Reason: "allowed"}
		}
	}
	return authz.CheckResult{Allowed: false, Reason: "scope_mismatch"}
}

func scopeMatches(binding permissionBinding, scope map[string]string) bool {
	switch strings.ToLower(strings.TrimSpace(binding.ScopeType)) {
	case "", "global":
		return true
	case "tenant":
		return scopeValue(scope, "tenant_id", "x-tenant-id") != "" && binding.TenantID == scopeValue(scope, "tenant_id", "x-tenant-id")
	case "project":
		return scopeValue(scope, "project_id", "x-project-id") != "" && binding.ProjectID == scopeValue(scope, "project_id", "x-project-id")
	case "kb":
		return scopeValue(scope, "kb_id", "x-kb-id") != "" && binding.KBID == scopeValue(scope, "kb_id", "x-kb-id")
	default:
		return false
	}
}

func hasAnyScope(scope map[string]string) bool {
	for _, key := range []string{"tenant_id", "project_id", "kb_id", "x-tenant-id", "x-project-id", "x-kb-id"} {
		if strings.TrimSpace(scope[key]) != "" {
			return true
		}
	}
	return false
}

func scopeValue(scope map[string]string, keys ...string) string {
	for _, key := range keys {
		value := strings.TrimSpace(scope[key])
		if value != "" {
			return value
		}
	}
	return ""
}

func normalizeScope(scope map[string]string) map[string]string {
	return map[string]string{
		"tenant_id":  scopeValue(scope, "tenant_id", "x-tenant-id"),
		"project_id": scopeValue(scope, "project_id", "x-project-id"),
		"kb_id":      scopeValue(scope, "kb_id", "x-kb-id"),
	}
}

// kbParentScope 是 tenant/project 作用域绑定解析出的父作用域（M4-R1 FIX #1）。
type kbParentScope struct {
	TenantID  string
	ProjectID string
}

// AuthorizedKBIDs 解析 subject（username/email）在 admin_user_role_bindings 中的
// 授权 KB 集合（契约 §2.4 user_authorized_kb_ids 的服务端解析，M4 问答/检索隔离使用）。
//
// 语义：
//   - kb 作用域绑定（scope_type='kb'）→ 显式 kb_ids（格式非法的绑定按拒绝处理，跳过）；
//   - 持有授予 permissions 中任一权限码的 GLOBAL 作用域绑定 → allKBs=true（全量哨兵）；
//   - tenant/project 作用域绑定 → 通过 knowledge_bases 目录表反查该父作用域下的真实
//     KB 集合（M4-R1 FIX #1：禁止把 tenant/project 绑定放大为全部 KB；目录中不存在
//     的 KB 不进入授权集合，交集为空由调用方按 KB_ACCESS_DENIED 处理）；
//   - 用户无任何绑定：failOpenWhenUnbound 开启时放行为 allKBs（与 CheckPermission 的
//     legacy_allow_no_binding 语义一致），否则返回空集合（交集为空 → 拒绝）。
//   - RBAC 未启用 → allKBs=true（与 CheckPermission 的 rbac_disabled 放行语义一致）。
//
// 返回的 kbIDs 已按 scope 契约归一化（trim + 小写）。
func (c *Client) AuthorizedKBIDs(ctx context.Context, subject string, permissions ...string) ([]string, bool, error) {
	if c == nil || c.db == nil {
		return nil, false, errors.New("admin store is not initialized")
	}
	subject = strings.TrimSpace(subject)
	if subject == "" {
		return nil, false, authz.ErrUnauthorized
	}
	user, err := c.findActiveUser(ctx, subject)
	if err != nil {
		return nil, false, err
	}
	if !c.rbacEnabled {
		return nil, true, nil
	}
	bindings, err := c.userPermissionBindings(ctx, user.ID)
	if err != nil {
		return nil, false, err
	}
	kbIDs, allKBs, parentScopes := authorizedKBsFromBindings(bindings, permissionCodeSet(permissions), c.failOpenWhenUnbound)
	if allKBs || len(parentScopes) == 0 {
		return kbIDs, allKBs, nil
	}
	resolved, err := c.knowledgeBaseIDsForParentScopes(ctx, parentScopes)
	if err != nil {
		return nil, false, err
	}
	merged := append(kbIDs, resolved...)
	return normalizedKBIDSet(merged), false, nil
}

// authorizedKBsFromBindings 是 AuthorizedKBIDs 的纯函数内核（可单测）：
// 按作用域类型归并绑定，返回 (显式 kb ids, allKBs 哨兵, tenant/project 父作用域清单)。
// 只有 global 绑定可以使用全量哨兵；tenant/project 绑定交由调用方反查 KB 目录。
func authorizedKBsFromBindings(bindings []permissionBinding, permissions map[string]bool, failOpenWhenUnbound bool) ([]string, bool, []kbParentScope) {
	parentScopes := []kbParentScope{}
	seenParent := map[kbParentScope]bool{}
	if len(bindings) == 0 {
		if failOpenWhenUnbound {
			return nil, true, nil
		}
		return nil, false, nil
	}
	allKBs := false
	kbSet := make(map[string]struct{})
	for _, binding := range bindings {
		if len(permissions) > 0 && !permissions[binding.PermissionCode] {
			continue
		}
		switch strings.ToLower(strings.TrimSpace(binding.ScopeType)) {
		case "", "global":
			// global：明确的全量授权，仅保留给全局绑定（M4-R1 FIX #1）。
			allKBs = true
		case "tenant", "project":
			// tenant/project：不放大为全部 KB，记录父作用域，由调用方通过
			// knowledge_bases 目录反查真实授权 KB 集合。
			tenantID, scopeErr := normalizeBindingScopeID("tenant_id", binding.TenantID)
			if scopeErr != nil {
				continue
			}
			projectID, scopeErr := normalizeBindingScopeID("project_id", binding.ProjectID)
			if scopeErr != nil {
				continue
			}
			if tenantID == "" && projectID == "" {
				continue
			}
			parent := kbParentScope{TenantID: tenantID, ProjectID: projectID}
			if !seenParent[parent] {
				seenParent[parent] = true
				parentScopes = append(parentScopes, parent)
			}
		case "kb":
			normalized, err := normalizeBindingKBID(binding.KBID)
			if err != nil || normalized == "" {
				// 非法 kb_id 绑定按“未授权该 KB”处理（fail-closed），不让脏数据放大授权。
				continue
			}
			kbSet[normalized] = struct{}{}
		default:
			continue
		}
	}
	if allKBs {
		return nil, true, nil
	}
	kbIDs := sortedKBIDSet(kbSet)
	return kbIDs, false, parentScopes
}

// knowledgeBaseIDsForParentScopes 通过 knowledge_bases 目录表反查父作用域下的 KB id。
// 目录查询失败（表不存在等）按错误返回，调用方 fail-closed。
func (c *Client) knowledgeBaseIDsForParentScopes(ctx context.Context, parentScopes []kbParentScope) ([]string, error) {
	if len(parentScopes) == 0 {
		return nil, nil
	}
	clauses := make([]string, 0, len(parentScopes))
	args := make([]interface{}, 0, len(parentScopes)*2)
	for _, parent := range parentScopes {
		if parent.TenantID != "" && parent.ProjectID != "" {
			args = append(args, parent.TenantID, parent.ProjectID)
			clauses = append(clauses, fmt.Sprintf("(tenant_id = $%d AND project_id = $%d)", len(args)-1, len(args)))
			continue
		}
		if parent.TenantID != "" {
			args = append(args, parent.TenantID)
			clauses = append(clauses, fmt.Sprintf("tenant_id = $%d", len(args)))
			continue
		}
		args = append(args, parent.ProjectID)
		clauses = append(clauses, fmt.Sprintf("project_id = $%d", len(args)))
	}
	query := fmt.Sprintf(`
		SELECT id
		FROM knowledge_bases
		WHERE %s
	`, strings.Join(clauses, " OR "))
	rows, err := c.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("query knowledge bases for parent scopes failed: %w", err)
	}
	defer rows.Close()

	kbSet := make(map[string]struct{})
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, fmt.Errorf("scan knowledge base id failed: %w", err)
		}
		normalized, scopeErr := normalizeBindingKBID(raw)
		if scopeErr != nil || normalized == "" {
			// 目录中的脏 id 不进入授权集合（fail-closed）。
			continue
		}
		kbSet[normalized] = struct{}{}
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate knowledge base ids failed: %w", err)
	}
	return sortedKBIDSet(kbSet), nil
}

func sortedKBIDSet(set map[string]struct{}) []string {
	if len(set) == 0 {
		return nil
	}
	kbIDs := make([]string, 0, len(set))
	for id := range set {
		kbIDs = append(kbIDs, id)
	}
	sort.Strings(kbIDs)
	return kbIDs
}

// normalizedKBIDSet 合并两组 kb id（显式绑定 + 目录反查），归一化去重排序。
func normalizedKBIDSet(values []string) []string {
	set := make(map[string]struct{}, len(values))
	for _, value := range values {
		normalized, err := normalizeBindingKBID(value)
		if err != nil || normalized == "" {
			continue
		}
		set[normalized] = struct{}{}
	}
	return sortedKBIDSet(set)
}

func permissionCodeSet(codes []string) map[string]bool {
	if len(codes) == 0 {
		return nil
	}
	set := make(map[string]bool, len(codes))
	for _, code := range codes {
		trimmed := strings.TrimSpace(code)
		if trimmed != "" {
			set[trimmed] = true
		}
	}
	if len(set) == 0 {
		return nil
	}
	return set
}

// normalizeBindingKBID 对绑定中的 kb_id 应用 scope 契约的归一化规则。
func normalizeBindingKBID(value string) (string, error) {
	return normalizeBindingScopeID("kb_id", value)
}

func normalizeBindingScopeID(kind, value string) (string, error) {
	normalized, scopeErr := scope.NormalizeScopeID(kind, value)
	if scopeErr != nil {
		return "", scopeErr
	}
	return normalized, nil
}
