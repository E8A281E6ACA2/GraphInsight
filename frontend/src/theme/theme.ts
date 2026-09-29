import { createTheme } from '@mui/material/styles';
import type { ThemeOptions } from '@mui/material/styles';

// 专业知识图谱配色方案 - 现代农业科技风格
const lightPalette = {
  primary: {
    main: '#1565c0', // 深蓝色 - 代表科技、数据
    light: '#42a5f5',
    dark: '#0d47a1',
    contrastText: '#ffffff',
  },
  secondary: {
    main: '#2e7d32', // 深绿色 - 代表农业、生长
    light: '#66bb6a',
    dark: '#1b5e20',
    contrastText: '#ffffff',
  },
  tertiary: {
    main: '#7b1fa2', // 紫色 - 代表创新、智能
    light: '#ba68c8',
    dark: '#4a148c',
  },
  error: {
    main: '#d32f2f', // 红色 - 代表病害、错误
    light: '#ef5350',
    dark: '#c62828',
  },
  warning: {
    main: '#f57c00', // 橙色 - 代表虫害、警告
    light: '#ff9800',
    dark: '#ef6c00',
  },
  info: {
    main: '#0288d1', // 信息蓝
    light: '#03a9f4',
    dark: '#01579b',
  },
  success: {
    main: '#388e3c', // 成功绿
    light: '#4caf50',
    dark: '#2e7d32',
  },
  background: {
    default: '#f8fafc', // 更柔和的背景
    paper: '#ffffff',
    surface: '#f1f5f9', // 表面色
  },
  text: {
    primary: '#1e293b', // 更深的文字色
    secondary: '#64748b',
    disabled: '#94a3b8',
  },
  divider: '#e2e8f0',
  grey: {
    50: '#f8fafc',
    100: '#f1f5f9',
    200: '#e2e8f0',
    300: '#cbd5e1',
    400: '#94a3b8',
    500: '#64748b',
    600: '#475569',
    700: '#334155',
    800: '#1e293b',
    900: '#0f172a',
  },
};

const darkPalette = {
  primary: {
    main: '#3b82f6', // 亮蓝色
    light: '#60a5fa',
    dark: '#1d4ed8',
    contrastText: '#ffffff',
  },
  secondary: {
    main: '#10b981', // 亮绿色
    light: '#34d399',
    dark: '#059669',
    contrastText: '#ffffff',
  },
  tertiary: {
    main: '#a855f7', // 亮紫色
    light: '#c084fc',
    dark: '#7c3aed',
  },
  error: {
    main: '#ef4444',
    light: '#f87171',
    dark: '#dc2626',
  },
  warning: {
    main: '#f59e0b',
    light: '#fbbf24',
    dark: '#d97706',
  },
  info: {
    main: '#06b6d4',
    light: '#22d3ee',
    dark: '#0891b2',
  },
  success: {
    main: '#10b981',
    light: '#34d399',
    dark: '#059669',
  },
  background: {
    default: '#0f172a', // 深色背景
    paper: '#1e293b',
    surface: '#334155',
  },
  text: {
    primary: '#f1f5f9',
    secondary: '#cbd5e1',
    disabled: '#64748b',
  },
  divider: '#475569',
  grey: {
    50: '#0f172a',
    100: '#1e293b',
    200: '#334155',
    300: '#475569',
    400: '#64748b',
    500: '#94a3b8',
    600: '#cbd5e1',
    700: '#e2e8f0',
    800: '#f1f5f9',
    900: '#f8fafc',
  },
};

