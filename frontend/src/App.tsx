import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Routes, Route, Navigate, Outlet } from 'react-router-dom';
import type { LayoutConfig, RendererAPI } from './renderers/core/types';
import { useAdminSession } from './hooks/useAdminSession';
import { getPreferredAdminHome } from './utils/adminAuth';
import { AppLayout } from './components/Layout/AppLayout';
import { AdminShell } from './components/Admin/AdminLayout';
import { LoadingState } from './components/Loading/AppleSpinner';

const DocChatPanel = lazy(() => import('./components/ChatPanel/DocChatPanel').then((mod) => ({ default: mod.DocChatPanel })));
const MainWorkspace = lazy(() => import('./components/Workspace/MainWorkspace').then((mod) => ({ default: mod.MainWorkspace })));
const RightPanel = lazy(() => import('./components/Layout/RightPanel').then((mod) => ({ default: mod.RightPanel })));
const ExportDialog = lazy(() => import('./components/ExportDialog/ExportDialog').then((mod) => ({ default: mod.ExportDialog })));

const LoginPage = lazy(() => import('./pages/Admin/LoginPage'));
const RegisterPage = lazy(() => import('./pages/Admin/RegisterPage'));
const TestPage = lazy(() => import('./pages/Admin/TestPage'));
const DashboardPage = lazy(() => import('./pages/Admin/DashboardPage'));
const ConfigPage = lazy(() => import('./pages/Admin/ConfigPage'));
const MonitorPage = lazy(() => import('./pages/Admin/MonitorPage'));
const LogsPage = lazy(() => import('./pages/Admin/LogsPage'));
const AnalyticsPage = lazy(() => import('./pages/Admin/AnalyticsPage'));
const ProfilePage = lazy(() => import('./pages/Admin/ProfilePage'));
const RbacPage = lazy(() => import('./pages/Admin/RbacPage'));
const UsersPage = lazy(() => import('./pages/Admin/UsersPage'));
const JobsPage = lazy(() => import('./pages/Admin/JobsPage'));
const QATracesPage = lazy(() => import('./pages/Admin/QATracesPage'));
const KnowledgeBasePage = lazy(() => import('./pages/Admin/KnowledgeBasePage'));
const KnowledgeBasesPage = lazy(() => import('./pages/Admin/KnowledgeBasesPage'));
const KnowledgeBaseDetailPage = lazy(() => import('./pages/Admin/KnowledgeBaseDetailPage'));

const PanelLoading = ({ label }: { label: string }) => (
  <LoadingState label={label} minHeight="100%" size={38} sx={{ bgcolor: '#f8fafc' }} />
);

