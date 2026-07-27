/**
 * 配置管理页面 v2.1
 * 使用标准化 API + 受控表单（避免刷新时重置输入）
 */
import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import {
  Box,
  Container,
  Card,
  Typography,
  TextField,
  Button,
  Alert,
  Snackbar,
  Tabs,
  Tab,
  IconButton,
  InputAdornment,
  Chip,
  Stack,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
	} from '@mui/material';
import {
  Visibility,
  VisibilityOff,
  CheckCircle,
  Clear,
  SaveOutlined,
  ScienceOutlined,
  FormatListBulletedOutlined,
  SettingsOutlined,
  PsychologyAltOutlined,
  ManageSearchOutlined,
  StorageOutlined,
  DescriptionOutlined,
	} from '@mui/icons-material';
import { useSearchParams } from 'react-router-dom';
import { configApi } from '../../services/adminService';
import type { ConfigItem, ConfigCategory, ConnectionTestResult, ModelCatalogItem } from '../../types/admin';
import AdminLayout from '../../components/Admin/AdminLayout';
import AdminRefreshButton from '../../components/Admin/AdminRefreshButton';
import AdminLoadingButton from '../../components/Admin/AdminLoadingButton';
import { LoadingState } from '../../components/Loading/AppleSpinner';
import { getErrorMessage } from '../../utils/errorMessage';

const REASONING_PROFILE_OPTIONS = ['fast', 'balanced', 'deep'] as const;
type ModelPickerTarget = 'ai_service' | 'embedding';

interface TabPanelProps {
  children?: React.ReactNode;
  index: number;
  value: number;
}

function TabPanel(props: TabPanelProps) {
  const { children, value, index, ...other } = props;
  return (
    <div hidden={value !== index} {...other}>
      {value === index && <Box sx={{ p: 3 }}>{children}</Box>}
    </div>
  );
}

// 表单值类型
interface FormValues {
  // Neo4j
  neo4j_uri: string;
  neo4j_user: string;
  neo4j_password: string;
  neo4j_database: string;
  // AI Service
  ai_service_provider: string;
  ai_service_enabled: string;
  ai_service_base_url: string;
  ai_service_api_key: string;
  ai_service_model: string;
  ai_service_docqa_reasoning_profile: string;
  ai_service_deep_research_reasoning_profile: string;
  ai_service_graph_extract_reasoning_profile: string;
  ai_service_graph_extract_complex_reasoning_profile: string;
  ai_service_max_tokens: string;
  ai_service_temperature: string;
  // Retrieval / Embedding / Vector Store
  retrieval_mode: string;
  retrieval_rrf_k: string;
  retrieval_candidate_multiplier: string;
  retrieval_graph_enabled: string;
  retrieval_rerank_enabled: string;
  retrieval_rerank_model: string;
  retrieval_rerank_base_url: string;
  retrieval_rerank_endpoint_path: string;
  retrieval_rerank_top_n: string;
  retrieval_rerank_timeout_seconds: string;
  embedding_enabled: string;
  embedding_provider: string;
  embedding_base_url: string;
  embedding_api_key: string;
  embedding_model: string;
  embedding_dimension: string;
  embedding_batch_size: string;
  vector_store_enabled: string;
  vector_store_provider: string;
  vector_store_uri: string;
  vector_store_db_name: string;
  vector_store_collection: string;
  vector_store_token: string;
  document_parser_provider: string;
  document_parser_fallback_provider: string;
  document_parser_base_url: string;
  document_parser_endpoint_path: string;
  document_parser_file_field: string;
  document_parser_parse_mode: string;
  document_parser_output_format: string;
  document_parser_timeout_seconds: string;
  // NL2Cypher
  nl2cypher_enabled: string;
  nl2cypher_cache_size: string;
  nl2cypher_max_limit: string;
}

const NEO4J_FIELD_CONFIGS: Array<{ field: keyof FormValues; key: string }> = [
  { field: 'neo4j_uri', key: 'uri' },
  { field: 'neo4j_user', key: 'user' },
  { field: 'neo4j_password', key: 'password' },
  { field: 'neo4j_database', key: 'database' },
];

const NEO4J_FIELD_NAMES = new Set<string>(NEO4J_FIELD_CONFIGS.map((item) => item.field));

const AI_SERVICE_FIELD_CONFIGS: Array<{ field: keyof FormValues; key: string }> = [
  { field: 'ai_service_provider', key: 'provider' },
  { field: 'ai_service_enabled', key: 'enabled' },
  { field: 'ai_service_base_url', key: 'base_url' },
  { field: 'ai_service_api_key', key: 'api_key' },
  { field: 'ai_service_model', key: 'model' },
  { field: 'ai_service_docqa_reasoning_profile', key: 'docqa_reasoning_profile' },
  { field: 'ai_service_deep_research_reasoning_profile', key: 'deep_research_reasoning_profile' },
  { field: 'ai_service_graph_extract_reasoning_profile', key: 'graph_extract_reasoning_profile' },
  { field: 'ai_service_graph_extract_complex_reasoning_profile', key: 'graph_extract_complex_reasoning_profile' },
  { field: 'ai_service_max_tokens', key: 'max_tokens' },
  { field: 'ai_service_temperature', key: 'temperature' },
];

const RETRIEVAL_FIELD_CONFIGS: Array<{ field: keyof FormValues; category: ConfigCategory; key: string }> = [
  { field: 'retrieval_mode', category: 'retrieval', key: 'mode' },
  { field: 'retrieval_rrf_k', category: 'retrieval', key: 'rrf_k' },
  { field: 'retrieval_candidate_multiplier', category: 'retrieval', key: 'candidate_multiplier' },
  { field: 'retrieval_graph_enabled', category: 'retrieval', key: 'graph_enabled' },
  { field: 'retrieval_rerank_enabled', category: 'retrieval', key: 'rerank_enabled' },
  { field: 'retrieval_rerank_model', category: 'retrieval', key: 'rerank_model' },
  { field: 'retrieval_rerank_base_url', category: 'retrieval', key: 'rerank_base_url' },
  { field: 'retrieval_rerank_endpoint_path', category: 'retrieval', key: 'rerank_endpoint_path' },
  { field: 'retrieval_rerank_top_n', category: 'retrieval', key: 'rerank_top_n' },
  { field: 'retrieval_rerank_timeout_seconds', category: 'retrieval', key: 'rerank_timeout_seconds' },
  { field: 'embedding_enabled', category: 'embedding', key: 'enabled' },
  { field: 'embedding_provider', category: 'embedding', key: 'provider' },
  { field: 'embedding_base_url', category: 'embedding', key: 'base_url' },
  { field: 'embedding_api_key', category: 'embedding', key: 'api_key' },
  { field: 'embedding_model', category: 'embedding', key: 'model' },
  { field: 'embedding_dimension', category: 'embedding', key: 'dimension' },
  { field: 'embedding_batch_size', category: 'embedding', key: 'batch_size' },
  { field: 'vector_store_enabled', category: 'vector_store', key: 'enabled' },
  { field: 'vector_store_provider', category: 'vector_store', key: 'provider' },
  { field: 'vector_store_uri', category: 'vector_store', key: 'uri' },
  { field: 'vector_store_db_name', category: 'vector_store', key: 'db_name' },
  { field: 'vector_store_collection', category: 'vector_store', key: 'collection' },
  { field: 'vector_store_token', category: 'vector_store', key: 'token' },
];

const DOCUMENT_PARSER_FIELD_CONFIGS: Array<{ field: keyof FormValues; category: ConfigCategory; key: string }> = [
  { field: 'document_parser_provider', category: 'document_parser', key: 'provider' },
  { field: 'document_parser_fallback_provider', category: 'document_parser', key: 'fallback_provider' },
  { field: 'document_parser_base_url', category: 'document_parser', key: 'base_url' },
  { field: 'document_parser_endpoint_path', category: 'document_parser', key: 'endpoint_path' },
  { field: 'document_parser_file_field', category: 'document_parser', key: 'file_field' },
  { field: 'document_parser_parse_mode', category: 'document_parser', key: 'parse_mode' },
  { field: 'document_parser_output_format', category: 'document_parser', key: 'output_format' },
  { field: 'document_parser_timeout_seconds', category: 'document_parser', key: 'timeout_seconds' },
];

const mergeModelOptions = (...groups: Array<Array<string | null | undefined>>) => {
  const seen = new Set<string>();
  const merged: string[] = [];
  groups.flat().forEach((item) => {
    const value = String(item || '').trim();
    if (!value) return;
    const key = value.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    merged.push(value);
  });
  return merged;
};

const maskSecretPreview = (value: string) => {
  const trimmed = value.trim();
  if (!trimmed) return '';
  if (trimmed.length <= 8) return '已配置';
  return `${trimmed.slice(0, 3)}...${trimmed.slice(-4)}`;
};

const configValueAsBool = (item?: ConfigItem) => {
  const value = String(item?.value ?? '').trim().toLowerCase();
  return value === 'true' || value === '1' || value === 'yes';
};

const getErrorDebugInfo = (reason: unknown) => {
  if (!reason || typeof reason !== 'object') {
    return { message: getErrorMessage(reason, '未知错误') };
  }
  const error = reason as {
    message?: unknown;
    code?: unknown;
    trace_id?: unknown;
    details?: unknown;
  };
  return {
    message: typeof error.message === 'string' ? error.message : getErrorMessage(reason, '未知错误'),
    code: error.code,
    trace_id: error.trace_id,
    details: error.details,
  };
};

