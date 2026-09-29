import axios from 'axios';
import { API_BASE_URL } from '../utils/apiBase';
import { useGraphStore } from '../store/graphStore';

// 创建 Axios 实例
export const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000, // 30 秒超时
  headers: {
    'Content-Type': 'application/json',
  },
});

// 请求拦截器
api.interceptors.request.use(
  (config) => {
    const token =
      typeof window !== 'undefined'
        ? window.localStorage.getItem('admin_token')
        : null;
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    // M4-R1 步骤 3：已选择知识库时统一注入 X-KB-ID，覆盖所有 /api/* 业务调用
    //（图谱 schema/节点、文档列表/上传/删除等）；未选择时不注入，由后端
    // 返回 KB_SCOPE_REQUIRED，不降级为全局查询。
    const activeKbId = useGraphStore.getState().activeKbId;
    if (activeKbId) {
      config.headers['X-KB-ID'] = activeKbId;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// 响应拦截器
api.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    // 统一错误处理
    if (error.response) {
      // 服务器返回错误响应
      const status = error.response.status;
      const log = status === 401 || status === 403 ? console.warn : console.error;
      const method = String(error.config?.method || 'GET').toUpperCase();
      const url = error.config?.url || '';
      const message = error.response.data?.message || error.message || '请求失败';
      const traceId = error.response.data?.trace_id;
      log(
        traceId
          ? `API ${status} ${method} ${url}: ${message} [trace_id: ${traceId}]`
          : `API ${status} ${method} ${url}: ${message}`
      );
    } else if (error.request) {
      // 请求已发送但没有收到响应
      console.error('Network Error:', error.message);
    } else {
      // 其他错误
      console.error('Error:', error.message);
    }
    return Promise.reject(error);
  }
);
