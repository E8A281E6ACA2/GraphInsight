import { useGraphStore } from '../store/graphStore';

// 未选择知识库时的本地错误码：调用方（DocQA/图谱展开等）在发请求前拦截，
// 避免以无作用域请求触达后端（后端会返回 KB_SCOPE_REQUIRED）。
export const KB_NOT_SELECTED_CODE = 'KB_NOT_SELECTED';

export class KbScopeError extends Error {
  public code: string;

  constructor(message: string, code: string) {
    super(message);
    this.name = 'KbScopeError';
    this.code = code;
  }
}

// 读取当前 workspace 激活的知识库 id（未选择时为 null）。
export function getActiveKbId(): string | null {
  return useGraphStore.getState().activeKbId;
}

// 显式携带 kb_id 的调用前置：无 activeKbId 时抛本地错误，不发请求。
export function requireActiveKbId(): string {
  const kbId = useGraphStore.getState().activeKbId;
  if (!kbId) {
    throw new KbScopeError('请先选择知识库', KB_NOT_SELECTED_CODE);
  }
  return kbId;
}
