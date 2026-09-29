import { useMemo } from 'react';
import type { ReactNode } from 'react';
import { ThemeProvider as MuiThemeProvider, CssBaseline } from '@mui/material';
import { lightTheme, darkTheme } from './theme';
import { useGraphStore } from '../store/graphStore';

interface ThemeProviderProps {
  children: ReactNode;
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  const isDarkMode = useGraphStore((state) => state.isDarkMode);

  const theme = useMemo(() => {
    return isDarkMode ? darkTheme : lightTheme;
  }, [isDarkMode]);

  return (
    <MuiThemeProvider theme={theme}>
      <CssBaseline />
      {children}
    </MuiThemeProvider>
  );
}
