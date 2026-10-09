import React, { useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Box,
  CssBaseline,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Stack,
  Toolbar,
  Typography,
  useMediaQuery,
  Button,
} from '@mui/material';
import {
  Analytics,
  Dashboard,
  Description,
  Folder,
  FactCheck,
  Hub,
  ManageSearch,
  MonitorHeart,
  Person,
  Settings,
  Shield,
  WorkHistory,
  Menu as MenuIcon,
  Logout,
} from '@mui/icons-material';
import { ThemeProvider } from '@mui/material/styles';
import { useLocation, useNavigate } from 'react-router-dom';
import { logoutAdminSession } from '../../services/adminSession';
import { adminTheme } from '../../theme/adminTheme';
import '../../styles/adminTheme.css';

type AdminLayoutProps = {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
};

const drawerWidth = 260;

type AdminFrameControls = {
  isMobile: boolean;
  openMobileNav: () => void;
};

type AdminNavItem = {
  label: string;
  icon: ReactNode;
  path: string;
};

type AdminNavGroup = {
  label: string;
  items: AdminNavItem[];
};

const AdminShellControlsContext = React.createContext<AdminFrameControls | null>(null);

const AdminPageChrome: React.FC<AdminLayoutProps & { controls: AdminFrameControls }> = ({
  title,
  subtitle,
  actions,
  children,
  controls,
}) => (
  <Box sx={{ flexGrow: 1, minWidth: 0, px: { xs: 2, md: 5 }, pt: 4, pb: { xs: 8, md: 10 } }}>
    <Stack direction="row" alignItems="center" spacing={2} sx={{ mb: 4 }}>
      {controls.isMobile && (
        <IconButton onClick={controls.openMobileNav}>
          <MenuIcon />
        </IconButton>
      )}
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography variant="h4">{title}</Typography>
        {subtitle && (
          <Typography variant="body2" color="text.secondary">
            {subtitle}
          </Typography>
        )}
      </Box>
      {actions}
    </Stack>
    {children}
  </Box>
);

