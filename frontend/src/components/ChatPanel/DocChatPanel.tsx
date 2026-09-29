import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Box,
  Button,
  Chip,
  Collapse,
  Divider,
  IconButton,
  MenuItem,
  TextField,
  Typography,
} from '@mui/material';
import {
  AutoGraph as AutoGraphIcon,
  AttachFile as AttachFileIcon,
  ExpandLess as ExpandLessIcon,
  ExpandMore as ExpandMoreIcon,
  Send as SendIcon,
  TipsAndUpdates as TipsIcon,
  Tune as TuneIcon,
  MenuBook as MenuBookIcon,
} from '@mui/icons-material';
import { triggerGraphBuild } from '../../services/graphBuild';
import { reportClientLog } from '../../services/clientLog';
import { useGraphStore } from '../../store/graphStore';
import { askDocDeepResearch, askDocQa, type DocQaConversationTurn, type ReasoningProfile } from '../../services/docQa';
import { loadDocumentOverviewGraph, loadFocusedDocumentGraph } from '../../services/documentGraphSync';
import { uploadDocuments } from '../../services/documents';
import { getErrorMessage } from '../../utils/errorMessage';
import LoadingButton from '../Loading/LoadingButton';

interface Citation {
  id: string;
  title: string;
  snippet: string;
  location?: string;
  entity_names?: string[];
  retrieval_score?: number;
  confidence?: number;
  confidence_level?: 'high' | 'medium' | 'low' | string;
}

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  mode?: 'qa' | 'deep_research';
  summary?: string;
  finalConclusion?: string;
  confidence?: {
    score: number;
    level: 'high' | 'medium' | 'low' | string;
    reason?: string;
  };
  subQuestions?: string[];
  evidenceStats?: {
    sub_questions: number;
    answered_sub_questions?: number;
    coverage_ratio?: number;
    retrieved_chunks: number;
    unique_citations: number;
    avg_citation_confidence?: number;
  };
}

const CITATION_COUNT = 2;
const REASONING_PROFILE_OPTIONS: Array<{ value: ReasoningProfile; label: string }> = [
  { value: 'fast', label: 'fast' },
  { value: 'balanced', label: 'balanced' },
  { value: 'deep', label: 'deep' },
];

const quickPrompts = [
  '总结这批文档的核心主题',
  '列出关键人物与关系',
  '生成一份结构化大纲',
  '找出政策中的关键时间点',
  '请基于当前文档做一份深度调研报告',
];

const QUERY_STOPWORDS = new Set([
  '如何',
  '怎么',
  '怎样',
  '请问',
  '一下',
  '关于',
  '这个',
  '那个',
  '哪些',
  '以及',
  '我们',
  '你们',
  '他们',
  '是否',
  '可以',
  '能够',
  '进行',
  '对于',
  '什么',
  '问题',
  '分析',
  '研究',
  '报告',
  '文档',
  '资料',
  '知识库',
]);