const buildFormValuesFromConfigs = (configs: Record<ConfigCategory, Record<string, ConfigItem>>): FormValues => {
  const aiServiceBaseUrl = configs.ai_service?.base_url?.value || '';
  const embeddingBaseUrl = configs.embedding?.base_url?.value || '';
  return {
    neo4j_uri: configs.neo4j?.uri?.value || '',
    neo4j_user: configs.neo4j?.user?.value || '',
    neo4j_password: '',
    neo4j_database: configs.neo4j?.database?.value || 'neo4j',
    ai_service_provider: configs.ai_service?.provider?.value || 'openai',
    ai_service_enabled: configs.ai_service?.enabled?.value || 'true',
    ai_service_base_url: aiServiceBaseUrl,
    ai_service_api_key: '',
    ai_service_model: configs.ai_service?.model?.value || '',
    ai_service_docqa_reasoning_profile: configs.ai_service?.docqa_reasoning_profile?.value || 'balanced',
    ai_service_deep_research_reasoning_profile: configs.ai_service?.deep_research_reasoning_profile?.value || 'deep',
    ai_service_graph_extract_reasoning_profile: configs.ai_service?.graph_extract_reasoning_profile?.value || 'fast',
    ai_service_graph_extract_complex_reasoning_profile: configs.ai_service?.graph_extract_complex_reasoning_profile?.value || 'balanced',
    ai_service_max_tokens: configs.ai_service?.max_tokens?.value || '',
    ai_service_temperature: configs.ai_service?.temperature?.value || '',
    retrieval_mode: configs.retrieval?.mode?.value || 'keyword',
    retrieval_rrf_k: configs.retrieval?.rrf_k?.value || '60',
    retrieval_candidate_multiplier: configs.retrieval?.candidate_multiplier?.value || '6',
    retrieval_graph_enabled: configs.retrieval?.graph_enabled?.value || 'true',
    retrieval_rerank_enabled: configs.retrieval?.rerank_enabled?.value || 'false',
    retrieval_rerank_model: configs.retrieval?.rerank_model?.value || '',
    retrieval_rerank_base_url: configs.retrieval?.rerank_base_url?.value || '',
    retrieval_rerank_endpoint_path: configs.retrieval?.rerank_endpoint_path?.value || '/rerank',
    retrieval_rerank_top_n: configs.retrieval?.rerank_top_n?.value || '20',
    retrieval_rerank_timeout_seconds: configs.retrieval?.rerank_timeout_seconds?.value || '15',
    embedding_enabled: configs.embedding?.enabled?.value || 'true',
    embedding_provider: configs.embedding?.provider?.value || configs.ai_service?.provider?.value || 'openai',
    embedding_base_url: embeddingBaseUrl === aiServiceBaseUrl ? '' : embeddingBaseUrl,
    embedding_api_key: '',
    embedding_model: configs.embedding?.model?.value || 'text-embedding-3-small',
    embedding_dimension: configs.embedding?.dimension?.value || '1536',
    embedding_batch_size: configs.embedding?.batch_size?.value || '32',
    vector_store_enabled: configs.vector_store?.enabled?.value || 'false',
    vector_store_provider: configs.vector_store?.provider?.value || 'milvus',
    vector_store_uri: configs.vector_store?.uri?.value || 'http://127.0.0.1:19530',
    vector_store_db_name: configs.vector_store?.db_name?.value || 'default',
    vector_store_collection: configs.vector_store?.collection?.value || 'graphinsight_chunks',
    vector_store_token: '',
    document_parser_provider: configs.document_parser?.provider?.value || 'native',
    document_parser_fallback_provider: configs.document_parser?.fallback_provider?.value || 'native',
    document_parser_base_url: configs.document_parser?.base_url?.value || '',
    document_parser_endpoint_path: configs.document_parser?.endpoint_path?.value || '/file_parse',
    document_parser_file_field: configs.document_parser?.file_field?.value || 'files',
    document_parser_parse_mode: configs.document_parser?.parse_mode?.value || 'auto',
    document_parser_output_format: configs.document_parser?.output_format?.value || 'markdown,json',
    document_parser_timeout_seconds: configs.document_parser?.timeout_seconds?.value || '300',
    nl2cypher_enabled: configs.nl2cypher?.enabled?.value || 'true',
    nl2cypher_cache_size: configs.nl2cypher?.cache_size?.value || '',
    nl2cypher_max_limit: configs.nl2cypher?.max_limit?.value || '',
  };
};