const AdminFrame: React.FC<{ children: (controls: AdminFrameControls) => ReactNode }> = ({ children }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const isMobile = useMediaQuery(adminTheme.breakpoints.down('md'));
  const [mobileOpen, setMobileOpen] = useState(false);
  const openMobileNav = useCallback(() => setMobileOpen(true), []);
  const controls = useMemo(() => ({ isMobile, openMobileNav }), [isMobile, openMobileNav]);

  const navGroups = useMemo<AdminNavGroup[]>(
    () => [
      {
        label: '总览',
        items: [
          { label: '仪表盘', icon: <Dashboard />, path: '/admin/dashboard' },
          { label: '图谱工作台', icon: <Hub />, path: '/workspace' },
        ],
      },
      {
        label: '知识库运营',
        items: [
          { label: '知识库目录', icon: <Folder />, path: '/admin/knowledge-bases' },
          { label: '任务中心', icon: <WorkHistory />, path: '/admin/jobs' },
        ],
      },
      {
        label: '问答与模型',
        items: [
          { label: '问答追踪', icon: <ManageSearch />, path: '/admin/qa-traces' },
          { label: '配置中心', icon: <Settings />, path: '/admin/config' },
        ],
      },
      {
        label: '观测与审计',
        items: [
          { label: '系统监控', icon: <MonitorHeart />, path: '/admin/monitor' },
          { label: '日志审计', icon: <FactCheck />, path: '/admin/logs' },
          { label: '数据分析', icon: <Analytics />, path: '/admin/analytics' },
        ],
      },
      {
        label: '组织安全',
        items: [
          { label: '权限管理', icon: <Shield />, path: '/admin/rbac' },
          { label: '用户管理', icon: <Person />, path: '/admin/users' },
        ],
      },
    ],
    []
  );

  const handleNavigate = (path: string) => {
    if (location.pathname !== path) {
      navigate(path);
    }
    if (isMobile) {
      setMobileOpen(false);
    }
  };

  const handleLogout = async () => {
    await logoutAdminSession();
    navigate('/admin/login', { replace: true });
    if (isMobile) {
      setMobileOpen(false);
    }
  };

  const drawer = (
    <Stack sx={{ height: '100%', px: 2, py: 3 }}>
      <Stack spacing={0.5} sx={{ mb: 3 }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>
          GraphInsight
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Control Plane
        </Typography>
      </Stack>

      <List sx={{ flex: 1, py: 0 }}>
        {navGroups.map((group) => (
          <Box key={group.label} sx={{ mb: 2 }}>
            <Typography
              variant="caption"
              sx={{
                display: 'block',
                px: 1.5,
                pb: 0.75,
                fontWeight: 700,
                color: 'text.secondary',
              }}
            >
              {group.label}
            </Typography>
            {group.items.map((item) => {
              const selected =
                location.pathname === item.path || location.pathname.startsWith(`${item.path}/`);
              return (
                <ListItemButton
                  key={item.path}
                  selected={selected}
                  onClick={() => handleNavigate(item.path)}
                  sx={{
                    borderRadius: 2,
                    mb: 0.5,
                    '&.Mui-selected': {
                      backgroundColor: 'rgba(27, 127, 121, 0.12)',
                    },
                    '&.Mui-selected:hover': {
                      backgroundColor: 'rgba(27, 127, 121, 0.18)',
                    },
                  }}
                >
                  <ListItemIcon sx={{ minWidth: 40, color: selected ? 'primary.main' : 'text.secondary' }}>
                    {item.icon}
                  </ListItemIcon>
                  <ListItemText primary={item.label} />
                </ListItemButton>
              );
            })}
          </Box>
        ))}
      </List>

      <Stack spacing={1} sx={{ mt: 2, pt: 2, borderTop: '1px solid rgba(15, 31, 45, 0.08)' }}>
        <ListItemButton
          onClick={() => handleNavigate('/admin/profile')}
          sx={{ borderRadius: 2, backgroundColor: 'rgba(15, 31, 45, 0.04)' }}
        >
          <ListItemIcon sx={{ minWidth: 40 }}>
            <Person />
          </ListItemIcon>
          <ListItemText primary="个人设置" secondary="账号与安全" />
        </ListItemButton>
        <ListItemButton
          onClick={() => handleNavigate('/workspace')}
          sx={{ borderRadius: 2, backgroundColor: 'rgba(15, 31, 45, 0.04)' }}
        >
          <ListItemIcon sx={{ minWidth: 40 }}>
            <Description />
          </ListItemIcon>
          <ListItemText primary="打开工作台" secondary="图谱主界面" />
        </ListItemButton>
        <Button
          variant="outlined"
          color="error"
          startIcon={<Logout />}
          onClick={handleLogout}
          sx={{
            justifyContent: 'flex-start',
            borderRadius: 2,
            mt: 1,
            py: 1.2,
          }}
        >
          退出登录
        </Button>
      </Stack>
    </Stack>
  );

  return (
    <Box className="admin-root" sx={{ minHeight: '100vh', position: 'relative' }}>
      <Box sx={{ display: 'flex', position: 'relative', zIndex: 1 }}>
        <Drawer
          variant={isMobile ? 'temporary' : 'permanent'}
          open={isMobile ? mobileOpen : true}
          onClose={() => setMobileOpen(false)}
          sx={{
            width: drawerWidth,
            flexShrink: 0,
            '& .MuiDrawer-paper': {
              width: drawerWidth,
              boxSizing: 'border-box',
              backgroundColor: 'rgba(255, 255, 255, 0.92)',
              backdropFilter: 'blur(10px)',
              borderRight: '1px solid rgba(15, 31, 45, 0.08)',
            },
          }}
        >
          <Toolbar sx={{ minHeight: 24 }} />
          {drawer}
        </Drawer>

        {children(controls)}
      </Box>
    </Box>
  );
};

export const AdminShell: React.FC<{ children: ReactNode }> = ({ children }) => (
  <ThemeProvider theme={adminTheme}>
    <CssBaseline />
    <AdminFrame>
      {(controls) => (
        <AdminShellControlsContext.Provider value={controls}>
          {children}
        </AdminShellControlsContext.Provider>
      )}
    </AdminFrame>
  </ThemeProvider>
);

const AdminLayout: React.FC<AdminLayoutProps> = ({ title, subtitle, actions, children }) => {
  const shellControls = useContext(AdminShellControlsContext);

  if (shellControls) {
    return (
      <AdminPageChrome title={title} subtitle={subtitle} actions={actions} controls={shellControls}>
        {children}
      </AdminPageChrome>
    );
  }

  return (
    <ThemeProvider theme={adminTheme}>
      <CssBaseline />
      <AdminFrame>
        {(controls) => (
          <AdminPageChrome title={title} subtitle={subtitle} actions={actions} controls={controls}>
            {children}
          </AdminPageChrome>
        )}
      </AdminFrame>
    </ThemeProvider>
  );
};

export default AdminLayout;