const RouteLoading = () => {
  const [isSlow, setIsSlow] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setIsSlow(true), 8000);
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        background: 'linear-gradient(135deg, #f8fafc 0%, #eef2ff 100%)',
        color: '#334155',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      }}
    >
      <div style={{ textAlign: 'center', display: 'grid', gap: 16 }}>
        <LoadingState label="GraphInsight 正在加载" size={52} minHeight={120} sx={{ p: 0 }} />
        {isSlow && (
          <div style={{ display: 'grid', gap: 14 }}>
            <div style={{ color: '#64748b', fontSize: 14 }}>
              加载时间过长，可能是浏览器缓存或网络请求没有完成。
            </div>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
              <button
                type="button"
                onClick={() => window.location.reload()}
                style={{
                  border: '1px solid #2563eb',
                  background: '#2563eb',
                  color: '#fff',
                  borderRadius: 6,
                  padding: '8px 14px',
                  cursor: 'pointer',
                }}
              >
                刷新
              </button>
              <button
                type="button"
                onClick={() => {
                  window.localStorage.removeItem('admin_token');
                  window.location.assign('/admin/login');
                }}
                style={{
                  border: '1px solid #cbd5e1',
                  background: '#fff',
                  color: '#334155',
                  borderRadius: 6,
                  padding: '8px 14px',
                  cursor: 'pointer',
                }}
              >
                回到登录
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

function MainApp() {
  const [exportDialogOpen, setExportDialogOpen] = useState(false);
  const rendererRef = useRef<RendererAPI | null>(null);

  const handleLayoutChange = (layout: string, config?: LayoutConfig) => {
    rendererRef.current?.applyLayout(layout, config);
    setTimeout(() => {
      rendererRef.current?.fitTo(undefined, 50);
    }, 120);
  };

  const handleGroupingChange = () => {
    if (rendererRef.current) {
      setTimeout(() => {
        rendererRef.current?.fitTo(undefined, 50);
      }, 100);
    }
  };

  return (
    <>
      <AppLayout
        queryPanel={
          <Suspense fallback={<PanelLoading label="正在加载问答面板" />}>
            <DocChatPanel />
          </Suspense>
        }
        graphCanvas={
          <Suspense fallback={<PanelLoading label="正在加载工作台" />}>
            <MainWorkspace rendererRef={rendererRef} onGroupingUpdate={handleGroupingChange} />
          </Suspense>
        }
        detailPanel={
          <Suspense fallback={<PanelLoading label="正在加载详情面板" />}>
            <RightPanel
              rendererRef={rendererRef}
              onLayoutChange={handleLayoutChange}
              onGroupingChange={handleGroupingChange}
            />
          </Suspense>
        }
        onExportClick={() => setExportDialogOpen(true)}
      />
      {exportDialogOpen && (
        <Suspense fallback={null}>
          <ExportDialog
            open={exportDialogOpen}
            onClose={() => setExportDialogOpen(false)}
            rendererRef={rendererRef}
          />
        </Suspense>
      )}
    </>
  );
}

function SessionLoading() {
  return <RouteLoading />;
}

function PageLoading() {
  return <LoadingState label="正在加载页面" minHeight="calc(100vh - 96px)" />;
}

function LazyPage({ children }: { children: ReactNode }) {
  return <Suspense fallback={<PageLoading />}>{children}</Suspense>;
}

function AdminShellRoute({ session }: { session: AdminSessionSnapshot }) {
  return (
    <RequireAdminAuth session={session}>
      <AdminShell>
        <LazyPage>
          <Outlet />
        </LazyPage>
      </AdminShell>
    </RequireAdminAuth>
  );
}

type AdminSessionSnapshot = {
  isChecking: boolean;
  isAuthenticated: boolean;
};

function RequireAdminAuth({ children, session }: { children: ReactNode; session: AdminSessionSnapshot }) {
  const { isChecking, isAuthenticated } = session;
  if (isChecking) {
    return <SessionLoading />;
  }
  return isAuthenticated ? <>{children}</> : <Navigate to="/admin/login" replace />;
}

function PublicAuthOnly({ children, session }: { children: ReactNode; session: AdminSessionSnapshot }) {
  const { isChecking, isAuthenticated } = session;
  if (isChecking) {
    return <SessionLoading />;
  }
  return isAuthenticated ? <Navigate to={getPreferredAdminHome()} replace /> : <>{children}</>;
}

function HomeRedirect({ session }: { session: AdminSessionSnapshot }) {
  const { isChecking, isAuthenticated } = session;
  if (isChecking) {
    return <SessionLoading />;
  }
  return <Navigate to={isAuthenticated ? getPreferredAdminHome() : '/admin/login'} replace />;
}

function App() {
  const { isChecking, isAuthenticated } = useAdminSession();
  const session = { isChecking, isAuthenticated };

  return (
    <Routes>
      <Route path="/" element={<HomeRedirect session={session} />} />
      <Route path="/login" element={<Navigate to="/admin/login" replace />} />
      <Route path="/register" element={<Navigate to="/admin/register" replace />} />
      <Route
        path="/admin/login"
        element={
          <PublicAuthOnly session={session}>
            <LazyPage>
              <LoginPage />
            </LazyPage>
          </PublicAuthOnly>
        }
      />
      <Route
        path="/admin/register"
        element={
          <PublicAuthOnly session={session}>
            <LazyPage>
              <RegisterPage />
            </LazyPage>
          </PublicAuthOnly>
        }
      />
      <Route
        path="/workspace"
        element={
          <RequireAdminAuth session={session}>
            <MainApp />
          </RequireAdminAuth>
        }
      />
      <Route path="/admin" element={<AdminShellRoute session={session} />}>
        <Route index element={<HomeRedirect session={session} />} />
        <Route path="test" element={<TestPage />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="config" element={<ConfigPage />} />
        <Route path="monitor" element={<MonitorPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="analytics" element={<AnalyticsPage />} />
        <Route path="profile" element={<ProfilePage />} />
        <Route path="rbac" element={<RbacPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="knowledge-base" element={<KnowledgeBasePage />} />
        <Route path="knowledge-bases" element={<KnowledgeBasesPage />} />
        <Route path="knowledge-bases/:kbId" element={<KnowledgeBaseDetailPage />} />
        <Route path="jobs" element={<JobsPage />} />
        <Route path="qa-traces" element={<QATracesPage />} />
      </Route>
      <Route path="*" element={<HomeRedirect session={session} />} />
    </Routes>
  );
}

export default App;
