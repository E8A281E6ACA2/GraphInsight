/**
 * 个人设置页面
 */
import React, { useEffect, useState } from 'react';
import {
  Box,
  Container,
  Card,
  CardContent,
  TextField,
  Button,
  Typography,
  Alert,
  Avatar,
  Divider,
  ToggleButton,
  ToggleButtonGroup,
} from '@mui/material';
import Grid from '@mui/material/GridLegacy';
import {
  Save as SaveIcon,
  Lock as LockIcon,
  Person as PersonIcon,
} from '@mui/icons-material';
import { useNavigate } from 'react-router-dom';
import { profileApi } from '../../services/adminService';
import type { ProfileInfo } from '../../types/admin';
import PasswordDialog from '../../components/Admin/PasswordDialog';
import AdminLayout from '../../components/Admin/AdminLayout';
import AdminLoadingButton from '../../components/Admin/AdminLoadingButton';
import { LoadingState } from '../../components/Loading/AppleSpinner';
import { getErrorMessage } from '../../utils/errorMessage';
import { clearAdminSession, getPreferredAdminHome, type AdminHomePath } from '../../utils/adminAuth';

const ProfilePage: React.FC = () => {
  const navigate = useNavigate();
  const [profile, setProfile] = useState<ProfileInfo | null>(null);
  const [formData, setFormData] = useState({
    email: '',
    full_name: '',
    phone: '',
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [preferredHome, setPreferredHomeState] = useState<AdminHomePath>(getPreferredAdminHome());

  useEffect(() => {
    fetchProfile();
  }, []);

  const fetchProfile = async () => {
    try {
      setLoading(true);
      setError('');
      
      const data = await profileApi.getProfile();
      setProfile(data);
      setFormData({
        email: data.email || '',
        full_name: data.full_name || '',
        phone: data.phone || '',
      });
      setPreferredHomeState(data.preferred_home_path || getPreferredAdminHome());
    } catch (err: unknown) {
      setError(getErrorMessage(err, '加载个人信息失败'));
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      setError('');
      setSuccess('');

      // 验证邮箱格式
      if (formData.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(formData.email)) {
        setError('邮箱格式不正确');
        return;
      }

      // 验证手机号格式（可选）
      if (formData.phone && !/^1[3-9]\d{9}$/.test(formData.phone)) {
        setError('手机号格式不正确');
        return;
      }

      const updated = await profileApi.updateProfile({
        ...formData,
        preferred_home_path: preferredHome,
      });
      setProfile(updated);
      setPreferredHomeState(updated.preferred_home_path || preferredHome);
      setSuccess('保存成功！');
      
      setTimeout(() => setSuccess(''), 3000);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const handlePasswordChangeSuccess = () => {
    setSuccess('密码修改成功！请重新登录');
    setTimeout(() => {
      clearAdminSession();
      navigate('/admin/login');
    }, 2000);
  };

  const handlePreferredHomeChange = (_event: React.MouseEvent<HTMLElement>, value: AdminHomePath | null) => {
    if (!value) {
      return;
    }
    setPreferredHomeState(value);
  };

  if (loading) {
    return (
      <AdminLayout title="个人设置" subtitle="账号资料、安全与偏好">
        <LoadingState label="正在加载个人资料" minHeight={420} />
      </AdminLayout>
    );
  }

  const actionBar = (
    <AdminLoadingButton
      variant="outlined"
      startIcon={<SaveIcon />}
      loading={saving}
      onClick={handleSave}
      disabled={saving}
      label="保存资料"
      loadingLabel="保存中..."
    />
  );

  return (
    <AdminLayout title="个人设置" subtitle="账号资料、安全与偏好" actions={actionBar}>
      <Container maxWidth="md" sx={{ px: 0 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 3 }} onClose={() => setError('')}>
            {error}
          </Alert>
        )}

        {success && (
          <Alert severity="success" sx={{ mb: 3 }} onClose={() => setSuccess('')}>
            {success}
          </Alert>
        )}

        {/* 基本信息卡片 */}
        <Card sx={{ mb: 3 }}>
          <CardContent>
            <Box sx={{ display: 'flex', alignItems: 'center', mb: 3 }}>
              <PersonIcon sx={{ mr: 1 }} />
              <Typography variant="h6">基本信息</Typography>
            </Box>

            <Grid container spacing={3}>
              {/* 头像区域 */}
              <Grid item xs={12} sx={{ display: 'flex', justifyContent: 'center', mb: 2 }}>
                <Avatar
                  sx={{
                    width: 100,
                    height: 100,
                    bgcolor: 'primary.main',
                    fontSize: '2.5rem',
                  }}
                >
                  {profile?.username?.charAt(0).toUpperCase() || 'U'}
                </Avatar>
              </Grid>

              {/* 用户名（只读） */}
              <Grid item xs={12}>
                <TextField
                  fullWidth
                  label="用户名"
                  value={profile?.username || ''}
                  disabled
                  helperText="用户名不可修改"
                />
              </Grid>

              {/* 邮箱 */}
              <Grid item xs={12}>
                <TextField
                  fullWidth
                  label="邮箱"
                  type="email"
                  value={formData.email}
                  onChange={(e) => setFormData({ ...formData, email: e.target.value })}
                  disabled={saving}
                  placeholder="your@email.com"
                />
              </Grid>

              {/* 真实姓名 */}
              <Grid item xs={12} sm={6}>
                <TextField
                  fullWidth
                  label="真实姓名"
                  value={formData.full_name}
                  onChange={(e) => setFormData({ ...formData, full_name: e.target.value })}
                  disabled={saving}
                  placeholder="请输入真实姓名"
                />
              </Grid>

              {/* 手机号 */}
              <Grid item xs={12} sm={6}>
                <TextField
                  fullWidth
                  label="手机号"
                  value={formData.phone}
                  onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
                  disabled={saving}
                  placeholder="请输入手机号"
                />
              </Grid>
            </Grid>

            <Divider sx={{ my: 3 }} />

            {/* 统计信息 */}
            <Box>
              <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                账户统计
              </Typography>
              <Grid container spacing={2} sx={{ mt: 1 }}>
                <Grid item xs={12} sm={4}>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      注册时间
                    </Typography>
                    <Typography variant="body2">
                      {profile?.created_at ? new Date(profile.created_at).toLocaleDateString() : '-'}
                    </Typography>
                  </Box>
                </Grid>
                <Grid item xs={12} sm={4}>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      最后登录
                    </Typography>
                    <Typography variant="body2">
                      {profile?.last_login ? new Date(profile.last_login).toLocaleString() : '-'}
                    </Typography>
                  </Box>
                </Grid>
                <Grid item xs={12} sm={4}>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      登录次数
                    </Typography>
                    <Typography variant="body2">
                      {profile?.login_count || 0} 次
                    </Typography>
                  </Box>
                </Grid>
              </Grid>
            </Box>

            <Box sx={{ mt: 3, display: 'flex', justifyContent: 'flex-end' }}>
              <AdminLoadingButton
                variant="contained"
                startIcon={<SaveIcon />}
                loading={saving}
                onClick={handleSave}
                disabled={saving}
                label="保存修改"
                loadingLabel="保存中..."
              />
            </Box>
          </CardContent>
        </Card>

        <Card sx={{ mb: 3 }}>
          <CardContent>
            <Box sx={{ display: 'flex', alignItems: 'center', mb: 3 }}>
              <PersonIcon sx={{ mr: 1 }} />
              <Typography variant="h6">进入偏好</Typography>
            </Box>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              登录成功和打开首页时，默认进入你常用的工作区域。
            </Typography>
            <ToggleButtonGroup
              color="primary"
              exclusive
              value={preferredHome}
              onChange={handlePreferredHomeChange}
              sx={{ flexWrap: 'wrap', gap: 1 }}
            >
              <ToggleButton value="/admin/dashboard">系统仪表板</ToggleButton>
              <ToggleButton value="/workspace">图谱工作台</ToggleButton>
            </ToggleButtonGroup>
          </CardContent>
        </Card>

        {/* 安全设置卡片 */}
        <Card>
          <CardContent>
            <Box sx={{ display: 'flex', alignItems: 'center', mb: 3 }}>
              <LockIcon sx={{ mr: 1 }} />
              <Typography variant="h6">安全设置</Typography>
            </Box>

            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', p: 2, bgcolor: 'grey.50', borderRadius: 1 }}>
              <Box>
                <Typography variant="subtitle1">登录密码</Typography>
                <Typography variant="body2" color="text.secondary">
                  定期修改密码可以提高账户安全性
                </Typography>
              </Box>
              <Button
                variant="outlined"
                startIcon={<LockIcon />}
                onClick={() => setPasswordDialogOpen(true)}
              >
                修改密码
              </Button>
            </Box>

            {profile?.last_login_ip && (
              <Box sx={{ mt: 2, p: 2, bgcolor: 'grey.50', borderRadius: 1 }}>
                <Typography variant="subtitle2" gutterBottom>
                  最后登录信息
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  IP 地址: {profile.last_login_ip}
                </Typography>
              </Box>
            )}
          </CardContent>
        </Card>
      </Container>

      {/* 修改密码对话框 */}
      <PasswordDialog
        open={passwordDialogOpen}
        onClose={() => setPasswordDialogOpen(false)}
        onSuccess={handlePasswordChangeSuccess}
      />
    </AdminLayout>
  );
};

export default ProfilePage;
