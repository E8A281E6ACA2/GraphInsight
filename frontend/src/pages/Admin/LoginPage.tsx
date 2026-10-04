/**
 * 管理系统登录页
 */
import React, { useState } from 'react';
import {
  Card,
  CardContent,
  TextField,
  Typography,
  Alert,
  Link,
  Stack,
} from '@mui/material';
import { useNavigate } from 'react-router-dom';
import { authApi } from '../../services/adminService';
import AdminAuthLayout from '../../components/Admin/AdminAuthLayout';
import AdminLoadingButton from '../../components/Admin/AdminLoadingButton';
import { getErrorMessage } from '../../utils/errorMessage';
import { getPreferredAdminHome } from '../../utils/adminAuth';

const LoginPage: React.FC = () => {
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      await authApi.login({ username: email, password });
      navigate(getPreferredAdminHome());
    } catch (err: unknown) {
      console.error('登录失败:', err);
      setError(getErrorMessage(err, '登录失败，请检查邮箱和密码'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <AdminAuthLayout title="GraphInsight 管理控制台" subtitle="企业级知识图谱运营中枢">
      <Card>
        <CardContent sx={{ p: 4 }}>
          <Stack spacing={2}>
            <Typography variant="h5">欢迎回来</Typography>
            <Typography variant="body2" color="text.secondary">
              请使用管理员邮箱登录继续管理
            </Typography>
          </Stack>

          {error && (
            <Alert severity="error" sx={{ mt: 3 }}>
              {error}
            </Alert>
          )}

          <form onSubmit={handleLogin}>
            <Stack spacing={2} sx={{ mt: 3 }}>
              <TextField
                fullWidth
                label="邮箱"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoFocus
                placeholder="your@email.com"
              />
              <TextField
                fullWidth
                label="密码"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
              <AdminLoadingButton
                fullWidth
                type="submit"
                variant="contained"
                size="large"
                loading={loading}
                disabled={loading}
                sx={{ py: 1.2 }}
                label="登录控制台"
                loadingLabel="登录中..."
              />
              <Typography variant="body2" color="text.secondary" align="center">
                还没有账号？{' '}
                <Link component="button" variant="body2" onClick={() => navigate('/admin/register')}>
                  立即注册
                </Link>
              </Typography>
            </Stack>
          </form>
        </CardContent>
      </Card>
    </AdminAuthLayout>
  );
};

export default LoginPage;
