import { useState, useRef, forwardRef, useImperativeHandle, useEffect } from 'react';
import { Box, Alert, Paper, Chip, Typography, TextField } from '@mui/material';
import { PlayArrow as ExecuteIcon } from '@mui/icons-material';
import Editor from '@monaco-editor/react';
import type * as Monaco from 'monaco-editor';
import { useGraphStore } from '../../store/graphStore';
import { useCypher } from '../../hooks/useCypher';
import { getGraphSchema } from '../../services/graphService';
import type { GraphSchemaSummary } from '../../types/api';
import { getErrorMessage } from '../../utils/errorMessage';
import { AppleSpinner } from '../Loading/AppleSpinner';
import LoadingButton from '../Loading/LoadingButton';

const FALLBACK_QUERY = `// 自动发现：当前图数据库暂未返回结构，先查看节点
MATCH (n)
RETURN n
LIMIT 80`;

const SCHEMA_CACHE_TTL_MS = 5000;

let schemaCache: { value: GraphSchemaSummary; fetchedAt: number } | null = null;
let schemaPromise: Promise<GraphSchemaSummary> | null = null;

// 切换知识库后结构缓存必须作废（schema 是按 KB 作用域发现的）。
function invalidateGraphSchemaCache() {
  schemaCache = null;
  schemaPromise = null;
}

function loadGraphSchemaOnce() {
  const now = Date.now();
  if (schemaCache && now - schemaCache.fetchedAt < SCHEMA_CACHE_TTL_MS) {
    return Promise.resolve(schemaCache.value);
  }
  if (!schemaPromise) {
    schemaPromise = getGraphSchema()
      .then((value) => {
        schemaCache = { value, fetchedAt: Date.now() };
        return value;
      })
      .finally(() => {
        schemaPromise = null;
      });
  }
  return schemaPromise;
}

export interface CypherEditorRef {
  setValue: (value: string) => void;
  executeQuery: () => void;
}

export const CypherEditor = forwardRef<CypherEditorRef>((_props, ref) => {
  const [query, setQuery] = useState('');
  const [schema, setSchema] = useState<GraphSchemaSummary | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(true);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const userEditedRef = useRef(false);
  const schemaQueryAppliedRef = useRef(false);
  const programmaticQueryRef = useRef<string | null>(null);
  const editorRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);

  const isDarkMode = useGraphStore((state) => state.isDarkMode);
  const activeKbId = useGraphStore((state) => state.activeKbId);
  const { execute, isExecuting, error, clearError } = useCypher();

  const handleExecute = async () => {
    // 未选择知识库时不发起无作用域查询（后端会 KB_SCOPE_REQUIRED）。
    if (!activeKbId) {
      clearError();
      return;
    }
    await execute(query);
  };

  const applyQueryValue = (value: string, options: { markEdited?: boolean } = {}) => {
    const markEdited = options.markEdited ?? true;
    if (markEdited) {
      userEditedRef.current = true;
      programmaticQueryRef.current = null;
    } else {
      programmaticQueryRef.current = value;
    }
    setQuery(value);
    const editor = editorRef.current;
    if (editor && editor.getValue() !== value) {
      editor.setValue(value);
    }
  };

  useEffect(() => {
    // 门控：无 activeKbId 时不探测 schema（避发无作用域请求）；切库时作废缓存后重新探测。
    if (!activeKbId) {
      invalidateGraphSchemaCache();
      setSchema(null);
      setSchemaLoading(false);
      setSchemaError(null);
      return;
    }
    invalidateGraphSchemaCache();
    let cancelled = false;

    const loadSchema = async () => {
      setSchemaLoading(true);
      setSchemaError(null);
      try {
        const discovered = await loadGraphSchemaOnce();
        if (cancelled) {
          return;
        }
        setSchema(discovered);
        const sampleQuery = discovered.sampleQuery?.trim() || FALLBACK_QUERY;
        if (!userEditedRef.current && !schemaQueryAppliedRef.current) {
          schemaQueryAppliedRef.current = true;
          applyQueryValue(sampleQuery, { markEdited: false });
        }
      } catch (err: unknown) {
        if (!cancelled) {
          setSchemaError(formatGraphError(err, '图数据库结构探测失败'));
          if (!userEditedRef.current && !schemaQueryAppliedRef.current) {
            schemaQueryAppliedRef.current = true;
            applyQueryValue(FALLBACK_QUERY, { markEdited: false });
          }
        }
      } finally {
        if (!cancelled) {
          setSchemaLoading(false);
        }
      }
    };

    loadSchema();

    return () => {
      cancelled = true;
    };
  }, [activeKbId]);

  // 暴露方法给父组件
  useImperativeHandle(ref, () => ({
    setValue: (value: string) => {
      applyQueryValue(value, { markEdited: true });
    },
    executeQuery: () => {
      handleExecute();
    },
  }));

  const handleEditorDidMount = (editor: Monaco.editor.IStandaloneCodeEditor, monaco: typeof Monaco) => {
    editorRef.current = editor;

    // 添加 Ctrl+Enter 快捷键
    editor.addCommand(
      monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter,
      () => {
        handleExecute();
      }
    );
  };

  const handleFallbackChange = (value: string) => {
    applyQueryValue(value, { markEdited: true });
  };

  const editorFallback = (
    <TextField
      value={query}
      onChange={(event) => handleFallbackChange(event.target.value)}
      onKeyDown={(event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
          event.preventDefault();
          void handleExecute();
        }
      }}
      multiline
      fullWidth
      variant="standard"
      placeholder="输入 Cypher 查询..."
      InputProps={{
        disableUnderline: true,
        sx: {
          height: '100%',
          alignItems: 'flex-start',
          fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, monospace',
          fontSize: 13,
          lineHeight: 1.5,
          p: 1,
        },
      }}
      sx={{
        height: '100%',
        '& .MuiInputBase-root': { height: '100%' },
        '& textarea': {
          height: '100% !important',
          overflow: 'auto !important',
        },
      }}
    />
  );

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {!activeKbId ? (
        <Alert severity="info" sx={{ py: 0.5 }} data-testid="cypher-kb-required">
          请先在右上角选择知识库，再探测图谱结构与执行查询。
        </Alert>
      ) : (
        <SchemaSummary schema={schema} loading={schemaLoading} error={schemaError} />
      )}

      {/* 编辑器 */}
      <Paper
        elevation={0}
        sx={{
          height: 180,
          overflow: 'hidden',
          border: 1,
          borderColor: 'divider',
          borderRadius: 1,
        }}
      >
        <Editor
          height="180px"
          defaultLanguage="sql" // 使用 SQL 作为 Cypher 的基础语法高亮
          value={query}
          loading={editorFallback}
          onChange={(value) => {
            const nextValue = value || '';
            if (programmaticQueryRef.current === nextValue) {
              programmaticQueryRef.current = null;
              setQuery(nextValue);
              return;
            }
            userEditedRef.current = true;
            setQuery(nextValue);
          }}
          onMount={handleEditorDidMount}
          theme={isDarkMode ? 'vs-dark' : 'light'}
          options={{
            minimap: { enabled: false },
            fontSize: 13,
            fontWeight: 'normal',
            lineNumbers: 'on',
            scrollBeyondLastLine: false,
            automaticLayout: true,
            tabSize: 2,
            wordWrap: 'on',
            wrappingIndent: 'indent',
            padding: { top: 8, bottom: 8 },
          }}
        />
      </Paper>

      {/* 执行按钮 */}
      <LoadingButton
        variant="contained"
        color="primary"
        size="large"
        startIcon={<ExecuteIcon />}
        loading={isExecuting}
        onClick={handleExecute}
        disabled={isExecuting || !query.trim() || !activeKbId}
        fullWidth
        label="执行查询 (Ctrl+Enter)"
        loadingLabel="执行中..."
      />

      {/* 错误消息 */}
      {error && (
        <Alert
          severity="error"
          onClose={clearError}
        >
          {error}
        </Alert>
      )}
    </Box>
  );
});