// 共享的主题配置 - 专业现代风格
const commonThemeOptions: ThemeOptions = {
  typography: {
    fontFamily: [
      '"Inter"',
      '"SF Pro Display"',
      '-apple-system',
      'BlinkMacSystemFont',
      '"Segoe UI"',
      'Roboto',
      '"Helvetica Neue"',
      'Arial',
      'sans-serif',
    ].join(','),
    h1: {
      fontSize: '2.5rem',
      fontWeight: 700,
      lineHeight: 1.2,
      letterSpacing: '-0.025em',
    },
    h2: {
      fontSize: '2rem',
      fontWeight: 600,
      lineHeight: 1.3,
      letterSpacing: '-0.025em',
    },
    h3: {
      fontSize: '1.75rem',
      fontWeight: 600,
      lineHeight: 1.3,
    },
    h4: {
      fontSize: '1.5rem',
      fontWeight: 600,
      lineHeight: 1.4,
    },
    h5: {
      fontSize: '1.25rem',
      fontWeight: 600,
      lineHeight: 1.4,
    },
    h6: {
      fontSize: '1.125rem',
      fontWeight: 600,
      lineHeight: 1.4,
    },
    body1: {
      fontSize: '1rem',
      lineHeight: 1.6,
    },
    body2: {
      fontSize: '0.875rem',
      lineHeight: 1.5,
      fontWeight: 400,
    },
    caption: {
      fontSize: '0.75rem',
      lineHeight: 1.4,
      fontWeight: 400,
    },
  },
  spacing: 8,
  shape: {
    borderRadius: 12, // 更现代的圆角
  },
};

// 创建浅色主题 - 专业现代风格
export const lightTheme = createTheme({
  ...commonThemeOptions,
  palette: {
    mode: 'light',
    ...lightPalette,
  },
  components: {
    MuiAppBar: {
      styleOverrides: {
        root: {
          backgroundColor: '#ffffff',
          color: '#1e293b',
          borderBottom: '1px solid #e2e8f0',
          boxShadow: '0 1px 3px rgba(0, 0, 0, 0.08)',
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          textTransform: 'none',
          fontWeight: 500,
          borderRadius: 8,
        },
        contained: {
          boxShadow: 'none',
          '&:hover': {
            boxShadow: '0 4px 12px rgba(21, 101, 192, 0.3)',
          },
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
          border: '1px solid #e2e8f0',
        },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          borderRadius: 12,
          border: '1px solid #e2e8f0',
          boxShadow: '0 1px 3px rgba(0, 0, 0, 0.08)',
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          borderRadius: 6,
          fontWeight: 500,
        },
      },
    },
    MuiIconButton: {
      styleOverrides: {
        root: {
          borderRadius: 8,
          '&:hover': {
            backgroundColor: 'rgba(0, 0, 0, 0.04)',
          },
        },
      },
    },
    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          fontSize: '0.875rem',
          borderRadius: 8,
        },
      },
    },
  },
});

// 创建深色主题 - 专业现代风格
export const darkTheme = createTheme({
  ...commonThemeOptions,
  palette: {
    mode: 'dark',
    ...darkPalette,
  },
  components: {
    MuiAppBar: {
      styleOverrides: {
        root: {
          backgroundColor: '#1e293b',
          color: '#f1f5f9',
          borderBottom: '1px solid #475569',
          boxShadow: '0 1px 3px rgba(0, 0, 0, 0.4)',
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          textTransform: 'none',
          fontWeight: 500,
          borderRadius: 8,
        },
        contained: {
          boxShadow: 'none',
          '&:hover': {
            boxShadow: '0 4px 12px rgba(59, 130, 246, 0.3)',
          },
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
          border: '1px solid #475569',
        },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          borderRadius: 12,
          border: '1px solid #475569',
          boxShadow: '0 1px 3px rgba(0, 0, 0, 0.4)',
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          borderRadius: 6,
          fontWeight: 500,
        },
      },
    },
    MuiIconButton: {
      styleOverrides: {
        root: {
          borderRadius: 8,
          '&:hover': {
            backgroundColor: 'rgba(255, 255, 255, 0.08)',
          },
        },
      },
    },
    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          fontSize: '0.875rem',
          borderRadius: 8,
        },
      },
    },
  },
});
