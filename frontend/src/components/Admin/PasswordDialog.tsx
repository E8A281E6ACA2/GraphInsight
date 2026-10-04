/**
 * 修改密码对话框
 */
import React, { useState } from 'react';
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Button,
  Alert,
  IconButton,
  InputAdornment,
  LinearProgress,
  Box,
  Typography,
} from '@mui/material';
import {
  Visibility,
  VisibilityOff,
  CheckCircle,
  Cancel,
} from '@mui/icons-material';
import { profileApi } from '../../services/adminService';
import { getErrorMessage } from '../../utils/errorMessage';
import AdminLoadingButton from './AdminLoadingButton';

interface PasswordDialogProps {
  open: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

const PasswordDialog: React.FC<PasswordDialogProps> = ({ open, onClose, onSuccess }) => {
  const [formData, setFormData] = useState({
    old_password: '',
    new_password: '',
    confirm_password: '',
  });
  const [showPasswords, setShowPasswords] = useState({
    old: false,
    new: false,
    confirm: false,
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);

  // 密码强度检查
  const getPasswordStrength = (password: string): { score: number; label: string; color: string } => {
    if (!password) return { score: 0, label: '', color: '' };
    
    let score = 0;
    if (password.length >= 8) score += 25;
    if (password.length >= 12) score += 25;
    if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score += 25;
    if (/\d/.test(password)) score += 15;
    if (/[^a-zA-Z\d]/.test(password)) score += 10;

    if (score < 40) return { score, label: '弱', color: 'error.main' };
    if (score < 70) return { score, label: '中等', color: 'warning.main' };
    return { score, label: '强', color: 'success.main' };
  };

  const passwordStrength = getPasswordStrength(formData.new_password);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccess(false);

    // 验证
    if (!formData.old_password) {
      setError('请输入当前密码');
      return;
    }
    if (!formData.new_password) {
      setError('请输入新密码');
      return;
    }
    if (formData.new_password.length < 6) {
      setError('新密码至少需要6个字符');
      return;
    }
    if (formData.new_password === formData.old_password) {
      setError('新密码不能与当前密码相同');
      return;
    }
    if (formData.new_password !== formData.confirm_password) {
      setError('两次输入的新密码不一致');
      return;
    }

    setLoading(true);

    try {
      await profileApi.changePassword({
        old_password: formData.old_password,
        new_password: formData.new_password,
        confirm_password: formData.confirm_password,
      });

      setSuccess(true);
      setTimeout(() => {
        onSuccess?.();
        handleClose();
      }, 1500);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '修改密码失败'));
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    setFormData({
      old_password: '',
      new_password: '',
      confirm_password: '',
    });
    setError('');
    setSuccess(false);
    onClose();
  };

  return (
    <Dialog open={open} onClose={handleClose} maxWidth="sm" fullWidth>
      <DialogTitle>修改密码</DialogTitle>
      <DialogContent>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}

        {success && (
          <Alert severity="success" sx={{ mb: 2 }}>
            密码修改成功！
          </Alert>
        )}

        <form onSubmit={handleSubmit}>
          <TextField
            fullWidth
            label="当前密码"
            type={showPasswords.old ? 'text' : 'password'}
            value={formData.old_password}
            onChange={(e) => setFormData({ ...formData, old_password: e.target.value })}
            margin="normal"
            required
            disabled={loading || success}
            InputProps={{
              endAdornment: (
                <InputAdornment position="end">
                  <IconButton
                    onClick={() => setShowPasswords({ ...showPasswords, old: !showPasswords.old })}
                    edge="end"
                  >
                    {showPasswords.old ? <VisibilityOff /> : <Visibility />}
                  </IconButton>
                </InputAdornment>
              ),
            }}
          />

          <TextField
            fullWidth
            label="新密码"
            type={showPasswords.new ? 'text' : 'password'}
            value={formData.new_password}
            onChange={(e) => setFormData({ ...formData, new_password: e.target.value })}
            margin="normal"
            required
            disabled={loading || success}
            helperText="至少6个字符，建议包含大小写字母、数字和特殊字符"
            InputProps={{
              endAdornment: (
                <InputAdornment position="end">
                  <IconButton
                    onClick={() => setShowPasswords({ ...showPasswords, new: !showPasswords.new })}
                    edge="end"
                  >
                    {showPasswords.new ? <VisibilityOff /> : <Visibility />}
                  </IconButton>
                </InputAdornment>
              ),
            }}
          />

          {formData.new_password && (
            <Box sx={{ mt: 1, mb: 2 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.5 }}>
                <Typography variant="caption" color="text.secondary">
                  密码强度:
                </Typography>
                <Typography
                  variant="caption"
                  sx={{ ml: 1, color: passwordStrength.color, fontWeight: 'bold' }}
                >
                  {passwordStrength.label}
                </Typography>
              </Box>
              <LinearProgress
                variant="determinate"
                value={passwordStrength.score}
                sx={{
                  height: 6,
                  borderRadius: 3,
                  backgroundColor: 'grey.200',
                  '& .MuiLinearProgress-bar': {
                    backgroundColor: passwordStrength.color,
                  },
                }}
              />
            </Box>
          )}

          <TextField
            fullWidth
            label="确认新密码"
            type={showPasswords.confirm ? 'text' : 'password'}
            value={formData.confirm_password}
            onChange={(e) => setFormData({ ...formData, confirm_password: e.target.value })}
            margin="normal"
            required
            disabled={loading || success}
            error={formData.confirm_password !== '' && formData.new_password !== formData.confirm_password}
            helperText={
              formData.confirm_password !== '' && formData.new_password !== formData.confirm_password
                ? '两次输入的密码不一致'
                : ''
            }
            InputProps={{
              endAdornment: (
                <InputAdornment position="end">
                  <IconButton
                    onClick={() => setShowPasswords({ ...showPasswords, confirm: !showPasswords.confirm })}
                    edge="end"
                  >
                    {showPasswords.confirm ? <VisibilityOff /> : <Visibility />}
                  </IconButton>
                  {formData.confirm_password !== '' && (
                    formData.new_password === formData.confirm_password ? (
                      <CheckCircle color="success" sx={{ ml: 1 }} />
                    ) : (
                      <Cancel color="error" sx={{ ml: 1 }} />
                    )
                  )}
                </InputAdornment>
              ),
            }}
          />
        </form>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose} disabled={loading}>
          取消
        </Button>
        <AdminLoadingButton
          onClick={handleSubmit}
          variant="contained"
          loading={loading}
          disabled={loading || success}
          label="确认修改"
          loadingLabel="修改中..."
        />
      </DialogActions>
    </Dialog>
  );
};

export default PasswordDialog;