const ConfigPage: React.FC = () => {
  const [searchParams] = useSearchParams();
  const initialTab = parseInt(searchParams.get('tab') || '0', 10);
  const [tabValue, setTabValue] = useState(initialTab);
  const [configs, setConfigs] = useState<Record<ConfigCategory, Record<string, ConfigItem>> | null>(null);
  const [showPassword, setShowPassword] = useState<{ [key: string]: boolean }>({});
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalogItem[]>([]);
  const [modelSource, setModelSource] = useState('');
  const [modelSourceMessage, setModelSourceMessage] = useState('');
  const [neo4jPasswordConfigured, setNeo4jPasswordConfigured] = useState(false);
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false);
  const [apiKeyPreview, setApiKeyPreview] = useState('');
  const [editingApiKey, setEditingApiKey] = useState(false);
  const [embeddingApiKeyConfigured, setEmbeddingApiKeyConfigured] = useState(false);
  const [embeddingApiKeyPreview, setEmbeddingApiKeyPreview] = useState('');
  const [editingEmbeddingApiKey, setEditingEmbeddingApiKey] = useState(false);
  const [vectorStoreTokenConfigured, setVectorStoreTokenConfigured] = useState(false);
  const [vectorStoreTokenPreview, setVectorStoreTokenPreview] = useState('');
  const [editingVectorStoreToken, setEditingVectorStoreToken] = useState(false);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelPickerTarget, setModelPickerTarget] = useState<ModelPickerTarget>('ai_service');
  const [loadingModels, setLoadingModels] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testingNeo4j, setTestingNeo4j] = useState(false);
  const [modelTestResult, setModelTestResult] = useState<ConnectionTestResult | null>(null);
  const [testingModel, setTestingModel] = useState(false);
  const [testingEmbedding, setTestingEmbedding] = useState(false);
  const [testingVectorStore, setTestingVectorStore] = useState(false);
  const [vectorStoreTestResult, setVectorStoreTestResult] = useState<ConnectionTestResult | null>(null);
  const [testingDocumentParser, setTestingDocumentParser] = useState(false);
  
  // 使用受控表单值，避免刷新时重置
  const [formValues, setFormValues] = useState<FormValues>({
    neo4j_uri: '',
    neo4j_user: '',
    neo4j_password: '',
    neo4j_database: 'neo4j',
    ai_service_provider: 'openai',
    ai_service_enabled: 'true',
    ai_service_base_url: '',
    ai_service_api_key: '',
    ai_service_model: '',
    ai_service_docqa_reasoning_profile: 'balanced',
    ai_service_deep_research_reasoning_profile: 'deep',
    ai_service_graph_extract_reasoning_profile: 'fast',
    ai_service_graph_extract_complex_reasoning_profile: 'balanced',
    ai_service_max_tokens: '',
    ai_service_temperature: '',
    retrieval_mode: 'keyword',
    retrieval_rrf_k: '60',
    retrieval_candidate_multiplier: '6',
    retrieval_graph_enabled: 'true',
    retrieval_rerank_enabled: 'false',
    retrieval_rerank_model: '',
    retrieval_rerank_base_url: '',
    retrieval_rerank_endpoint_path: '/rerank',
    retrieval_rerank_top_n: '20',
    retrieval_rerank_timeout_seconds: '15',
    embedding_enabled: 'true',
    embedding_provider: 'openai',
    embedding_base_url: '',
    embedding_api_key: '',
    embedding_model: 'text-embedding-3-small',
    embedding_dimension: '1536',
    embedding_batch_size: '32',
    vector_store_enabled: 'false',
    vector_store_provider: 'milvus',
    vector_store_uri: 'http://127.0.0.1:19530',
    vector_store_db_name: 'default',
    vector_store_collection: 'graphinsight_chunks',
    vector_store_token: '',
    document_parser_provider: 'native',
    document_parser_fallback_provider: 'native',
    document_parser_base_url: '',
    document_parser_endpoint_path: '/file_parse',
    document_parser_file_field: 'files',
    document_parser_parse_mode: 'auto',
    document_parser_output_format: 'markdown,json',
    document_parser_timeout_seconds: '300',
    nl2cypher_enabled: 'true',
    nl2cypher_cache_size: '',
    nl2cypher_max_limit: '',
  });
  
  // 跟踪哪些字段被修改过
  const [dirtyFields, setDirtyFields] = useState<Set<string>>(new Set());
  const initialLoadDone = useRef(false);
  const neo4jDirtyCount = useMemo(
    () => NEO4J_FIELD_CONFIGS.filter((item) => dirtyFields.has(item.field)).length,
    [dirtyFields]
  );
  const aiDirtyCount = useMemo(
    () => [...AI_SERVICE_FIELD_CONFIGS, ...RETRIEVAL_FIELD_CONFIGS, ...DOCUMENT_PARSER_FIELD_CONFIGS].filter((item) => dirtyFields.has(item.field)).length,
    [dirtyFields]
  );
  const manualSaveDirtyFields = useMemo(
    () => Array.from(dirtyFields).filter((field) => !NEO4J_FIELD_NAMES.has(field)),
    [dirtyFields]
  );
  const manualSaveDirtyCount = manualSaveDirtyFields.length;
  const currentPickerModel = modelPickerTarget === 'embedding' ? formValues.embedding_model : formValues.ai_service_model;
  const modelOptions = useMemo(
    () => mergeModelOptions([currentPickerModel], availableModels),
    [availableModels, currentPickerModel]
  );
  const modelCatalogByKey = useMemo(() => {
    const items = new Map<string, ModelCatalogItem>();
    modelCatalog.forEach((item) => {
      items.set(item.model.toLowerCase(), item);
    });
    return items;
  }, [modelCatalog]);
  const selectedModelKey = currentPickerModel.trim().toLowerCase();
  const apiKeyDisplayValue = editingApiKey ? formValues.ai_service_api_key : apiKeyPreview;
  const embeddingApiKeyDisplayValue = editingEmbeddingApiKey ? formValues.embedding_api_key : embeddingApiKeyPreview;
  const vectorStoreTokenDisplayValue = editingVectorStoreToken ? formValues.vector_store_token : vectorStoreTokenPreview;

  useEffect(() => {
    loadConfigs();
  }, []);

  useEffect(() => {
    const nextTab = parseInt(searchParams.get('tab') || '0', 10);
    if (!Number.isNaN(nextTab) && nextTab >= 0 && nextTab <= 2) {
      setTabValue(nextTab);
    }
  }, [searchParams]);

  // 当配置加载完成后，初始化表单值（仅首次加载）
  useEffect(() => {
    if (configs && !initialLoadDone.current) {
      initialLoadDone.current = true;
      setNeo4jPasswordConfigured(configValueAsBool(configs.neo4j?.password_configured));
      setApiKeyConfigured(configValueAsBool(configs.ai_service?.api_key_configured));
      setApiKeyPreview(configs.ai_service?.api_key_preview?.value || '');
      setEmbeddingApiKeyConfigured(configValueAsBool(configs.embedding?.api_key_configured) || Boolean(configs.embedding?.api_key?.is_sensitive));
      setEmbeddingApiKeyPreview(configs.embedding?.api_key_preview?.value || (configs.embedding?.api_key?.is_sensitive ? '已配置' : ''));
      setVectorStoreTokenConfigured(configValueAsBool(configs.vector_store?.token_configured) || Boolean(configs.vector_store?.token?.is_sensitive));
      setVectorStoreTokenPreview(configs.vector_store?.token_preview?.value || (configs.vector_store?.token?.is_sensitive ? '已配置' : ''));
      setFormValues(buildFormValuesFromConfigs(configs));
    }
  }, [configs]);

  const loadConfigs = async (options?: { showLoading?: boolean }) => {
    const showLoading = options?.showLoading ?? true;
    try {
      if (showLoading) {
        setLoading(true);
      }
      const data = await configApi.getAll();
      setConfigs(data);
      try {
        const latestModelTest = await configApi.getLatestModelConnectionTest();
        setModelTestResult(latestModelTest);
      } catch (latestErr) {
        console.warn('Load latest model test skipped:', latestErr);
      }
      setError('');
    } catch (err: unknown) {
      console.error('Load configs error:', err);
      setError(getErrorMessage(err, '加载配置失败'));
    } finally {
      if (showLoading) {
        setLoading(false);
      }
    }
  };

  // 更新表单值
  const handleFormChange = useCallback((field: keyof FormValues, value: string) => {
    setFormValues(prev => ({ ...prev, [field]: value }));
    setDirtyFields(prev => new Set(prev).add(field));
    if (field.startsWith('vector_store_')) {
      setVectorStoreTestResult(null);
    }
  }, []);

  const handleSelectModel = useCallback((model: string) => {
    if (modelPickerTarget === 'embedding') {
      handleFormChange('embedding_model', model);
    } else {
      handleFormChange('ai_service_model', model);
      const item = modelCatalogByKey.get(model.toLowerCase());
      const suggestedMaxTokens = item?.max_output_tokens || item?.suggested_max_tokens || 0;
      if (!formValues.ai_service_max_tokens.trim() && suggestedMaxTokens > 0) {
        handleFormChange('ai_service_max_tokens', String(suggestedMaxTokens));
      }
    }
    setModelPickerOpen(false);
  }, [formValues.ai_service_max_tokens, handleFormChange, modelCatalogByKey, modelPickerTarget]);

  const scrollToConfigSection = useCallback((id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  // 保存单个配置（失焦时触发）
  const handleSaveField = useCallback(async (category: ConfigCategory, key: string, field: keyof FormValues) => {
    // 只有修改过的字段才保存
    if (!dirtyFields.has(field)) return;
    if (category === 'neo4j') return;
    
    const value = formValues[field];
    try {
      setSaving(true);
      await configApi.update(category, key, value);
      if (category === 'ai_service' && key === 'api_key') {
        const preview = maskSecretPreview(value);
        setApiKeyConfigured(Boolean(preview));
        setApiKeyPreview(preview);
        setEditingApiKey(false);
        setFormValues(prev => ({ ...prev, ai_service_api_key: '' }));
      }
      if (category === 'embedding' && key === 'api_key') {
        const preview = maskSecretPreview(value);
        setEmbeddingApiKeyConfigured(Boolean(preview));
        setEmbeddingApiKeyPreview(preview);
        setEditingEmbeddingApiKey(false);
        setFormValues(prev => ({ ...prev, embedding_api_key: '' }));
      }
      if (category === 'vector_store' && key === 'token') {
        const preview = maskSecretPreview(value);
        setVectorStoreTokenConfigured(Boolean(preview));
        setVectorStoreTokenPreview(preview);
        setEditingVectorStoreToken(false);
        setFormValues(prev => ({ ...prev, vector_store_token: '' }));
      }
      setDirtyFields(prev => {
        const newSet = new Set(prev);
        newSet.delete(field);
        return newSet;
      });
      setMessage('配置已保存');
    } catch (err: unknown) {
      console.error('Save config error:', getErrorDebugInfo(err));
      setError(getErrorMessage(err, '保存失败'));
    } finally {
      setSaving(false);
    }
  }, [formValues, dirtyFields]);

  // 保存所有修改过的字段
  const handleSaveAll = async () => {
    if (manualSaveDirtyCount === 0) {
      setMessage('没有需要保存的更改');
      return;
    }

    try {
      setSaving(true);
      const fieldToConfig: Record<string, { category: ConfigCategory; key: string }> = {
        ai_service_provider: { category: 'ai_service' as ConfigCategory, key: 'provider' },
        ai_service_enabled: { category: 'ai_service' as ConfigCategory, key: 'enabled' },
        ai_service_base_url: { category: 'ai_service' as ConfigCategory, key: 'base_url' },
        ai_service_api_key: { category: 'ai_service' as ConfigCategory, key: 'api_key' },
        ai_service_model: { category: 'ai_service' as ConfigCategory, key: 'model' },
        ai_service_docqa_reasoning_profile: { category: 'ai_service' as ConfigCategory, key: 'docqa_reasoning_profile' },
        ai_service_deep_research_reasoning_profile: { category: 'ai_service' as ConfigCategory, key: 'deep_research_reasoning_profile' },
        ai_service_graph_extract_reasoning_profile: { category: 'ai_service' as ConfigCategory, key: 'graph_extract_reasoning_profile' },
        ai_service_graph_extract_complex_reasoning_profile: { category: 'ai_service' as ConfigCategory, key: 'graph_extract_complex_reasoning_profile' },
        ai_service_max_tokens: { category: 'ai_service' as ConfigCategory, key: 'max_tokens' },
        ai_service_temperature: { category: 'ai_service' as ConfigCategory, key: 'temperature' },
        retrieval_mode: { category: 'retrieval', key: 'mode' },
        retrieval_rrf_k: { category: 'retrieval', key: 'rrf_k' },
        retrieval_candidate_multiplier: { category: 'retrieval', key: 'candidate_multiplier' },
        retrieval_graph_enabled: { category: 'retrieval', key: 'graph_enabled' },
        retrieval_rerank_enabled: { category: 'retrieval', key: 'rerank_enabled' },
        retrieval_rerank_model: { category: 'retrieval', key: 'rerank_model' },
        retrieval_rerank_base_url: { category: 'retrieval', key: 'rerank_base_url' },
        retrieval_rerank_endpoint_path: { category: 'retrieval', key: 'rerank_endpoint_path' },
        retrieval_rerank_top_n: { category: 'retrieval', key: 'rerank_top_n' },
        retrieval_rerank_timeout_seconds: { category: 'retrieval', key: 'rerank_timeout_seconds' },
        embedding_enabled: { category: 'embedding', key: 'enabled' },
        embedding_provider: { category: 'embedding', key: 'provider' },
        embedding_base_url: { category: 'embedding', key: 'base_url' },
        embedding_api_key: { category: 'embedding', key: 'api_key' },
        embedding_model: { category: 'embedding', key: 'model' },
        embedding_dimension: { category: 'embedding', key: 'dimension' },
        embedding_batch_size: { category: 'embedding', key: 'batch_size' },
        vector_store_enabled: { category: 'vector_store', key: 'enabled' },
        vector_store_provider: { category: 'vector_store', key: 'provider' },
        vector_store_uri: { category: 'vector_store', key: 'uri' },
        vector_store_db_name: { category: 'vector_store', key: 'db_name' },
        vector_store_collection: { category: 'vector_store', key: 'collection' },
        vector_store_token: { category: 'vector_store', key: 'token' },
        document_parser_provider: { category: 'document_parser', key: 'provider' },
        document_parser_fallback_provider: { category: 'document_parser', key: 'fallback_provider' },
        document_parser_base_url: { category: 'document_parser', key: 'base_url' },
        document_parser_endpoint_path: { category: 'document_parser', key: 'endpoint_path' },
        document_parser_file_field: { category: 'document_parser', key: 'file_field' },
        document_parser_parse_mode: { category: 'document_parser', key: 'parse_mode' },
        document_parser_output_format: { category: 'document_parser', key: 'output_format' },
        document_parser_timeout_seconds: { category: 'document_parser', key: 'timeout_seconds' },
        nl2cypher_enabled: { category: 'nl2cypher', key: 'enabled' },
        nl2cypher_cache_size: { category: 'nl2cypher', key: 'cache_size' },
        nl2cypher_max_limit: { category: 'nl2cypher', key: 'max_limit' },
      };

      let savedCount = 0;
      const savedFields = new Set<string>();
      for (const field of manualSaveDirtyFields) {
        const config = fieldToConfig[field];
        if (config) {
          await configApi.update(config.category, config.key, formValues[field as keyof FormValues]);
          if (field === 'ai_service_api_key') {
            const preview = maskSecretPreview(formValues.ai_service_api_key);
            setApiKeyConfigured(Boolean(preview));
            setApiKeyPreview(preview);
            setEditingApiKey(false);
            setFormValues(prev => ({ ...prev, ai_service_api_key: '' }));
          }
          if (field === 'embedding_api_key') {
            const preview = maskSecretPreview(formValues.embedding_api_key);
            setEmbeddingApiKeyConfigured(Boolean(preview));
            setEmbeddingApiKeyPreview(preview);
            setEditingEmbeddingApiKey(false);
            setFormValues(prev => ({ ...prev, embedding_api_key: '' }));
          }
          if (field === 'vector_store_token') {
            const preview = maskSecretPreview(formValues.vector_store_token);
            setVectorStoreTokenConfigured(Boolean(preview));
            setVectorStoreTokenPreview(preview);
            setEditingVectorStoreToken(false);
            setFormValues(prev => ({ ...prev, vector_store_token: '' }));
          }
          savedFields.add(field);
          savedCount++;
        }
      }

      setDirtyFields(prev => {
        const next = new Set(prev);
        savedFields.forEach((field) => next.delete(field));
        return next;
      });
      setMessage(`成功保存 ${savedCount} 项配置`);
    } catch (err: unknown) {
      console.error('Save all error:', err);
      setError(getErrorMessage(err, '保存失败'));
    } finally {
      setSaving(false);
    }
  };

  // 重置表单到服务器值
  const handleReset = () => {
    if (configs) {
      setNeo4jPasswordConfigured(configValueAsBool(configs.neo4j?.password_configured));
      setApiKeyConfigured(configValueAsBool(configs.ai_service?.api_key_configured));
      setApiKeyPreview(configs.ai_service?.api_key_preview?.value || '');
      setEmbeddingApiKeyConfigured(configValueAsBool(configs.embedding?.api_key_configured) || Boolean(configs.embedding?.api_key?.is_sensitive));
      setEmbeddingApiKeyPreview(configs.embedding?.api_key_preview?.value || (configs.embedding?.api_key?.is_sensitive ? '已配置' : ''));
      setEditingEmbeddingApiKey(false);
      setVectorStoreTokenConfigured(configValueAsBool(configs.vector_store?.token_configured) || Boolean(configs.vector_store?.token?.is_sensitive));
      setVectorStoreTokenPreview(configs.vector_store?.token_preview?.value || (configs.vector_store?.token?.is_sensitive ? '已配置' : ''));
      setEditingVectorStoreToken(false);
      setFormValues(buildFormValuesFromConfigs(configs));
    }
    setDirtyFields(new Set());
    setMessage('已重置所有未保存的更改');
  };

  const handleTest = async (type: 'neo4j' | 'ai_service') => {
    try {
      if (type === 'neo4j') {
        setTestingNeo4j(true);
        const result = await configApi.testConnection('neo4j', {
          uri: formValues.neo4j_uri,
          user: formValues.neo4j_user,
          database: formValues.neo4j_database,
          password: formValues.neo4j_password.trim() ? formValues.neo4j_password : undefined,
        });
        if (result.success) {
          if (formValues.neo4j_password.trim()) {
            setNeo4jPasswordConfigured(true);
          }
          setFormValues(prev => ({ ...prev, neo4j_password: '' }));
          setDirtyFields(prev => {
            const next = new Set(prev);
            NEO4J_FIELD_CONFIGS.forEach((item) => next.delete(item.field));
            return next;
          });
          await loadConfigs({ showLoading: false });
          setMessage(result.message || 'Neo4j 连接成功，配置已保存');
        } else {
          setError(result.message || 'Neo4j 连接测试失败，配置未保存');
        }
        return;
      }
      if (type === 'ai_service') {
        await saveDirtyAiServiceFields();
      }
      const result = await configApi.testConnection(type);
      if (result.success) {
        setMessage(result.message || `${type === 'ai_service' ? 'AI 服务' : type} 连接测试成功`);
      } else {
        setError(result.message || `${type === 'ai_service' ? 'AI 服务' : type} 连接测试失败`);
      }
    } catch (err: unknown) {
      console.error('Test connection error:', err);
      setError(getErrorMessage(err, '测试失败'));
    } finally {
      if (type === 'neo4j') {
        setTestingNeo4j(false);
      }
    }
  };

  const togglePasswordVisibility = (key: string) => {
    setShowPassword((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const saveDirtyAiServiceFields = async () => {
    const savedFields = new Set<string>();
    for (const item of [
      ...AI_SERVICE_FIELD_CONFIGS.map((config) => ({ ...config, category: 'ai_service' as ConfigCategory })),
      ...RETRIEVAL_FIELD_CONFIGS,
      ...DOCUMENT_PARSER_FIELD_CONFIGS,
    ]) {
      if (!dirtyFields.has(item.field)) continue;
      await configApi.update(item.category, item.key, formValues[item.field]);
      if (item.field === 'ai_service_api_key') {
        const preview = maskSecretPreview(formValues.ai_service_api_key);
        setApiKeyConfigured(Boolean(preview));
        setApiKeyPreview(preview);
        setEditingApiKey(false);
        setFormValues(prev => ({ ...prev, ai_service_api_key: '' }));
      }
      if (item.field === 'embedding_api_key') {
        const preview = maskSecretPreview(formValues.embedding_api_key);
        setEmbeddingApiKeyConfigured(Boolean(preview));
        setEmbeddingApiKeyPreview(preview);
        setEditingEmbeddingApiKey(false);
        setFormValues(prev => ({ ...prev, embedding_api_key: '' }));
      }
      if (item.field === 'vector_store_token') {
        const preview = maskSecretPreview(formValues.vector_store_token);
        setVectorStoreTokenConfigured(Boolean(preview));
        setVectorStoreTokenPreview(preview);
        setEditingVectorStoreToken(false);
        setFormValues(prev => ({ ...prev, vector_store_token: '' }));
      }
      savedFields.add(item.field);
    }
    if (savedFields.size > 0) {
      setDirtyFields(prev => {
        const next = new Set(prev);
        savedFields.forEach((field) => next.delete(field));
        return next;
      });
    }
    return savedFields.size;
  };

  const handleSaveAiService = async () => {
    try {
      setSaving(true);
      const savedCount = await saveDirtyAiServiceFields();
      setMessage(savedCount > 0 ? `模型与检索配置已保存 ${savedCount} 项` : '模型与检索配置没有需要保存的更改');
    } catch (err: unknown) {
      console.error('Save AI service config error:', getErrorDebugInfo(err));
      setError(getErrorMessage(err, '保存模型与检索配置失败'));
    } finally {
      setSaving(false);
    }
  };

  const handleEmbeddingConnectionTest = async () => {
    setTestingEmbedding(true);
    setError('');
    try {
      await saveDirtyAiServiceFields();
      const result = await configApi.testConnection('embedding');
      if (result.success) {
        setMessage(result.message || '嵌入模型连通性测试成功');
      } else {
        setError(result.message || '嵌入模型连通性测试失败');
      }
    } catch (err: unknown) {
      console.error('Embedding connection test error:', err);
      setError(getErrorMessage(err, '嵌入模型连通性测试失败'));
    } finally {
      setTestingEmbedding(false);
    }
  };

  const handleVectorStoreConnectionTest = async () => {
    setTestingVectorStore(true);
    setError('');
    try {
      await saveDirtyAiServiceFields();
      const result = await configApi.testConnection('vector_store');
      setVectorStoreTestResult(result);
      if (result.success) {
        setMessage(result.message || 'Milvus 连通性测试成功');
      } else {
        setError(result.message || 'Milvus 连通性测试失败');
      }
    } catch (err: unknown) {
      console.error('Vector store connection test error:', err);
      setError(getErrorMessage(err, 'Milvus 连通性测试失败'));
    } finally {
      setTestingVectorStore(false);
    }
  };

  const handleDocumentParserConnectionTest = async () => {
    setTestingDocumentParser(true);
    setError('');
    try {
      await saveDirtyAiServiceFields();
      const result = await configApi.testConnection('document_parser');
      if (result.success) {
        setMessage(result.message || '文档解析器连通性测试成功');
      } else {
        setError(result.message || '文档解析器连通性测试失败');
      }
    } catch (err: unknown) {
      console.error('Document parser connection test error:', err);
      setError(getErrorMessage(err, '文档解析器连通性测试失败'));
    } finally {
      setTestingDocumentParser(false);
    }
  };

  const handleModelConnectionTest = async () => {
    setTestingModel(true);
    setError('');
    try {
      await saveDirtyAiServiceFields();
      const result = await configApi.testConnection('model');
      setModelTestResult(result);
      if (result.success) {
        setMessage(result.message || '模型连通性测试成功');
      } else {
        setError(result.message || '模型连通性测试失败');
      }
    } catch (err: unknown) {
      console.error('Model connection test error:', err);
      setError(getErrorMessage(err, '模型连通性测试失败'));
    } finally {
      setTestingModel(false);
    }
  };

  const handleFetchModels = async (target: ModelPickerTarget = 'ai_service') => {
    setLoadingModels(true);
    setModelPickerTarget(target);
    try {
      // 先落库 AI 配置（尤其 API Key），避免 onBlur 未触发时读到旧值
      await saveDirtyAiServiceFields();

      const modelResponse = await configApi.getAvailableModels({
        provider: target === 'embedding' ? formValues.embedding_provider || formValues.ai_service_provider : formValues.ai_service_provider,
        base_url: target === 'embedding' ? formValues.embedding_base_url || formValues.ai_service_base_url : formValues.ai_service_base_url,
        model: target === 'embedding' ? formValues.embedding_model : formValues.ai_service_model,
        purpose: target === 'embedding' ? 'embedding' : 'chat',
      });
      const models = modelResponse.models || [];
      setAvailableModels(models);
      setModelCatalog(Array.isArray(modelResponse.catalog) ? modelResponse.catalog : []);
      setModelSource(modelResponse.source || '');
      setModelSourceMessage(modelResponse.source_message || '');
      if (models.length > 0) {
        setModelPickerOpen(true);
        const sourceLabel = modelResponse.source === 'remote' ? '服务商' : modelResponse.source === 'configured' ? '配置' : '当前配置';
        setMessage(`成功获取 ${models.length} 个${target === 'embedding' ? '嵌入' : '问答'}模型候选（来源：${sourceLabel}）`);
        if (target === 'ai_service' && !formValues.ai_service_model) {
          handleFormChange('ai_service_model', models[0]);
        } else if (target === 'embedding' && !formValues.embedding_model) {
          handleFormChange('embedding_model', models[0]);
        }
      } else {
        setError('未获取到模型列表，请检查 AI 服务网关配置');
      }
    } catch (err: unknown) {
      console.error('Fetch models error:', getErrorDebugInfo(err));
      setError(getErrorMessage(err, '获取模型列表失败'));
    } finally {
      setLoadingModels(false);
    }
  };

  if (loading) {
    return (
      <AdminLayout title="配置中心" subtitle="统一管理连接、模型与系统能力">
        <LoadingState label="正在加载配置" minHeight={420} />
      </AdminLayout>
    );
  }

  if (!configs) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="error">加载配置失败</Alert>
      </Box>
    );
  }

  return (
    <AdminLayout title="配置中心" subtitle="统一管理连接、模型与系统能力">
      <Container maxWidth="lg" sx={{ px: 0 }}>
        <Card>
          <Tabs value={tabValue} onChange={(_, v) => setTabValue(v)}>
            <Tab label="Neo4j 配置" />
            <Tab label="AI / 模型 / 检索" />
            <Tab label="NL2Cypher 配置" />
          </Tabs>

	          {/* Neo4j 配置 */}
	          <TabPanel value={tabValue} index={0}>
	            <Stack spacing={2}>
                <Box
                  sx={(theme) => ({
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: 1,
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    p: 1.5,
                    border: `1px solid ${theme.palette.divider}`,
                    borderRadius: 1.5,
                    bgcolor: theme.palette.background.paper,
                    boxShadow: '0 10px 24px rgba(15, 23, 42, 0.08)',
                  })}
                >
                  <Stack spacing={0.25} sx={{ minWidth: 240 }}>
                    <Typography variant="subtitle2">Neo4j 连接配置</Typography>
                    <Typography variant="caption" color={neo4jDirtyCount > 0 ? 'warning.main' : 'text.secondary'}>
                      {neo4jDirtyCount > 0
                        ? `${neo4jDirtyCount} 项待测试，通过后自动保存`
                        : `当前数据库：${formValues.neo4j_database || 'neo4j'}`}
                    </Typography>
                  </Stack>
                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap justifyContent="flex-end">
                    <Chip
                      size="small"
                      color={neo4jPasswordConfigured ? 'success' : 'warning'}
                      variant={neo4jPasswordConfigured ? 'outlined' : 'filled'}
                      label={neo4jPasswordConfigured ? '密码已保存' : '密码未保存'}
                    />
                    <AdminLoadingButton
                      variant="contained"
                      startIcon={<ScienceOutlined />}
                      loading={testingNeo4j}
                      onClick={() => handleTest('neo4j')}
                      disabled={saving || testingNeo4j}
                      label={neo4jDirtyCount > 0 ? '测试并保存' : '测试连接'}
                      loadingLabel="测试中..."
                    />
                  </Stack>
                </Box>
                <TextField
                  fullWidth
                  label="URI"
                  value={formValues.neo4j_uri}
                  onChange={(e) => handleFormChange('neo4j_uri', e.target.value)}
                  helperText="例如: bolt://localhost:7687"
                />
	                <TextField
	                  fullWidth
	                  label="用户名"
	                  value={formValues.neo4j_user}
	                  onChange={(e) => handleFormChange('neo4j_user', e.target.value)}
	                />
	                <TextField
	                  fullWidth
	                  label="数据库名"
	                  value={formValues.neo4j_database}
	                  onChange={(e) => handleFormChange('neo4j_database', e.target.value)}
	                  helperText="Aura 通常填写实例数据库名；本地默认 neo4j"
	                  placeholder="neo4j"
	                />
	                <TextField
	                  fullWidth
	                  label="密码"
	                  type={showPassword['neo4j_password'] ? 'text' : 'password'}
	                  value={formValues.neo4j_password}
	                  onChange={(e) => handleFormChange('neo4j_password', e.target.value)}
	                  placeholder={neo4jPasswordConfigured ? '已保存 Neo4j 密码' : '请输入 Neo4j 密码'}
	                  helperText={neo4jPasswordConfigured ? '密码已保存；页面不会回显明文' : '尚未保存 Neo4j 密码'}
	                  InputProps={{
	                    endAdornment: (
	                      <InputAdornment position="end">
	                        {neo4jPasswordConfigured && !formValues.neo4j_password && (
	                          <Chip
	                            label="已保存"
	                            size="small"
	                            color="success"
	                            variant="outlined"
	                            sx={{ mr: 1 }}
	                          />
	                        )}
	                        <IconButton
	                          onClick={() => togglePasswordVisibility('neo4j_password')}
	                          edge="end"
                        >
                          {showPassword['neo4j_password'] ? <VisibilityOff /> : <Visibility />}
                        </IconButton>
                      </InputAdornment>
                    ),
                  }}
                />
	            </Stack>
	          </TabPanel>

	          {/* AI 服务配置 */}
	          <TabPanel value={tabValue} index={1}>
	            <Stack spacing={2}>
                <Box
                  sx={(theme) => ({
                    position: 'sticky',
                    top: 0,
                    zIndex: 2,
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: 1,
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    p: 1.5,
                    border: `1px solid ${theme.palette.divider}`,
                    borderRadius: 1.5,
                    bgcolor: theme.palette.background.paper,
                    boxShadow: '0 10px 24px rgba(15, 23, 42, 0.08)',
                  })}
                >
                  <Stack spacing={0.25} sx={{ minWidth: 220 }}>
                    <Typography variant="subtitle2">AI / 检索 / 解析配置</Typography>
                    <Typography variant="caption" color={aiDirtyCount > 0 ? 'warning.main' : 'text.secondary'}>
                      {aiDirtyCount > 0 ? `${aiDirtyCount} 项未保存，测试前会自动保存` : `当前模型：${formValues.ai_service_model || '未配置'}`}
                    </Typography>
                  </Stack>
                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap justifyContent="flex-end">
                    <AdminLoadingButton
                      variant={aiDirtyCount > 0 ? 'contained' : 'outlined'}
                      startIcon={<SaveOutlined />}
                      loading={saving}
                      onClick={handleSaveAiService}
                      disabled={saving || aiDirtyCount === 0}
                      label={aiDirtyCount > 0 ? `保存 (${aiDirtyCount})` : '已保存'}
                      loadingLabel="保存中..."
                    />
                    <AdminLoadingButton
                      variant="outlined"
                      loading={loadingModels}
                      onClick={() => handleFetchModels('ai_service')}
                      disabled={loadingModels || saving}
                      label="获取模型"
                      loadingLabel="获取中..."
                    />
                    <Button
                      variant="outlined"
                      startIcon={<ScienceOutlined />}
                      onClick={() => handleTest('ai_service')}
                      disabled={saving}
                    >
                      测试 API
                    </Button>
                    <AdminLoadingButton
                      variant="contained"
                      startIcon={<ScienceOutlined />}
                      loading={testingModel}
                      onClick={handleModelConnectionTest}
                      disabled={testingModel || saving}
                      label="测试当前模型"
                      loadingLabel="测试中..."
                    />
                  </Stack>
                </Box>
                <Box
                  id="ai-config-overview"
                  sx={{
                    display: 'grid',
                    gridTemplateColumns: { xs: '1fr', md: 'repeat(auto-fit, minmax(180px, 1fr))' },
                    gap: 1.5,
                    scrollMarginTop: 120,
                  }}
                >
                  {[
                    {
                      id: 'ai-service-basic',
                      title: 'AI 基础',
                      icon: <SettingsOutlined fontSize="small" />,
                      summary: `${formValues.ai_service_provider || 'provider'} · ${formValues.ai_service_enabled === 'true' ? '启用' : '禁用'}`,
                    },
                    {
                      id: 'model-strategy',
                      title: '模型策略',
                      icon: <PsychologyAltOutlined fontSize="small" />,
                      summary: `问答 ${formValues.ai_service_docqa_reasoning_profile} / 调研 ${formValues.ai_service_deep_research_reasoning_profile}`,
                    },
                    {
                      id: 'retrieval-strategy',
                      title: '检索策略',
                      icon: <ManageSearchOutlined fontSize="small" />,
                      summary: `${formValues.retrieval_mode} · 图谱扩展 ${formValues.retrieval_graph_enabled === 'true' ? '开' : '关'}`,
                    },
                    {
                      id: 'embedding-vector-store',
                      title: 'Embedding / Milvus',
                      icon: <StorageOutlined fontSize="small" />,
                      summary: `${formValues.embedding_model || '未配置'} · Milvus ${formValues.vector_store_enabled === 'true' ? '启用' : '禁用'}`,
                    },
                    {
                      id: 'document-parser',
                      title: '文档解析 / MinerU',
                      icon: <DescriptionOutlined fontSize="small" />,
                      summary: `${formValues.document_parser_provider || 'native'} · fallback ${formValues.document_parser_fallback_provider || 'native'}`,
                    },
                  ].map((item) => (
                    <Box
                      key={item.id}
                      sx={(theme) => ({
                        display: 'grid',
                        gap: 0.75,
                        p: 1.5,
                        border: `1px solid ${theme.palette.divider}`,
                        borderRadius: 1.5,
                        bgcolor: theme.palette.background.default,
                      })}
                    >
                      <Stack direction="row" spacing={1} alignItems="center">
                        <Box sx={{ color: 'primary.main', display: 'flex' }}>{item.icon}</Box>
                        <Typography variant="subtitle2">{item.title}</Typography>
                      </Stack>
                      <Typography variant="caption" color="text.secondary">
                        {item.summary}
                      </Typography>
                      <Button size="small" variant="outlined" onClick={() => scrollToConfigSection(item.id)}>
                        查看配置
                      </Button>
                    </Box>
                  ))}
                </Box>
                <Stack direction="row" justifyContent="flex-end">
                  <Button size="small" variant="text" onClick={() => scrollToConfigSection('ai-config-overview')}>
                    回到配置分区
                  </Button>
                </Stack>
                <Box id="ai-service-basic" sx={{ scrollMarginTop: 120 }}>
	                <TextField
	                  fullWidth
	                  select
                  label="AI 服务提供商"
                  SelectProps={{ native: true }}
                  value={formValues.ai_service_provider}
                  onChange={(e) => handleFormChange('ai_service_provider', e.target.value)}
                  onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'provider', 'ai_service_provider')}
                  helperText="选择 AI 服务提供商（OpenAI 兼容适用于智谱、通义千问等）"
                >
                  <option value="openai">OpenAI</option>
                  <option value="openai_compatible">OpenAI 兼容（智谱/通义千问等）</option>
                  <option value="claude">Claude (Anthropic)</option>
                </TextField>
                <TextField
                  fullWidth
                  select
                  label="启用状态"
                  SelectProps={{ native: true }}
                  value={formValues.ai_service_enabled}
                  onChange={(e) => handleFormChange('ai_service_enabled', e.target.value)}
                  onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'enabled', 'ai_service_enabled')}
                  helperText="是否启用 AI 服务"
                >
                  <option value="true">启用</option>
                  <option value="false">禁用</option>
                </TextField>
                <TextField
                  fullWidth
                  label="API 地址（可选）"
                  value={formValues.ai_service_base_url}
                  onChange={(e) => handleFormChange('ai_service_base_url', e.target.value)}
                  onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'base_url', 'ai_service_base_url')}
                  helperText="留空使用官方地址，或填入转发地址，例如: https://api.openai-proxy.com/v1"
                  placeholder="https://api.openai.com/v1"
                />
                <TextField
                  fullWidth
                  label="API Key"
                  type={editingApiKey && !showPassword['ai_service_key'] ? 'password' : 'text'}
                  value={apiKeyDisplayValue}
                  onChange={(e) => {
                    if (!editingApiKey) return;
                    handleFormChange('ai_service_api_key', e.target.value);
                  }}
                  onBlur={() => {
                    if (editingApiKey) {
                      handleSaveField('ai_service' as ConfigCategory, 'api_key', 'ai_service_api_key');
                    }
                  }}
                  placeholder={apiKeyConfigured ? '已保存 API Key' : '请输入 API Key'}
                  helperText={
                    editingApiKey
                      ? '输入新的 API Key，保存后会替换当前密钥'
                      : apiKeyConfigured
                        ? 'API Key 已保存；页面只显示安全预览'
                        : '尚未保存 API Key'
                  }
                  InputProps={{
                    readOnly: !editingApiKey,
                    endAdornment: (
                      <InputAdornment position="end">
                        {apiKeyConfigured && !editingApiKey && (
                          <Chip
                            label="已保存"
                            size="small"
                            color="success"
                            variant="outlined"
                            sx={{ mr: 1 }}
                          />
                        )}
                        <Button
                          size="small"
                          variant="text"
                          onMouseDown={(event) => event.preventDefault()}
                          onClick={() => {
                            if (editingApiKey) {
                              setEditingApiKey(false);
                              setFormValues(prev => ({ ...prev, ai_service_api_key: '' }));
                              setDirtyFields(prev => {
                                const next = new Set(prev);
                                next.delete('ai_service_api_key');
                                return next;
                              });
                              return;
                            }
                            setEditingApiKey(true);
                            setFormValues(prev => ({ ...prev, ai_service_api_key: '' }));
                          }}
                          sx={{ mr: 0.5, minWidth: 56 }}
                        >
                          {editingApiKey ? '取消' : apiKeyConfigured ? '更换' : '填写'}
                        </Button>
                        {editingApiKey && (
                          <IconButton
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => togglePasswordVisibility('ai_service_key')}
                            edge="end"
                          >
                            {showPassword['ai_service_key'] ? <VisibilityOff /> : <Visibility />}
                          </IconButton>
                        )}
                      </InputAdornment>
                    ),
                  }}
                />
                </Box>
	                <Box id="model-strategy" sx={{ display: 'grid', gap: 1, scrollMarginTop: 120 }}>
                    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                      <Typography variant="subtitle2">问答模型</Typography>
                      <Chip size="small" variant="outlined" label="用于生成回答" />
                    </Stack>
	                  <TextField
	                    fullWidth
	                    label="问答模型"
	                    value={formValues.ai_service_model}
	                    onChange={(e) => handleFormChange('ai_service_model', e.target.value)}
	                    onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'model', 'ai_service_model')}
	                    helperText={
	                      modelSource === 'remote'
	                        ? `来自服务商模型接口（${availableModels.length} 个）`
	                        : modelSourceMessage || '可手动输入模型名；未获取到服务商模型列表时只显示当前配置'
                    }
                    InputProps={{
                      endAdornment: (
	                        <InputAdornment position="end">
	                          <Button
	                            size="small"
	                            variant="text"
	                            startIcon={<FormatListBulletedOutlined />}
	                            onMouseDown={(event) => event.preventDefault()}
	                            onClick={() => {
                                setModelPickerTarget('ai_service');
                                setModelPickerOpen(true);
                              }}
	                            disabled={modelOptions.length === 0}
	                            sx={{ mr: 0.5, whiteSpace: 'nowrap' }}
	                          >
	                            选择
	                          </Button>
	                          <IconButton
	                            edge="end"
	                            onMouseDown={(event) => event.preventDefault()}
	                            onClick={() => {
	                              handleFormChange('ai_service_model', '');
	                            }}
	                          >
	                            <Clear />
	                          </IconButton>
	                        </InputAdornment>
	                      ),
	                    }}
	                  />
	                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
	                    <Chip
	                      size="small"
	                      variant="outlined"
	                      label={modelOptions.length > 0 ? `已加载 ${modelOptions.length} 个模型` : '请先获取模型'}
	                    />
	                    {modelSource && (
	                      <Chip
	                        size="small"
	                        color={modelSource === 'remote' ? 'success' : 'default'}
	                        variant={modelSource === 'remote' ? 'filled' : 'outlined'}
	                        label={modelSource === 'remote' ? '服务商实时列表' : '本地候选'}
	                      />
	                    )}
	                  </Stack>
	                </Box>
                <Box sx={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 2 }}>
                  <TextField
                    fullWidth
                    select
                    label="问答默认档位"
                    SelectProps={{ native: true }}
                    value={formValues.ai_service_docqa_reasoning_profile}
                    onChange={(e) => handleFormChange('ai_service_docqa_reasoning_profile', e.target.value)}
                    onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'docqa_reasoning_profile', 'ai_service_docqa_reasoning_profile')}
                    helperText="文档问答默认使用的推理档位"
                  >
                    {REASONING_PROFILE_OPTIONS.map((item) => (
                      <option key={item} value={item}>{item}</option>
                    ))}
                  </TextField>
                  <TextField
                    fullWidth
                    select
                    label="调研默认档位"
                    SelectProps={{ native: true }}
                    value={formValues.ai_service_deep_research_reasoning_profile}
                    onChange={(e) => handleFormChange('ai_service_deep_research_reasoning_profile', e.target.value)}
                    onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'deep_research_reasoning_profile', 'ai_service_deep_research_reasoning_profile')}
                    helperText="深度调研默认使用的推理档位"
                  >
                    {REASONING_PROFILE_OPTIONS.map((item) => (
                      <option key={item} value={item}>{item}</option>
                    ))}
                  </TextField>
                  <TextField
                    fullWidth
                    select
                    label="建图默认档位"
                    SelectProps={{ native: true }}
                    value={formValues.ai_service_graph_extract_reasoning_profile}
                    onChange={(e) => handleFormChange('ai_service_graph_extract_reasoning_profile', e.target.value)}
                    onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'graph_extract_reasoning_profile', 'ai_service_graph_extract_reasoning_profile')}
                    helperText="常规图谱抽取默认使用的推理档位"
                  >
                    {REASONING_PROFILE_OPTIONS.map((item) => (
                      <option key={item} value={item}>{item}</option>
                    ))}
                  </TextField>
                  <TextField
                    fullWidth
                    select
                    label="复杂建图档位"
                    SelectProps={{ native: true }}
                    value={formValues.ai_service_graph_extract_complex_reasoning_profile}
                    onChange={(e) => handleFormChange('ai_service_graph_extract_complex_reasoning_profile', e.target.value)}
                    onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'graph_extract_complex_reasoning_profile', 'ai_service_graph_extract_complex_reasoning_profile')}
                    helperText="复杂抽取任务默认使用的推理档位"
                  >
                    {REASONING_PROFILE_OPTIONS.map((item) => (
                      <option key={item} value={item}>{item}</option>
                    ))}
                  </TextField>
                </Box>
                <Box sx={{ display: 'grid', gap: 1 }}>
                  <Typography variant="subtitle2">生成参数</Typography>
                  <Box
                    sx={{
                      display: 'grid',
                      gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(180px, 240px))' },
                      gap: 2,
                      width: '100%',
                      maxWidth: 520,
                    }}
                  >
                    <TextField
                      fullWidth
                      label="Max Tokens"
                      type="number"
                      value={formValues.ai_service_max_tokens}
                      onChange={(e) => handleFormChange('ai_service_max_tokens', e.target.value)}
                      onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'max_tokens', 'ai_service_max_tokens')}
                    />
                    <TextField
                      fullWidth
                      label="Temperature"
                      type="number"
                      inputProps={{ step: 0.1, min: 0, max: 2 }}
                      value={formValues.ai_service_temperature}
                      onChange={(e) => handleFormChange('ai_service_temperature', e.target.value)}
                      onBlur={() => handleSaveField('ai_service' as ConfigCategory, 'temperature', 'ai_service_temperature')}
                    />
                  </Box>
                </Box>
                <Box id="retrieval-strategy" sx={{ display: 'grid', gap: 2, mt: 1, pt: 2, borderTop: 1, borderColor: 'divider', scrollMarginTop: 120 }}>
                  <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ xs: 'stretch', md: 'center' }} justifyContent="space-between">
                    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                      <Typography variant="subtitle2">嵌入模型与向量检索</Typography>
                      <Chip size="small" color="primary" variant="outlined" label="用于 Milvus 召回" />
                      <Chip
                        size="small"
                        color={formValues.vector_store_enabled === 'true' ? 'success' : 'default'}
                        variant={formValues.vector_store_enabled === 'true' ? 'filled' : 'outlined'}
                        label={formValues.vector_store_enabled === 'true' ? '向量检索已启用' : '向量检索已禁用'}
                      />
                      <Chip
                        size="small"
                        color={vectorStoreTestResult?.success ? 'success' : vectorStoreTestResult ? 'error' : 'default'}
                        variant={vectorStoreTestResult ? 'filled' : 'outlined'}
                        label={vectorStoreTestResult?.success ? 'Milvus 已连接' : vectorStoreTestResult ? 'Milvus 连接失败' : 'Milvus 未检测'}
                      />
                    </Stack>
                    <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap justifyContent={{ xs: 'flex-start', md: 'flex-end' }}>
                      <AdminLoadingButton
                        variant="outlined"
                        startIcon={<ScienceOutlined />}
                        loading={testingEmbedding}
                        onClick={handleEmbeddingConnectionTest}
                        disabled={testingEmbedding || saving}
                        label="测试嵌入模型"
                        loadingLabel="测试中..."
                      />
                      <AdminLoadingButton
                        variant="outlined"
                        startIcon={<ScienceOutlined />}
                        loading={testingVectorStore}
                        onClick={handleVectorStoreConnectionTest}
                        disabled={testingVectorStore || saving}
                        label="测试 Milvus"
                        loadingLabel="测试中..."
                      />
                    </Stack>
                  </Stack>
                  <Box id="embedding-vector-store" sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(2, 1fr)' }, gap: 2, scrollMarginTop: 120 }}>
                    <TextField
                      fullWidth
                      select
                      label="检索模式"
                      SelectProps={{ native: true }}
                      value={formValues.retrieval_mode}
                      onChange={(e) => handleFormChange('retrieval_mode', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'mode', 'retrieval_mode')}
                      helperText="keyword 保持旧全文检索；hybrid 同时使用全文和向量；graph_hybrid 再加入图谱扩展"
                    >
                      <option value="keyword">keyword</option>
                      <option value="vector">vector</option>
                      <option value="hybrid">hybrid</option>
                      <option value="graph_hybrid">graph_hybrid</option>
                    </TextField>
                    <TextField
                      fullWidth
                      select
                      label="启用向量库"
                      SelectProps={{ native: true }}
                      value={formValues.vector_store_enabled}
                      onChange={(e) => handleFormChange('vector_store_enabled', e.target.value)}
                      onBlur={() => handleSaveField('vector_store', 'enabled', 'vector_store_enabled')}
                    >
                      <option value="false">禁用</option>
                      <option value="true">启用</option>
                    </TextField>
                    <TextField
                      fullWidth
                      select
                      label="向量库类型"
                      SelectProps={{ native: true }}
                      value={formValues.vector_store_provider}
                      onChange={(e) => handleFormChange('vector_store_provider', e.target.value)}
                      onBlur={() => handleSaveField('vector_store', 'provider', 'vector_store_provider')}
                    >
                      <option value="milvus">Milvus</option>
                    </TextField>
                    <TextField
                      fullWidth
                      select
                      label="启用 Embedding"
                      SelectProps={{ native: true }}
                      value={formValues.embedding_enabled}
                      onChange={(e) => handleFormChange('embedding_enabled', e.target.value)}
                      onBlur={() => handleSaveField('embedding', 'enabled', 'embedding_enabled')}
                    >
                      <option value="true">启用</option>
                      <option value="false">禁用</option>
                    </TextField>
                    <TextField
                      fullWidth
                      select
                      label="嵌入服务类型"
                      SelectProps={{ native: true }}
                      value={formValues.embedding_provider}
                      onChange={(e) => handleFormChange('embedding_provider', e.target.value)}
                      onBlur={() => handleSaveField('embedding', 'provider', 'embedding_provider')}
                    >
                      <option value="openai">OpenAI</option>
                      <option value="openai_compatible">OpenAI Compatible</option>
                    </TextField>
                    <TextField
                      fullWidth
                      label="嵌入模型"
                      value={formValues.embedding_model}
                      onChange={(e) => handleFormChange('embedding_model', e.target.value)}
                      onBlur={() => handleSaveField('embedding', 'model', 'embedding_model')}
                      helperText="默认从 AI 服务网关获取候选；模型维度需要和下方配置一致"
                      InputProps={{
                        endAdornment: (
                          <InputAdornment position="end">
                            <Button
                              size="small"
                              variant="text"
                              startIcon={<FormatListBulletedOutlined />}
                              onMouseDown={(event) => event.preventDefault()}
                              onClick={() => handleFetchModels('embedding')}
                              disabled={loadingModels || saving}
                              sx={{ mr: 0.5, whiteSpace: 'nowrap' }}
                            >
                              获取
                            </Button>
                          </InputAdornment>
                        ),
                      }}
                    />
                    <TextField
                      fullWidth
                      label="嵌入服务地址"
                      value={formValues.embedding_base_url}
                      onChange={(e) => handleFormChange('embedding_base_url', e.target.value)}
                      onBlur={() => handleSaveField('embedding', 'base_url', 'embedding_base_url')}
                      placeholder="默认留空，复用 AI 服务网关地址"
                      helperText="只有嵌入模型走独立供应商时才需要填写"
                    />
                    <TextField
                      fullWidth
                      label="嵌入 API Key"
                      type={editingEmbeddingApiKey && !showPassword['embedding_api_key'] ? 'password' : 'text'}
                      value={embeddingApiKeyDisplayValue}
                      onChange={(e) => {
                        if (!editingEmbeddingApiKey) return;
                        handleFormChange('embedding_api_key', e.target.value);
                      }}
                      onBlur={() => {
                        if (editingEmbeddingApiKey) {
                          handleSaveField('embedding', 'api_key', 'embedding_api_key');
                        }
                      }}
                      helperText={editingEmbeddingApiKey ? '可单独配置；留空时后端会复用 AI 服务 API Key' : embeddingApiKeyConfigured ? '嵌入 API Key 已保存' : '未单独保存，默认复用 AI 服务 API Key'}
                      InputProps={{
                        readOnly: !editingEmbeddingApiKey,
                        endAdornment: (
                          <InputAdornment position="end">
                            {embeddingApiKeyConfigured && !editingEmbeddingApiKey && (
                              <Chip label="已保存" size="small" color="success" variant="outlined" sx={{ mr: 1 }} />
                            )}
                            <Button
                              size="small"
                              variant="text"
                              onMouseDown={(event) => event.preventDefault()}
                              onClick={() => {
                                if (editingEmbeddingApiKey) {
                                  setEditingEmbeddingApiKey(false);
                                  setFormValues(prev => ({ ...prev, embedding_api_key: '' }));
                                  setDirtyFields(prev => {
                                    const next = new Set(prev);
                                    next.delete('embedding_api_key');
                                    return next;
                                  });
                                  return;
                                }
                                setEditingEmbeddingApiKey(true);
                                setFormValues(prev => ({ ...prev, embedding_api_key: '' }));
                              }}
                              sx={{ mr: 0.5, minWidth: 56 }}
                            >
                              {editingEmbeddingApiKey ? '取消' : embeddingApiKeyConfigured ? '更换' : '填写'}
                            </Button>
                            {editingEmbeddingApiKey && (
                              <IconButton
                                onMouseDown={(event) => event.preventDefault()}
                                onClick={() => togglePasswordVisibility('embedding_api_key')}
                                edge="end"
                              >
                                {showPassword['embedding_api_key'] ? <VisibilityOff /> : <Visibility />}
                              </IconButton>
                            )}
                          </InputAdornment>
                        ),
                      }}
                    />
                    <TextField
                      fullWidth
                      label="嵌入维度"
                      type="number"
                      value={formValues.embedding_dimension}
                      onChange={(e) => handleFormChange('embedding_dimension', e.target.value)}
                      onBlur={() => handleSaveField('embedding', 'dimension', 'embedding_dimension')}
                    />
                    <TextField
                      fullWidth
                      label="批大小"
                      type="number"
                      value={formValues.embedding_batch_size}
                      onChange={(e) => handleFormChange('embedding_batch_size', e.target.value)}
                      onBlur={() => handleSaveField('embedding', 'batch_size', 'embedding_batch_size')}
                    />
                    <TextField
                      fullWidth
                      label="Milvus 地址"
                      value={formValues.vector_store_uri}
                      onChange={(e) => handleFormChange('vector_store_uri', e.target.value)}
                      onBlur={() => handleSaveField('vector_store', 'uri', 'vector_store_uri')}
                    />
                    <TextField
                      fullWidth
                      label="Milvus Collection"
                      value={formValues.vector_store_collection}
                      onChange={(e) => handleFormChange('vector_store_collection', e.target.value)}
                      onBlur={() => handleSaveField('vector_store', 'collection', 'vector_store_collection')}
                    />
                    <TextField
                      fullWidth
                      label="Milvus 数据库"
                      value={formValues.vector_store_db_name}
                      onChange={(e) => handleFormChange('vector_store_db_name', e.target.value)}
                      onBlur={() => handleSaveField('vector_store', 'db_name', 'vector_store_db_name')}
                    />
                    <TextField
                      fullWidth
                      label="Milvus Token"
                      type={editingVectorStoreToken && !showPassword['vector_store_token'] ? 'password' : 'text'}
                      value={vectorStoreTokenDisplayValue}
                      onChange={(e) => {
                        if (!editingVectorStoreToken) return;
                        handleFormChange('vector_store_token', e.target.value);
                      }}
                      onBlur={() => {
                        if (editingVectorStoreToken) {
                          handleSaveField('vector_store', 'token', 'vector_store_token');
                        }
                      }}
                      helperText={editingVectorStoreToken ? '云端 Milvus/Zilliz 可填写 token；本地通常留空' : vectorStoreTokenConfigured ? 'Token 已保存' : '本地 Milvus 通常不需要 Token'}
                      InputProps={{
                        readOnly: !editingVectorStoreToken,
                        endAdornment: (
                          <InputAdornment position="end">
                            {vectorStoreTokenConfigured && !editingVectorStoreToken && (
                              <Chip label="已保存" size="small" color="success" variant="outlined" sx={{ mr: 1 }} />
                            )}
                            <Button
                              size="small"
                              variant="text"
                              onMouseDown={(event) => event.preventDefault()}
                              onClick={() => {
                                if (editingVectorStoreToken) {
                                  setEditingVectorStoreToken(false);
                                  setFormValues(prev => ({ ...prev, vector_store_token: '' }));
                                  setDirtyFields(prev => {
                                    const next = new Set(prev);
                                    next.delete('vector_store_token');
                                    return next;
                                  });
                                  return;
                                }
                                setEditingVectorStoreToken(true);
                                setFormValues(prev => ({ ...prev, vector_store_token: '' }));
                              }}
                              sx={{ mr: 0.5, minWidth: 56 }}
                            >
                              {editingVectorStoreToken ? '取消' : vectorStoreTokenConfigured ? '更换' : '填写'}
                            </Button>
                            {editingVectorStoreToken && (
                              <IconButton
                                onMouseDown={(event) => event.preventDefault()}
                                onClick={() => togglePasswordVisibility('vector_store_token')}
                                edge="end"
                              >
                                {showPassword['vector_store_token'] ? <VisibilityOff /> : <Visibility />}
                              </IconButton>
                            )}
                          </InputAdornment>
                        ),
                      }}
                    />
                  </Box>
                  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(4, 1fr)' }, gap: 2 }}>
                    <TextField
                      fullWidth
                      label="RRF K"
                      type="number"
                      value={formValues.retrieval_rrf_k}
                      onChange={(e) => handleFormChange('retrieval_rrf_k', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'rrf_k', 'retrieval_rrf_k')}
                    />
                    <TextField
                      fullWidth
                      label="候选倍数"
                      type="number"
                      value={formValues.retrieval_candidate_multiplier}
                      onChange={(e) => handleFormChange('retrieval_candidate_multiplier', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'candidate_multiplier', 'retrieval_candidate_multiplier')}
                    />
                    <TextField
                      fullWidth
                      select
                      label="图谱扩展"
                      SelectProps={{ native: true }}
                      value={formValues.retrieval_graph_enabled}
                      onChange={(e) => handleFormChange('retrieval_graph_enabled', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'graph_enabled', 'retrieval_graph_enabled')}
                    >
                      <option value="true">启用</option>
                      <option value="false">禁用</option>
                    </TextField>
                    <TextField
                      fullWidth
                      select
                      label="重排器"
                      SelectProps={{ native: true }}
                      value={formValues.retrieval_rerank_enabled}
                      onChange={(e) => handleFormChange('retrieval_rerank_enabled', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'rerank_enabled', 'retrieval_rerank_enabled')}
                    >
                      <option value="false">禁用</option>
                      <option value="true">启用</option>
                    </TextField>
                  </Box>
                  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '2fr 2fr 1.2fr 1fr 1fr' }, gap: 2 }}>
                    <TextField
                      fullWidth
                      label="Reranker 模型"
                      value={formValues.retrieval_rerank_model}
                      onChange={(e) => handleFormChange('retrieval_rerank_model', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'rerank_model', 'retrieval_rerank_model')}
                      helperText="留空则跳过二阶段重排"
                    />
                    <TextField
                      fullWidth
                      label="Reranker 地址"
                      value={formValues.retrieval_rerank_base_url}
                      onChange={(e) => handleFormChange('retrieval_rerank_base_url', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'rerank_base_url', 'retrieval_rerank_base_url')}
                      helperText="留空复用 AI 服务地址"
                    />
                    <TextField
                      fullWidth
                      label="接口路径"
                      value={formValues.retrieval_rerank_endpoint_path}
                      onChange={(e) => handleFormChange('retrieval_rerank_endpoint_path', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'rerank_endpoint_path', 'retrieval_rerank_endpoint_path')}
                    />
                    <TextField
                      fullWidth
                      label="重排候选"
                      type="number"
                      value={formValues.retrieval_rerank_top_n}
                      onChange={(e) => handleFormChange('retrieval_rerank_top_n', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'rerank_top_n', 'retrieval_rerank_top_n')}
                    />
                    <TextField
                      fullWidth
                      label="超时秒"
                      type="number"
                      value={formValues.retrieval_rerank_timeout_seconds}
                      onChange={(e) => handleFormChange('retrieval_rerank_timeout_seconds', e.target.value)}
                      onBlur={() => handleSaveField('retrieval', 'rerank_timeout_seconds', 'retrieval_rerank_timeout_seconds')}
                    />
                  </Box>
                </Box>
                <Box id="document-parser" sx={{ display: 'grid', gap: 2, mt: 1, pt: 2, borderTop: 1, borderColor: 'divider', scrollMarginTop: 120 }}>
                  <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ xs: 'stretch', md: 'center' }} justifyContent="space-between">
                    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                      <Typography variant="subtitle2">文档解析器</Typography>
                      <Chip size="small" color="primary" variant="outlined" label="PDF / Office 解析入口" />
                      <Chip
                        size="small"
                        color={formValues.document_parser_provider === 'mineru' ? 'success' : 'default'}
                        variant={formValues.document_parser_provider === 'mineru' ? 'filled' : 'outlined'}
                        label={formValues.document_parser_provider === 'mineru' ? 'MinerU 已选择' : '内置解析器'}
                      />
                    </Stack>
                    <AdminLoadingButton
                      variant="outlined"
                      startIcon={<ScienceOutlined />}
                      loading={testingDocumentParser}
                      onClick={handleDocumentParserConnectionTest}
                      disabled={testingDocumentParser || saving}
                      label="测试解析器"
                      loadingLabel="测试中..."
                    />
                  </Stack>
                  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(4, 1fr)' }, gap: 2 }}>
                    <TextField
                      fullWidth
                      select
                      label="解析器"
                      SelectProps={{ native: true }}
                      value={formValues.document_parser_provider}
                      onChange={(e) => handleFormChange('document_parser_provider', e.target.value)}
                      onBlur={() => handleSaveField('document_parser', 'provider', 'document_parser_provider')}
                      helperText="native 为内置解析；mineru 走外部 MinerU 服务"
                    >
                      <option value="native">native</option>
                      <option value="mineru">mineru</option>
                    </TextField>
                    <TextField
                      fullWidth
                      select
                      label="失败回退"
                      SelectProps={{ native: true }}
                      value={formValues.document_parser_fallback_provider}
                      onChange={(e) => handleFormChange('document_parser_fallback_provider', e.target.value)}
                      onBlur={() => handleSaveField('document_parser', 'fallback_provider', 'document_parser_fallback_provider')}
                      helperText="建议保持 native，MinerU 异常时保底解析"
                    >
                      <option value="native">native</option>
                      <option value="none">none</option>
                    </TextField>
                    <TextField
                      fullWidth
                      label="MinerU 地址"
                      value={formValues.document_parser_base_url}
                      onChange={(e) => handleFormChange('document_parser_base_url', e.target.value)}
                      onBlur={() => handleSaveField('document_parser', 'base_url', 'document_parser_base_url')}
                      placeholder="http://mineru-api:8040"
                      helperText="只填写服务根地址，不包含 /file_parse"
                    />
                    <TextField
                      fullWidth
                      label="解析路径"
                      value={formValues.document_parser_endpoint_path}
                      onChange={(e) => handleFormChange('document_parser_endpoint_path', e.target.value)}
                      onBlur={() => handleSaveField('document_parser', 'endpoint_path', 'document_parser_endpoint_path')}
                      placeholder="/file_parse"
                    />
                    <TextField
                      fullWidth
                      label="文件字段"
                      value={formValues.document_parser_file_field}
                      onChange={(e) => handleFormChange('document_parser_file_field', e.target.value)}
                      onBlur={() => handleSaveField('document_parser', 'file_field', 'document_parser_file_field')}
                      helperText="当前 MinerU API 使用 files"
                    />
                    <TextField
                      fullWidth
                      select
                      label="解析模式"
                      SelectProps={{ native: true }}
                      value={formValues.document_parser_parse_mode}
                      onChange={(e) => handleFormChange('document_parser_parse_mode', e.target.value)}
                      onBlur={() => handleSaveField('document_parser', 'parse_mode', 'document_parser_parse_mode')}
                    >
                      <option value="auto">auto</option>
                      <option value="ocr">ocr</option>
                      <option value="txt">txt</option>
                    </TextField>
                    <TextField
                      fullWidth
                      label="输出格式"
                      value={formValues.document_parser_output_format}
                      onChange={(e) => handleFormChange('document_parser_output_format', e.target.value)}
                      onBlur={() => handleSaveField('document_parser', 'output_format', 'document_parser_output_format')}
                      helperText="默认 markdown,json"
                    />
                    <TextField
                      fullWidth
                      label="超时秒数"
                      type="number"
                      value={formValues.document_parser_timeout_seconds}
                      onChange={(e) => handleFormChange('document_parser_timeout_seconds', e.target.value)}
                      onBlur={() => handleSaveField('document_parser', 'timeout_seconds', 'document_parser_timeout_seconds')}
                    />
                  </Box>
                </Box>
                {modelCatalog.length > 0 && (
                  <Box sx={{ display: 'grid', gap: 1 }}>
                    <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
                      <Typography variant="subtitle2">{modelPickerTarget === 'embedding' ? '嵌入模型目录' : '问答模型目录'}</Typography>
	                      <Typography variant="caption" color="text.secondary">
                        {modelSource === 'remote'
                          ? `服务商返回 ${modelCatalog.length} 个模型`
                          : `非服务商完整列表：${modelCatalog.length} 个候选`}
	                      </Typography>
                    </Stack>
                    <Box sx={{ display: 'grid', gap: 1, maxHeight: 360, overflowY: 'auto', pr: 0.5 }}>
	                      {modelCatalog.map((item) => {
	                        const isCurrent = item.model.toLowerCase() === selectedModelKey;
	                        return (
		                      <Box
		                        key={`${item.provider}:${item.model}`}
		                        sx={(theme) => ({
		                          display: 'grid',
	                            gridTemplateColumns: { xs: '1fr', md: 'minmax(0, 1.3fr) minmax(0, 0.9fr) minmax(0, 0.9fr) auto auto' },
		                          gap: 1.5,
		                          alignItems: 'center',
		                          p: 1.25,
                            border: `1px solid ${isCurrent ? theme.palette.primary.main : theme.palette.divider}`,
	                          borderRadius: 1,
                            bgcolor: isCurrent ? theme.palette.action.selected : theme.palette.background.default,
	                        })}
	                      >
	                        <Box>
                            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                              <Typography variant="body2" fontWeight={600}>{item.label || item.model}</Typography>
                              {isCurrent && <Chip size="small" label="当前" color="primary" variant="outlined" />}
                            </Stack>
	                          <Typography variant="caption" color="text.secondary">
	                            {item.provider} · {item.model}
	                          </Typography>
                        </Box>
                        <Typography variant="caption" color="text.secondary">
                          默认档位 {item.default_profile} · 支持 {item.supported_profiles.join(' / ')}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {item.max_output_tokens
                            ? `输出上限 ${item.max_output_tokens.toLocaleString()}`
                            : item.suggested_max_tokens
                              ? `建议 Max ${item.suggested_max_tokens.toLocaleString()}`
                              : '输出上限未知'}
                          {item.context_window ? ` · 上下文 ${item.context_window.toLocaleString()}` : ''}
                        </Typography>
                        <Chip
                          size="small"
	                          color={item.supports_reasoning ? 'success' : 'default'}
	                          variant={item.supports_reasoning ? 'filled' : 'outlined'}
		                          label={item.supports_reasoning ? '支持推理档位' : '固定档位'}
		                        />
	                        <Button
	                          size="small"
	                          variant={isCurrent ? 'contained' : 'outlined'}
	                          onClick={() => handleSelectModel(item.model)}
	                        >
	                          {isCurrent ? '已选' : '选用'}
	                        </Button>
		                      </Box>
	                        );
	                      })}
                    </Box>
                  </Box>
                )}
                {modelTestResult && (
                  <Alert severity={modelTestResult.success ? 'success' : 'warning'}>
                    <Stack spacing={1}>
                      <Typography variant="subtitle2">{modelTestResult.message}</Typography>
                      <Typography variant="body2">
                        Provider: {modelTestResult.provider || '-'} · Model: {modelTestResult.model || '-'} · 延迟: {modelTestResult.latency_ms?.toFixed(1) || '0.0'} ms
                      </Typography>
                      {modelTestResult.endpoint && (
                        <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                          Endpoint: {modelTestResult.endpoint}
                        </Typography>
                      )}
                      {modelTestResult.checked_at && (
                        <Typography variant="body2" color="text.secondary">
                          最近测试: {new Date(modelTestResult.checked_at).toLocaleString()}
                        </Typography>
                      )}
                      {(modelTestResult.checks || []).map((item) => (
                        <Box key={item.name} sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
                          <Typography variant="body2">
                            {item.success ? '通过' : '失败'} · {item.name}
                          </Typography>
                          <Typography variant="body2" color="text.secondary" sx={{ textAlign: 'right' }}>
                            {item.message}{item.latency_ms ? ` · ${item.latency_ms.toFixed(1)} ms` : ''}
                          </Typography>
                        </Box>
                      ))}
                    </Stack>
                  </Alert>
                )}
            </Stack>
          </TabPanel>

          {/* NL2Cypher 配置 */}
          <TabPanel value={tabValue} index={2}>
            <Stack spacing={2}>
                <TextField
                  fullWidth
                  label="是否启用"
                  select
                  SelectProps={{ native: true }}
                  value={formValues.nl2cypher_enabled}
                  onChange={(e) => handleFormChange('nl2cypher_enabled', e.target.value)}
                  onBlur={() => handleSaveField('nl2cypher', 'enabled', 'nl2cypher_enabled')}
                >
                  <option value="true">启用</option>
                  <option value="false">禁用</option>
                </TextField>
                <Box sx={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 2 }}>
                  <TextField
                    fullWidth
                    label="缓存大小"
                    type="number"
                    value={formValues.nl2cypher_cache_size}
                    onChange={(e) => handleFormChange('nl2cypher_cache_size', e.target.value)}
                    onBlur={() => handleSaveField('nl2cypher', 'cache_size', 'nl2cypher_cache_size')}
                  />
                  <TextField
                    fullWidth
                    label="最大限制"
                    type="number"
                    value={formValues.nl2cypher_max_limit}
                    onChange={(e) => handleFormChange('nl2cypher_max_limit', e.target.value)}
                    onBlur={() => handleSaveField('nl2cypher', 'max_limit', 'nl2cypher_max_limit')}
                  />
                </Box>
            </Stack>
          </TabPanel>

          {/* 底部操作按钮 */}
          <Box sx={{ p: 3, borderTop: 1, borderColor: 'divider', display: 'flex', gap: 2, justifyContent: 'flex-end', alignItems: 'center', bgcolor: 'background.default' }}>
            {neo4jDirtyCount > 0 && (
              <Typography variant="body2" color="warning.main" sx={{ mr: 2 }}>
                Neo4j {neo4jDirtyCount} 项需测试通过后保存
              </Typography>
            )}
            {manualSaveDirtyCount > 0 && (
              <Typography variant="body2" color="warning.main" sx={{ mr: 2 }}>
                {manualSaveDirtyCount} 项未保存
              </Typography>
            )}
            <Button
              variant="outlined"
              onClick={handleReset}
              disabled={dirtyFields.size === 0 || saving}
            >
              重置更改
            </Button>
            {manualSaveDirtyCount > 0 && (
              <AdminLoadingButton
                variant="contained"
                onClick={handleSaveAll}
                loading={saving}
                disabled={saving}
                color="warning"
                label={`保存所有更改 (${manualSaveDirtyCount})`}
                loadingLabel="保存中..."
              />
            )}
          </Box>
        </Card>
      </Container>

	      <Snackbar
	        open={!!message}
	        autoHideDuration={3000}
	        anchorOrigin={{ vertical: 'top', horizontal: 'center' }}
	        onClose={() => setMessage('')}
	      >
	        <Alert severity="success">{message}</Alert>
	      </Snackbar>

	      <Dialog
	        open={modelPickerOpen}
	        onClose={() => setModelPickerOpen(false)}
	        fullWidth
	        maxWidth="md"
	      >
	        <DialogTitle>
	          <Stack spacing={0.5}>
	            <Typography variant="h6">{modelPickerTarget === 'embedding' ? '选择嵌入模型' : '选择问答模型'}</Typography>
	            <Typography variant="body2" color="text.secondary">
	              {modelSource === 'remote'
	                ? `来自服务商模型接口，共 ${modelOptions.length} 个`
	                : modelOptions.length > 0
	                  ? `当前可选 ${modelOptions.length} 个${modelPickerTarget === 'embedding' ? '嵌入' : '问答'}模型候选`
	                  : '还没有模型列表，请先点击“获取模型”'}
	            </Typography>
	          </Stack>
	        </DialogTitle>
	        <DialogContent dividers>
	          {modelOptions.length === 0 ? (
	            <Alert severity="info">
	              还没有加载到模型。请关闭窗口后点击“获取模型”，或直接在模型输入框中手动填写模型名。
	            </Alert>
	          ) : (
	            <Box
	              sx={{
	                display: 'grid',
	                gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' },
	                gap: 1,
	              }}
	            >
	              {modelOptions.map((model) => {
	                const selected = model.toLowerCase() === selectedModelKey;
	                return (
	                  <Button
	                    key={model}
	                    variant={selected ? 'contained' : 'outlined'}
	                    color={selected ? 'primary' : 'inherit'}
	                    onClick={() => handleSelectModel(model)}
	                    sx={{
	                      justifyContent: 'space-between',
	                      minHeight: 46,
	                      px: 1.5,
	                      textTransform: 'none',
	                      overflow: 'hidden',
	                    }}
	                  >
	                    <Typography
	                      component="span"
	                      variant="body2"
	                      sx={{
	                        minWidth: 0,
	                        overflow: 'hidden',
	                        textOverflow: 'ellipsis',
	                        whiteSpace: 'nowrap',
	                      }}
	                    >
	                      {model}
	                    </Typography>
	                    {selected && <CheckCircle fontSize="small" />}
	                  </Button>
	                );
	              })}
	            </Box>
	          )}
	        </DialogContent>
	        <DialogActions>
	          <AdminRefreshButton
	            onClick={() => handleFetchModels(modelPickerTarget)}
	            loading={loadingModels}
	            disabled={saving}
	            label="重新获取"
	            loadingLabel="获取中..."
	          />
	          <Button onClick={() => setModelPickerOpen(false)}>关闭</Button>
	        </DialogActions>
	      </Dialog>

	      <Snackbar
	        open={!!error}
	        autoHideDuration={3000}
	        anchorOrigin={{ vertical: 'top', horizontal: 'center' }}
        onClose={() => setError('')}
      >
        <Alert severity="error">{error}</Alert>
      </Snackbar>
    </AdminLayout>
  );
};

export default ConfigPage;
