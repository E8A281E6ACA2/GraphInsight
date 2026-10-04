import { useState } from 'react';
import {
  Box,
  TextField,
  Button,
  Alert,
  Chip,
  Typography,
  Paper,
  IconButton,
  Collapse,
} from '@mui/material';
import {
  AutoAwesome as AIIcon,
  PlayArrow as ExecuteIcon,
  Refresh as RefreshIcon,
  ExpandMore as ExpandIcon,
  ExpandLess as CollapseIcon,
} from '@mui/icons-material';
import { buildApiUrl } from '../../utils/apiBase';
import { getErrorMessage } from '../../utils/errorMessage';
import LoadingButton from '../Loading/LoadingButton';

interface NL2CypherInputProps {
  onCypherGenerated: (cypher: string) => void;
  onExecute: (cypher: string) => void;
}

type NL2CypherResult = {
  success: boolean;
  cypher?: string;
  error?: string;
  explanation?: string;
  confidence?: number;
};

export function NL2CypherInput({ onCypherGenerated, onExecute }: NL2CypherInputProps) {
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<NL2CypherResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showExamples, setShowExamples] = useState(true);

  const examples = [
    '查找所有水稻相关的病虫害',
    '显示小麦和它的防治方法',
    '找出影响玉米的所有疾病',
    '查询所有作物和它们的病害数量',
  ];

  const handleGenerate = async () => {
    if (!input.trim()) return;

    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const response = await fetch(buildApiUrl('/nl2cypher'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ natural_language: input }),
      });

      const data = (await response.json()) as NL2CypherResult;
      console.log('[NL2CypherInput] API 响应:', data);

      if (data.success && data.cypher) {
        console.log('[NL2CypherInput] 生成成功，Cypher:', data.cypher);
        setResult(data);
        onCypherGenerated(data.cypher);
      } else {
        console.error('[NL2CypherInput] 生成失败:', data.error);
        setError(data.error || '生成失败');
      }
    } catch (err: unknown) {
      setError('生成失败：' + getErrorMessage(err, '未知错误'));
    } finally {
      setLoading(false);
    }
  };

  const handleExecute = () => {
    if (result?.cypher) {
      onExecute(result.cypher);
      // 清空结果，准备下一次查询
      setResult(null);
      setInput('');
    }
  };

  const handleRegenerate = () => {
    setResult(null);
    handleGenerate();
  };

  return (
    <Paper elevation={2} sx={{ p: 2, mb: 2, bgcolor: 'background.paper' }}>
      {/* 标题 */}
      <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
        <AIIcon color="primary" sx={{ mr: 1 }} />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>
          AI 查询助手
        </Typography>
        <IconButton
          size="small"
          onClick={() => setShowExamples(!showExamples)}
        >
          {showExamples ? <CollapseIcon /> : <ExpandIcon />}
        </IconButton>
      </Box>

      {/* 输入框 */}
      <Box sx={{ display: 'flex', gap: 1, mb: 2 }}>
        <TextField
          fullWidth
          placeholder="用自然语言描述你的查询，例如：查找所有水稻相关的病虫害"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyPress={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleGenerate();
            }
          }}
          disabled={loading}
          size="small"
          multiline
          maxRows={3}
        />
        <LoadingButton
          variant="contained"
          startIcon={<AIIcon />}
          loading={loading}
          onClick={handleGenerate}
          disabled={loading || !input.trim()}
          label="生成"
          loadingLabel="生成中..."
          sx={{ minWidth: 100 }}
        />
      </Box>

      {/* 示例 */}
      <Collapse in={showExamples}>
        <Box sx={{ mb: 2 }}>
          <Typography variant="caption" color="text.secondary" sx={{ mb: 1, display: 'block' }}>
            提示: 点击示例快速开始：
          </Typography>
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            {examples.map((example, index) => (
              <Chip
                key={index}
                label={example}
                size="small"
                onClick={() => setInput(example)}
                sx={{ cursor: 'pointer' }}
                variant="outlined"
              />
            ))}
          </Box>
        </Box>
      </Collapse>

      {/* 生成结果 */}
      {result && (
        <Alert
          severity="success"
          sx={{ mb: 0 }}
          action={
            <Box sx={{ display: 'flex', gap: 1 }}>
              <Button
                color="inherit"
                size="small"
                startIcon={<RefreshIcon />}
                onClick={handleRegenerate}
              >
                重新生成
              </Button>
              <Button
                color="inherit"
                size="small"
                startIcon={<ExecuteIcon />}
                onClick={handleExecute}
                variant="outlined"
              >
                执行查询
              </Button>
            </Box>
          }
        >
          <Typography variant="body2" sx={{ mb: 1, fontWeight: 'bold' }}>
            已生成 Cypher 查询
          </Typography>
          <Typography variant="body2" sx={{ mb: 1 }}>
            {result.explanation || '已根据问题生成查询语句'}
          </Typography>
          <Box
            sx={{
              bgcolor: 'rgba(0, 0, 0, 0.05)',
              p: 1,
              borderRadius: 1,
              fontFamily: 'monospace',
              fontSize: '0.875rem',
              mb: 1,
              overflowX: 'auto',
            }}
          >
            {result.cypher}
          </Box>
          <Typography variant="caption" color="text.secondary">
            置信度：{((result.confidence ?? 0) * 100).toFixed(0)}%
          </Typography>
        </Alert>
      )}

      {/* 错误提示 */}
      {error && (
        <Alert severity="error" onClose={() => setError(null)}>
          <Typography variant="body2" sx={{ mb: 1 }}>
            {error}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            提示：请确保 OpenAI API Key 已正确配置
          </Typography>
        </Alert>
      )}
    </Paper>
  );
}