export function DocChatPanel() {
  const setSelectedCitation = useGraphStore((state) => state.setSelectedCitation);
  const setWorkspaceTab = useGraphStore((state) => state.setWorkspaceTab);
  const setGraphData = useGraphStore((state) => state.setGraphData);
  const setLastQueryStats = useGraphStore((state) => state.setLastQueryStats);
  const addQueryToHistory = useGraphStore((state) => state.addQueryToHistory);
  const setHighlightAll = useGraphStore((state) => state.setHighlightAll);
  const recentUploadedDocIds = useGraphStore((state) => state.recentUploadedDocIds);
  const setRecentUploadedDocIds = useGraphStore((state) => state.setRecentUploadedDocIds);
  const requestDocumentRefresh = useGraphStore((state) => state.requestDocumentRefresh);
  const activeKbId = useGraphStore((state) => state.activeKbId);
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content: '已连接文档知识库。你可以直接提问，我会给出答案并定位引用证据。',
    },
  ]);
  const [input, setInput] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const [typingLabel, setTypingLabel] = useState('正在整理答案...');
  const [buildStatus, setBuildStatus] = useState<'idle' | 'running' | 'done'>('idle');
  const [buildError, setBuildError] = useState<string | null>(null);
  const [buildSummary, setBuildSummary] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadSummary, setUploadSummary] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [graphSyncMode, setGraphSyncMode] = useState<'precise' | 'full'>('precise');
  const [qaReasoningProfile, setQaReasoningProfile] = useState<ReasoningProfile>('balanced');
  const [deepResearchReasoningProfile, setDeepResearchReasoningProfile] = useState<ReasoningProfile>('deep');
  const [showAdvancedOptions, setShowAdvancedOptions] = useState(false);
  const [showPromptSuggestions, setShowPromptSuggestions] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messageScrollRef = useRef<HTMLDivElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const lastSyncRef = useRef<{ citations: Citation[]; question: string; answer: string } | null>(null);

  const extractQuestionKeywords = (question: string) => {
    const tokens = (question || '')
      .toLowerCase()
      .match(/[\u4e00-\u9fff0-9]{2,12}|[a-z][a-z0-9_-]{2,}/g);
    if (!tokens) return [] as string[];
    return Array.from(
      new Set(
        tokens
          .map((token) => token.trim())
          .filter((token) => token.length >= 2 && !QUERY_STOPWORDS.has(token))
      )
    ).slice(0, 10);
  };

  const normalizeEntityNames = (citations: Citation[]) =>
    Array.from(
      new Set(
        citations
          .flatMap((item) => item.entity_names || [])
          .map((name) => (name || '').trim().toLowerCase())
          .filter(Boolean)
      )
    ).slice(0, 60);

  const collectChunkIds = (citations: Citation[]) =>
    Array.from(
      new Set(
        citations
          .map((item) => (item.id || '').trim())
          .filter(Boolean)
      )
    ).slice(0, 60);

  const buildConversationHistory = (items: ChatMessage[]): DocQaConversationTurn[] =>
    items
      .filter((item) => item.id !== 'welcome')
      .filter((item) => item.role === 'user' || item.role === 'assistant')
      .map((item) => ({
        role: item.role,
        content: item.content.replace(/\s+/g, ' ').trim().slice(0, 1200),
      }))
      .filter((item) => item.content.length > 0)
      .slice(-6);

  const applyCitationSelection = useCallback((
    citation: Citation,
    openGraph = true,
    extraKeywords?: string[]
  ) => {
    setSelectedCitation({
      id: citation.id,
      title: citation.title,
      snippet: citation.snippet,
      location: citation.location,
      entityNames: citation.entity_names || [],
      keywords: extraKeywords?.length ? extraKeywords : undefined,
      retrievalScore: citation.retrieval_score,
      confidence: citation.confidence,
      confidenceLevel: citation.confidence_level,
    });
    if (openGraph) {
      setWorkspaceTab('graph');
    }
  }, [setSelectedCitation, setWorkspaceTab]);

  const handleSend = async () => {
    const value = input.trim();
    if (!value || isTyping) return;
    // 未选择知识库时不发起无作用域问答（后端会 KB_SCOPE_REQUIRED）。
    if (!activeKbId) {
      setMessages((prev) => [
        ...prev,
        {
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          content: '请先在右上角选择知识库，再发起文档问答。',
          citations: [],
        },
      ]);
      return;
    }

    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: value,
    };
    const conversationHistory = buildConversationHistory(messages);

    setMessages((prev) => [...prev, userMessage]);
    setSelectedCitation(null);
    setInput('');
    setIsTyping(true);
    setTypingLabel('正在整理答案...');

    try {
      const result = await askDocQa(value, CITATION_COUNT, qaReasoningProfile, conversationHistory);
      const citations = result?.citations || [];
      const assistantMessage: ChatMessage = {
        id: `assistant-${Date.now()}`,
        role: 'assistant',
        content: result?.answer || `我已基于文档库检索到与「${value.slice(0, 24)}」相关的内容，并整理如下。`,
        citations,
        mode: 'qa',
      };
      setMessages((prev) => [...prev, assistantMessage]);
      const answerText = result?.answer || '';
      await syncGraphWithCitations(citations, value, answerText);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setMessages((prev) => [
        ...prev,
        {
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          content: getErrorMessage(error, '抱歉，当前问答服务不可用，请稍后重试。'),
          citations: [],
        },
      ]);
      reportClientLog({
        level: 'error',
        message: '文档问答失败',
        source: 'doc_chat',
        event: 'doc_qa',
        context: { error: message },
      });
    } finally {
      setIsTyping(false);
    }
  };

  const handleDeepResearch = async () => {
    const value = input.trim();
    if (!value || isTyping) return;
    // 未选择知识库时不发起无作用域深度调研。
    if (!activeKbId) {
      setMessages((prev) => [
        ...prev,
        {
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          content: '请先在右上角选择知识库，再发起深度调研。',
          citations: [],
          mode: 'deep_research',
        },
      ]);
      return;
    }

    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: `[深度调研] ${value}`,
    };

    setMessages((prev) => [...prev, userMessage]);
    setSelectedCitation(null);
    setInput('');
    setIsTyping(true);
    setTypingLabel('正在进行深度调研...');

    try {
      const result = await askDocDeepResearch(value, {
        topK: 8,
        maxSubQuestions: 4,
        reasoningProfile: deepResearchReasoningProfile,
      });
      const citations = result?.citations || [];
      const assistantMessage: ChatMessage = {
        id: `assistant-${Date.now()}`,
        role: 'assistant',
        content: result?.report || result?.summary || '已生成深度调研报告。',
        summary: result?.summary || '',
        finalConclusion: result?.final_conclusion || '',
        confidence: result?.confidence,
        subQuestions: result?.sub_questions || [],
        evidenceStats: result?.evidence_stats,
        citations,
        mode: 'deep_research',
      };
      setMessages((prev) => [...prev, assistantMessage]);
      const answerText = result?.report || result?.summary || '';
      await syncGraphWithCitations(citations, value, answerText);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setMessages((prev) => [
        ...prev,
        {
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          content: getErrorMessage(error, '抱歉，深度调研暂时不可用，请稍后重试。'),
          citations: [],
          mode: 'deep_research',
        },
      ]);
      reportClientLog({
        level: 'error',
        message: '文档深度调研失败',
        source: 'doc_chat',
        event: 'doc_deep_research',
        context: { error: message },
      });
    } finally {
      setIsTyping(false);
      setTypingLabel('正在整理答案...');
    }
  };

  const handlePromptClick = (prompt: string) => {
    setInput(prompt);
  };

  const handleUpload = useCallback(
    async (files: File[]) => {
      if (files.length === 0 || uploading) return;
      setUploading(true);
      setUploadSummary(null);
      setUploadError(null);
      try {
        const result = await uploadDocuments(files);
        const uploaded = result?.uploaded?.length || 0;
        const skipped = result?.skipped?.length || 0;
        const uploadedDocIds = (result?.uploaded || [])
          .map((item) => item.doc_id || item.id)
          .filter((item): item is string => Boolean(item));
        setRecentUploadedDocIds(uploadedDocIds);
        requestDocumentRefresh();
        setUploadSummary(`上传成功 ${uploaded} · 跳过 ${skipped}`);
        setWorkspaceTab('document');
      } catch (error) {
        setUploadError(getErrorMessage(error, '文档上传失败'));
      } finally {
        setUploading(false);
      }
    },
    [requestDocumentRefresh, setRecentUploadedDocIds, setWorkspaceTab, uploading]
  );

  const handleFileSelect = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    void handleUpload(files);
    event.target.value = '';
  };

  const handleChatDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    const files = Array.from(event.dataTransfer.files || []);
    if (files.length > 0) {
      void handleUpload(files);
    }
  };

  const handleCitationClick = (citation: Citation) => {
    applyCitationSelection(citation, true);
  };

  const runDocGraphQuery = useCallback(async () => {
    try {
      const { graphData, query } = await loadDocumentOverviewGraph();
      setGraphData(graphData);
      if (graphData.stats) {
        setLastQueryStats(graphData.stats);
      }
      addQueryToHistory(query, graphData.nodes.length + graphData.edges.length);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      reportClientLog({
        level: 'warn',
        message: '文档图谱查询失败',
        source: 'doc_chat',
        event: 'doc_graph_query',
        context: { error: message },
      });
    }
  }, [addQueryToHistory, setGraphData, setLastQueryStats]);

  const syncGraphWithCitations = useCallback(async (
    citations: Citation[],
    question: string,
    answerText: string
  ) => {
    lastSyncRef.current = { citations, question, answer: answerText };
    const rankedCitations = [...citations].sort((a, b) => {
      const aScore = (a.confidence ?? a.retrieval_score ?? 0);
      const bScore = (b.confidence ?? b.retrieval_score ?? 0);
      return bScore - aScore;
    });
    const focusCitations = rankedCitations.slice(0, 1);
    const chunkIds = collectChunkIds(focusCitations);
    const entityNames = normalizeEntityNames(focusCitations);
    const questionKeywords = extractQuestionKeywords(question);
    const answerKeywords = extractQuestionKeywords(answerText);
    const keywords = Array.from(new Set([...questionKeywords, ...answerKeywords])).slice(0, 16);

    try {
      if (graphSyncMode === 'full') {
        setHighlightAll(true);
        await runDocGraphQuery();
        if (focusCitations.length > 0) {
          applyCitationSelection(focusCitations[0], true, keywords);
        } else {
          setSelectedCitation(null);
          setWorkspaceTab('graph');
        }
        return;
      }
      setHighlightAll(false);

      const { graphData: result, query } = await loadFocusedDocumentGraph({
        chunkIds,
        entityNames,
        keywords,
      });

      setGraphData(result);
      if (result.stats) {
        setLastQueryStats(result.stats);
      }
      addQueryToHistory(query, result.nodes.length + result.edges.length);

      if (focusCitations.length > 0) {
        applyCitationSelection(focusCitations[0], true, keywords);
      } else {
        setSelectedCitation(null);
        setWorkspaceTab('graph');
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      reportClientLog({
        level: 'warn',
        message: '问答后图谱自动联动失败',
        source: 'doc_chat',
        event: 'doc_graph_auto_sync',
        context: {
          error: message,
          chunkCount: chunkIds.length,
          entityCount: entityNames.length,
          keywordCount: keywords.length,
        },
      });
      await runDocGraphQuery();
      if (focusCitations.length > 0) {
        applyCitationSelection(focusCitations[0], true, keywords);
      } else {
        setSelectedCitation(null);
        setWorkspaceTab('graph');
      }
    }
  }, [
    addQueryToHistory,
    applyCitationSelection,
    graphSyncMode,
    runDocGraphQuery,
    setGraphData,
    setHighlightAll,
    setLastQueryStats,
    setSelectedCitation,
    setWorkspaceTab,
  ]);

  useEffect(() => {
    if (!lastSyncRef.current) return;
    if (graphSyncMode !== 'full') return;
    const { citations, question, answer } = lastSyncRef.current;
    syncGraphWithCitations(citations, question, answer);
  }, [graphSyncMode, syncGraphWithCitations]);

  const handleBuildGraph = () => {
    if (buildStatus === 'running') return;
    setBuildStatus('running');
    setBuildError(null);
    setBuildSummary(null);
    const targetDocIds = recentUploadedDocIds.filter(Boolean);
    triggerGraphBuild({
      source: targetDocIds.length > 0 ? 'selected_documents' : 'documents',
      docIds: targetDocIds,
      note: targetDocIds.length > 0 ? 'recent_upload_scope' : 'full_library_scope',
    })
      .then((result) => {
        setBuildStatus('done');
        if (targetDocIds.length > 0) {
          setRecentUploadedDocIds([]);
        }
        const jobId = typeof result?.job_id === 'number' ? result.job_id : null;
        setBuildSummary(jobId ? `建图任务 #${jobId} 已提交` : '建图任务已提交');
        setBuildError(result?.message ?? '建图任务已提交，请在任务中心查看进度。');
      })
      .catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        setBuildStatus('idle');
        setBuildError('建图失败，请稍后重试。');
        reportClientLog({
          level: 'error',
          message: '一键建图触发失败',
          source: 'doc_chat',
          event: 'build_graph',
          context: { error: message },
        });
      });
  };

  const buildLabel = useMemo(() => {
    if (buildStatus === 'running') return '正在提交...';
    if (buildStatus === 'done') return '任务已提交';
    return '一键建图';
  }, [buildStatus]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const scrollBox = messageScrollRef.current;
      if (scrollBox) {
        scrollBox.scrollTop = scrollBox.scrollHeight;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, isTyping, showAdvancedOptions, showPromptSuggestions]);

  return (
    <Box
      sx={(theme) => ({
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflow: 'hidden',
        minWidth: 0,
        bgcolor: 'background.paper',
        fontFamily: '"Space Grotesk", "Noto Sans SC", sans-serif',
        borderRight: `1px solid ${theme.palette.divider}`,
      })}
    >
      <Box
        sx={(theme) => ({
          px: 2.25,
          py: 1.25,
          borderBottom: `1px solid ${theme.palette.divider}`,
          background:
            theme.palette.mode === 'dark'
              ? 'linear-gradient(135deg, rgba(15, 23, 42, 0.95) 0%, rgba(30, 41, 59, 0.92) 100%)'
              : 'linear-gradient(135deg, rgba(226, 232, 240, 0.8) 0%, rgba(240, 253, 250, 0.9) 100%)',
          backgroundImage:
            theme.palette.mode === 'dark'
              ? 'radial-gradient(120% 120% at 20% 0%, rgba(56, 189, 248, 0.16) 0%, rgba(15, 23, 42, 0) 55%), radial-gradient(120% 120% at 100% 20%, rgba(34, 197, 94, 0.14) 0%, rgba(15, 23, 42, 0) 45%)'
              : 'radial-gradient(120% 120% at 15% 0%, rgba(56, 189, 248, 0.18) 0%, rgba(248, 250, 252, 0) 55%), radial-gradient(120% 120% at 100% 20%, rgba(34, 197, 94, 0.12) 0%, rgba(248, 250, 252, 0) 45%)',
        })}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1.5 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h6" fontWeight={700} sx={{ letterSpacing: 0.3 }}>
              文档问答
            </Typography>
          </Box>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <Chip
              icon={<MenuBookIcon />}
              label={recentUploadedDocIds.length > 0 ? `最近上传 ${recentUploadedDocIds.length}` : '全库'}
              size="small"
              variant="outlined"
            />
            <Chip
              label={graphSyncMode === 'precise' ? '精准联动' : '全量联动'}
              size="small"
              color={graphSyncMode === 'precise' ? 'primary' : 'default'}
              variant={graphSyncMode === 'precise' ? 'filled' : 'outlined'}
              onClick={() => setGraphSyncMode((prev) => (prev === 'precise' ? 'full' : 'precise'))}
            />
            <Button
              size="small"
              variant="text"
              endIcon={showPromptSuggestions ? <ExpandLessIcon /> : <ExpandMoreIcon />}
              onClick={() => setShowPromptSuggestions((prev) => !prev)}
              sx={{ flexShrink: 0 }}
            >
              提示词
            </Button>
          </Box>
        </Box>

        <Collapse in={showPromptSuggestions} timeout="auto" unmountOnExit>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, mt: 1 }}>
            {quickPrompts.map((prompt) => (
              <Chip
                key={prompt}
                label={prompt}
                size="small"
                onClick={() => handlePromptClick(prompt)}
                sx={{ bgcolor: 'background.paper', maxWidth: '100%' }}
              />
            ))}
          </Box>
        </Collapse>
      </Box>

      <Box
        ref={messageScrollRef}
        onDrop={handleChatDrop}
        onDragOver={(event) => event.preventDefault()}
        sx={(theme) => ({
          flex: 1,
          overflowY: 'auto',
          overflowX: 'hidden',
          px: 2.5,
          pt: 2,
          pb: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 2,
          bgcolor: theme.palette.background.default,
          minWidth: 0,
          backgroundImage:
            theme.palette.mode === 'dark'
              ? 'radial-gradient(circle at top left, rgba(56, 189, 248, 0.08), transparent 60%), radial-gradient(circle at 80% 30%, rgba(34, 197, 94, 0.06), transparent 55%)'
              : 'radial-gradient(circle at top left, rgba(14, 165, 233, 0.08), transparent 60%), radial-gradient(circle at 80% 30%, rgba(34, 197, 94, 0.08), transparent 55%)',
        })}
      >
        {messages.map((message) => {
          const isUser = message.role === 'user';
          return (
            <Box
              key={message.id}
              sx={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: isUser ? 'flex-end' : 'flex-start',
                gap: 1,
              }}
            >
              <Box
                sx={(theme) => ({
                  maxWidth: '85%',
                  minWidth: 0,
                  px: 2,
                  py: 1.5,
                  borderRadius: 2.5,
                  bgcolor: isUser ? theme.palette.primary.main : theme.palette.background.paper,
                  color: isUser ? theme.palette.primary.contrastText : theme.palette.text.primary,
                  border: isUser ? 'none' : `1px solid ${theme.palette.divider}`,
                  boxShadow: isUser ? 'none' : theme.shadows[1],
                })}
              >
                {isUser ? (
                  <Typography variant="body2" component="div" sx={{ lineHeight: 1.6 }}>
                    {message.content}
                  </Typography>
                ) : (
                  <Box
                    sx={{
                      typography: 'body2',
                      lineHeight: 1.6,
                      overflowWrap: 'anywhere',
                      '& p': { my: 0.5 },
                    }}
                  >
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {message.content}
                    </ReactMarkdown>
                  </Box>
                )}
                {message.mode === 'deep_research' && message.summary && (
                  <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 1 }}>
                    摘要：{message.summary}
                  </Typography>
                )}
                {message.mode === 'deep_research' && message.finalConclusion && (
                  <Typography variant="caption" component="div" sx={{ mt: 0.5, fontWeight: 700 }}>
                    结论：{message.finalConclusion}
                  </Typography>
                )}
                {message.mode === 'deep_research' && message.confidence && (
                  <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
                    置信度：{Math.round((message.confidence.score || 0) * 100)}%（{message.confidence.level}）
                    {message.confidence.reason ? ` · ${message.confidence.reason}` : ''}
                  </Typography>
                )}
                {message.mode === 'deep_research' && message.evidenceStats && (
                  <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
                    子问题 {message.evidenceStats.sub_questions} · 检索片段 {message.evidenceStats.retrieved_chunks} · 引用 {message.evidenceStats.unique_citations}
                  </Typography>
                )}
                {message.mode === 'deep_research' && message.subQuestions && message.subQuestions.length > 0 && (
                  <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
                    子问题：{message.subQuestions.join('；')}
                  </Typography>
                )}
              </Box>

              {!isUser && message.citations && message.citations.length > 0 && (
                <Box sx={{ width: '100%', maxWidth: '90%', minWidth: 0, display: 'flex' }}>
                  <Button
                    size="small"
                    variant="text"
                    startIcon={<MenuBookIcon fontSize="small" />}
                    onClick={() => {
                      handleCitationClick(message.citations![0]);
                    }}
                    sx={(theme) => ({
                      alignSelf: 'flex-start',
                      borderRadius: 1,
                      px: 1,
                      minHeight: 28,
                      color: theme.palette.text.secondary,
                      bgcolor: theme.palette.action.hover,
                      '&:hover': {
                        bgcolor: theme.palette.action.selected,
                      },
                    })}
                  >
                    引用 {message.citations.length} 条 · 查看证据
                  </Button>
                </Box>
              )}
            </Box>
          );
        })}

        {isTyping && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <TipsIcon fontSize="small" color="primary" />
            <Typography variant="body2" color="text.secondary">
              {typingLabel}
            </Typography>
          </Box>
        )}
        <Box ref={endRef} sx={{ height: { xs: 132, md: 148 }, flexShrink: 0 }} />
      </Box>

      <Divider />

      <Box sx={{ px: 2, py: 1.25, display: 'flex', flexDirection: 'column', gap: 1, flexShrink: 0 }}>
        {(uploadSummary || uploadError || buildSummary || buildError) && (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75 }}>
            {uploadSummary && (
              <Chip label={uploadSummary} size="small" color="success" variant="outlined" />
            )}
            {uploadError && (
              <Chip label={uploadError} size="small" color="error" variant="outlined" />
            )}
            {buildSummary && (
              <Chip label={buildSummary} size="small" color="success" variant="outlined" />
            )}
            {buildError && (
              <Chip label={buildError} size="small" color="error" variant="outlined" />
            )}
          </Box>
        )}
        <TextField
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          multiline
          minRows={1}
          maxRows={6}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          sx={{
            '& .MuiOutlinedInput-root': {
              borderRadius: 3,
              bgcolor: 'background.default',
              alignItems: 'flex-end',
            },
          }}
          InputProps={{
            startAdornment: (
              <IconButton
                size="small"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                aria-label="上传文档"
                sx={{ mr: 0.5, mb: 0.25 }}
              >
                <AttachFileIcon fontSize="small" />
              </IconButton>
            ),
          }}
        />
        <input ref={fileInputRef} type="file" multiple hidden onChange={handleFileSelect} />
        <Box sx={{ display: 'grid', gap: 1 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1 }}>
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            >
              回答强制引用 · 问答 {qaReasoningProfile} · 调研 {deepResearchReasoningProfile}
            </Typography>
            <Button
              size="small"
              variant="text"
              startIcon={<TuneIcon />}
              endIcon={showAdvancedOptions ? <ExpandLessIcon /> : <ExpandMoreIcon />}
              onClick={() => setShowAdvancedOptions((prev) => !prev)}
              sx={{ flexShrink: 0 }}
            >
              高级
            </Button>
          </Box>

          <Collapse in={showAdvancedOptions} timeout="auto" unmountOnExit>
            <Box
              sx={{
                display: 'grid',
                gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                gap: 1,
                pt: 0.5,
              }}
            >
              <TextField
                select
                size="small"
                label="问答档位"
                value={qaReasoningProfile}
                onChange={(e) => setQaReasoningProfile(e.target.value as ReasoningProfile)}
                sx={{ minWidth: 0 }}
              >
                {REASONING_PROFILE_OPTIONS.map((option) => (
                  <MenuItem key={option.value} value={option.value}>
                    {option.label}
                  </MenuItem>
                ))}
              </TextField>
              <TextField
                select
                size="small"
                label="调研档位"
                value={deepResearchReasoningProfile}
                onChange={(e) => setDeepResearchReasoningProfile(e.target.value as ReasoningProfile)}
                sx={{ minWidth: 0 }}
              >
                {REASONING_PROFILE_OPTIONS.map((option) => (
                  <MenuItem key={option.value} value={option.value}>
                    {option.label}
                  </MenuItem>
                ))}
              </TextField>
            </Box>
          </Collapse>

          <Box sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', alignItems: 'center', gap: 1 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0, flexWrap: 'wrap' }}>
              <LoadingButton
                size="small"
                variant="outlined"
                startIcon={<AttachFileIcon fontSize="small" />}
                loading={uploading}
                disabled={uploading}
                onClick={() => fileInputRef.current?.click()}
                label="上传"
                loadingLabel="上传中"
              />
              <Button
                size="small"
                variant="outlined"
                startIcon={<AutoGraphIcon fontSize="small" />}
                onClick={handleBuildGraph}
                disabled={buildStatus === 'running'}
              >
                {buildLabel}
              </Button>
              <Button
                size="small"
                variant="outlined"
                onClick={handleDeepResearch}
                disabled={!input.trim() || isTyping}
              >
                深度调研
              </Button>
            </Box>
            <IconButton
              color="primary"
              onClick={handleSend}
              disabled={!input.trim() || isTyping}
              sx={{
                borderRadius: 2,
                bgcolor: 'primary.main',
                color: 'primary.contrastText',
                '&:hover': {
                  bgcolor: 'primary.dark',
                },
              }}
            >
              <SendIcon />
            </IconButton>
          </Box>
        </Box>
      </Box>
    </Box>
  );
}