function SchemaSummary({
  schema,
  loading,
  error,
}: {
  schema: GraphSchemaSummary | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading) {
    return (
      <Paper
        elevation={0}
        sx={{ border: 1, borderColor: 'divider', borderRadius: 1, px: 1.5, py: 1 }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <AppleSpinner size={18} compact />
          <Typography variant="body2" color="text.secondary">
            正在探测图数据库结构...
          </Typography>
        </Box>
      </Paper>
    );
  }

  if (error) {
    return (
      <Alert severity="warning" sx={{ py: 0.5 }}>
        {error}
      </Alert>
    );
  }

  if (!schema) {
    return null;
  }

  const topLabels = schema.labels.slice(0, 4);
  const topRelationships = schema.relationships.slice(0, 4);
  const topPattern = schema.patterns[0];

  return (
    <Paper
      elevation={0}
      sx={{
        border: 1,
        borderColor: 'divider',
        borderRadius: 1,
        px: 1.5,
        py: 1.25,
        bgcolor: 'background.default',
      }}
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1 }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            当前图谱结构
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {schema.stats.nodeCount} 节点 / {schema.stats.edgeCount} 关系
          </Typography>
        </Box>

        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75 }}>
          {topLabels.map((item) => (
            <Chip key={item.label} label={formatSchemaChip(item.label, item.count)} size="small" variant="outlined" />
          ))}
          {topRelationships.map((item) => (
            <Chip key={item.type} label={formatSchemaChip(item.type, item.count)} size="small" color="primary" variant="outlined" />
          ))}
        </Box>

        {topPattern && (
          <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-word' }}>
            最常见模式：({topPattern.sourceLabels.join(':') || 'Node'})-[:{topPattern.relationship}]-&gt;(
            {topPattern.targetLabels.join(':') || 'Node'})，{topPattern.count} 条
          </Typography>
        )}
      </Box>
    </Paper>
  );
}

function formatSchemaChip(name: string, count: number) {
  return count > 0 ? `${name} ${count}` : name;
}

function formatGraphError(err: unknown, fallback: string) {
  const code = err && typeof err === 'object' && 'code' in err ? (err as { code?: unknown }).code : undefined;
  if (code === 'DATABASE_UNAVAILABLE') {
    return 'Neo4j 当前不可用：请先检查 /health，确认 Neo4j 已启动且 Bolt 可连接。';
  }
  if (code === 'DATABASE_MEMORY_EXHAUSTED') {
    return 'Neo4j 事务内存已耗尽：请重启 Neo4j 或终止重查询后再试。';
  }
  if (code === 'QUERY_TIMEOUT') {
    return '图数据库结构探测超时：请使用更窄的标签、关系类型或属性条件查询。';
  }
  return getErrorMessage(err, fallback);
}
