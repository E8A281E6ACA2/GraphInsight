"""
NL2Cypher 服务
将自然语言转换为 Cypher 查询

M4（契约 §16 M4 / §12.3）：
- 生成查询必须携带知识库作用域：节点匹配统一注入 kb_id IN $authorized_kb_ids；
- 仅允许受限只读查询：包含写关键字（CREATE/MERGE/DELETE/SET/DROP/LOAD/CALL 等）直接拒绝；
- LRU 缓存键包含 kb 作用域，避免跨 KB 复用同一条生成结果。
"""
from typing import Dict, List, Optional, Tuple
import json
import re
from functools import lru_cache
from config import get_settings
from services.openai_client_factory import build_async_openai_client
from services.runtime_config import get_ai_runtime_config, get_nl2cypher_runtime_config
from services.schema_service import SchemaService
from services.scope_contract import require_kb_scope


# 受限只读：生成 Cypher 中出现以下写关键字一律拒绝（词边界匹配，避免 dataset/OFFSET 误伤）
_CYPHER_WRITE_PATTERN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|CALL)\b",
    re.IGNORECASE,
)


class CypherNotPermittedError(ValueError):
    """生成的 Cypher 含写操作或不允许的调用，被只读策略拒绝。"""


class NL2CypherService:
    """将自然语言转换为 Cypher 查询的服务"""

    def __init__(self):
        self.settings = get_settings()
        self.schema_service = SchemaService()

    def _get_ai_client(self):
        """获取 AI 客户端（优先运行时配置，回退环境变量）"""
        config = get_ai_runtime_config()
        api_key = str(config.get("api_key") or "").strip() or self.settings.openai_api_key
        base_url = str(config.get("base_url") or "").strip() or None
        return build_async_openai_client(api_key=api_key, base_url=base_url, timeout=30.0)

    def _get_nl2cypher_config(self) -> dict:
        """获取 NL2Cypher 配置（优先运行时配置，回退环境变量）"""
        return get_nl2cypher_runtime_config()

    def _get_ai_params(self) -> dict:
        """获取 AI 参数（优先运行时配置，回退环境变量）"""
        config = get_ai_runtime_config()
        return {
            "model": config["model"],
            "temperature": config["temperature"],
            "max_tokens": config["max_tokens"],
        }

    async def convert(
        self,
        natural_language: str,
        context: Optional[Dict] = None,
        *,
        kb_ids: List[str],
    ) -> Dict:
        """
        将自然语言转换为 Cypher 查询（M4：kb_ids 必填，生成查询强制注入作用域）

        Args:
            natural_language: 用户输入的自然语言
            context: 上下文信息（可选）
            kb_ids: 授权知识库列表（缺失/为空 -> KB_SCOPE_REQUIRED）

        Returns:
            包含 Cypher 查询、作用域参数和解释的字典
        """
        # 作用域强制点：无 kb 不生成任何查询
        authorized_kb_ids = require_kb_scope(kb_ids)
        # 检查配置（优先使用数据库配置）
        nl2cypher_config = self._get_nl2cypher_config()
        if not nl2cypher_config.get("enabled", True):
            return {
                "success": False,
                "error": "NL2Cypher 功能未启用"
            }

        # 验证 AI API Key
        ai_config = get_ai_runtime_config()
        api_key = str(ai_config.get("api_key") or "").strip()
        enabled = bool(ai_config.get("enabled", True))

        if not enabled:
            return {
                "success": False,
                "error": "AI 服务未启用"
            }

        if not api_key:
            return {
                "success": False,
                "error": "AI API Key 未配置"
            }

        try:
            # 构建 prompt
            prompt = await self._build_prompt(natural_language, context)

            # 调用 LLM
            response = await self._call_llm(prompt)

            # 解析响应
            result = self._parse_response(response)

            # 验证和优化 Cypher（含只读策略）
            result['cypher'] = self._validate_and_fix_cypher(result['cypher'])

            # 注入知识库作用域（M4）：所有节点匹配限定在授权 kb 内
            result['cypher'] = self._inject_kb_scope(result['cypher'], authorized_kb_ids)
            result['authorized_kb_ids'] = list(authorized_kb_ids)
            result['scope_parameters'] = {"authorized_kb_ids": list(authorized_kb_ids)}

            result['success'] = True
            return result

        except CypherNotPermittedError as e:
            print(f"[ERROR] NL2Cypher 只读策略拒绝: {e}")
            return {
                "success": False,
                "error": f"生成的查询被拒绝（仅允许只读查询）: {str(e)}",
                "suggestions": [
                    "请描述你想查询的内容，例如'查找某实体及其关联节点'",
                    "系统仅允许 MATCH...RETURN 形式的只读查询",
                ],
            }
        except Exception as e:
            import traceback
            print(f"[ERROR] NL2Cypher conversion failed: {e}")
            traceback.print_exc()
            return {
                "success": False,
                "error": f"生成失败: {str(e)}",
                "suggestions": [
                    "请更具体地描述你想查询的内容",
                    "尝试使用示例格式：'查找 [节点类型] 和它的 [关系]'",
                    "检查 OpenAI API Key 是否正确配置"
                ]
            }

    async def _build_prompt(
        self,
        natural_language: str,
        context: Optional[Dict]
    ) -> List[Dict]:
        """构建 LLM prompt"""

        # 获取 Schema 信息（同步调用）
        schema_summary = self.schema_service.get_schema_summary()

        # 获取最大限制配置
        nl2cypher_config = self._get_nl2cypher_config()
        max_limit = nl2cypher_config.get("max_limit", 100)

        # System Prompt
        system_prompt = f"""你是一个 Neo4j Cypher 查询生成器。根据用户的自然语言生成精确的 Cypher 查询。

数据库 Schema:
{schema_summary}

重要规则：
1. 必须根据用户提到的具体名称生成查询，如用户说"小麦"，则查询 {{name: '小麦'}}
2. 查询格式：MATCH (n {{name: '具体名称'}})-[r]-(m) RETURN n, r, m LIMIT {max_limit}
3. 中文属性值必须用单引号包围
4. 必须返回节点和关系：RETURN n, r, m
5. 只允许只读查询（MATCH...RETURN），禁止任何写操作（CREATE/MERGE/DELETE/SET/DROP/LOAD/CALL 等）
6. 知识库作用域（kb_id 过滤）由平台自动注入 $authorized_kb_ids 参数，不要自行添加 kb 条件

输出格式（严格JSON，不要添加任何其他文字）：
{{"cypher": "MATCH (n {{name: '用户提到的名称'}})-[r]-(m) RETURN n, r, m LIMIT {max_limit}", "explanation": "查询说明", "confidence": 0.9}}"""

        # User Prompt
        user_prompt = f"""用户查询：{natural_language}

请根据用户查询中提到的具体名称（如"小麦"、"郑麦136"等）生成精确的 Cypher 查询。"""

        # 添加上下文信息
        if context:
            if context.get("recent_queries"):
                user_prompt += f"\n\n最近查询历史：\n{context['recent_queries']}"

        # 构建消息列表
        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # 添加 Few-shot 示例
        examples = self._get_examples()
        for example in examples:
            messages.append({"role": "user", "content": example["nl"]})
            messages.append({"role": "assistant", "content": json.dumps({
                "cypher": example["cypher"],
                "explanation": example["explanation"],
                "confidence": 0.95
            }, ensure_ascii=False)})

        # 添加用户查询
        messages.append({"role": "user", "content": user_prompt})

        return messages

    def _get_examples(self) -> List[Dict]:
        """获取 Few-shot 示例"""
        return [
            {
                "nl": "查找郑麦136的相关节点信息",
                "cypher": "MATCH (n {name: '郑麦136'})-[r]-(m) RETURN n, r, m LIMIT 50",
                "explanation": "查询名为'郑麦136'的节点及其所有相关联的节点和关系"
            },
            {
                "nl": "显示小麦和它的病害",
                "cypher": "MATCH (c {name: '小麦'})-[r]-(d) RETURN c, r, d LIMIT 50",
                "explanation": "查询小麦节点及其所有关联的节点和关系"
            },
            {
                "nl": "查找所有作物",
                "cypher": "MATCH (n:Crop)-[r]-(m) RETURN n, r, m LIMIT 50",
                "explanation": "查询所有Crop类型的节点及其关联的节点和关系"
            },
            {
                "nl": "找出影响玉米的害虫",
                "cypher": "MATCH (c {name: '玉米'})-[r]-(p) RETURN c, r, p LIMIT 50",
                "explanation": "查询玉米节点及其所有关联的节点"
            },
        ]

    async def _call_llm(self, messages: List[Dict]) -> str:
        """调用 LLM API"""
        # 获取 AI 客户端
        client = self._get_ai_client()

        # 获取 AI 参数
        params = self._get_ai_params()

        response = await client.chat.completions.create(
            model=params["model"],
            messages=messages,
            temperature=params["temperature"],
            max_tokens=params["max_tokens"]
        )
        return response.choices[0].message.content

    def _parse_response(self, response: str) -> Dict:
        """解析 LLM 响应"""
        print(f"[DEBUG] AI 原始响应: {response}")

        # 清理响应文本
        cleaned = response.strip()

        # 移除可能的 markdown 代码块
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
        cleaned = cleaned.strip()

        try:
            # 尝试直接解析 JSON
            result = json.loads(cleaned)
            print(f"[DEBUG] JSON 解析成功: {result}")
            return result
        except json.JSONDecodeError as e:
            print(f"[DEBUG] JSON 解析失败: {e}, 清理后内容: {cleaned[:200]}")

            # 尝试从响应中提取 JSON（更宽松的匹配）
            json_match = re.search(r'\{.*?"cypher"\s*:\s*"([^"]+)".*?\}', cleaned, re.DOTALL)
            if json_match:
                try:
                    # 尝试解析整个 JSON
                    json_str = re.search(r'\{[^{}]*\}', cleaned, re.DOTALL)
                    if json_str:
                        result = json.loads(json_str.group())
                        print(f"[DEBUG] 从文本中提取 JSON 成功: {result}")
                        return result
                except Exception as ex:
                    print(f"[DEBUG] JSON 提取失败: {ex}")
                    # 直接提取 cypher 值
                    cypher_value = json_match.group(1)
                    print(f"[DEBUG] 直接提取 cypher 值: {cypher_value}")
                    return {
                        "cypher": cypher_value,
                        "explanation": "自动生成的查询",
                        "confidence": 0.7
                    }

            # 如果不是 JSON，尝试提取 Cypher
            cypher = self._extract_cypher(response)
            print(f"[DEBUG] 提取的 Cypher: {cypher}")
            return {
                "cypher": cypher,
                "explanation": "自动生成的查询",
                "confidence": 0.7
            }

    def _extract_cypher(self, text: str) -> str:
        """从文本中提取 Cypher 查询"""
        print(f"[DEBUG] _extract_cypher 输入: {text[:300]}")

        # 尝试提取代码块中的内容
        code_block_match = re.search(r'```(?:cypher)?\s*(.*?)\s*```', text, re.DOTALL)
        if code_block_match:
            extracted = code_block_match.group(1).strip()
            print(f"[DEBUG] 从代码块提取: {extracted}")
            return extracted

        # 尝试查找 MATCH 语句（宽松匹配）
        match_pattern = re.search(r'(MATCH\s*\(.+?RETURN\s+.+?)(?:LIMIT\s+\d+)?', text, re.IGNORECASE | re.DOTALL)
        if match_pattern:
            extracted = match_pattern.group(0).strip()
            print(f"[DEBUG] 提取 MATCH-RETURN 语句: {extracted}")
            return extracted

        # 更宽松：只找 MATCH 开头的内容
        match_only = re.search(r'(MATCH\s+.+)', text, re.IGNORECASE | re.DOTALL)
        if match_only:
            extracted = match_only.group(1).strip()
            # 清理尾部
            extracted = re.split(r'\n\n|\n(?=[^A-Z\s])', extracted)[0]
            print(f"[DEBUG] 提取 MATCH 语句: {extracted}")
            return extracted

        print(f"[DEBUG] 无法提取 Cypher，返回原文本")
        return text.strip()

    def _validate_and_fix_cypher(self, cypher: str) -> str:
        """验证和修复 Cypher 语法"""
        print(f"[DEBUG] _validate_and_fix_cypher 输入: {cypher}")

        # 移除末尾的分号
        cypher = cypher.rstrip(';').strip()

        # 移除可能的 markdown 代码块标记
        cypher = re.sub(r'^```(?:cypher)?\s*', '', cypher)
        cypher = re.sub(r'\s*```$', '', cypher)
        cypher = cypher.strip()

        # 检查是否包含写操作（M4：受限只读策略）
        write_match = _CYPHER_WRITE_PATTERN.search(cypher)
        if write_match:
            raise CypherNotPermittedError(f"不允许的操作：{write_match.group(0).upper()}")

        # 基本语法验证
        if not cypher.upper().strip().startswith('MATCH'):
            print(f"[DEBUG] Cypher 不以 MATCH 开头，尝试提取...")
            # 尝试提取 MATCH 语句
            match = re.search(r'(MATCH\s+.+)', cypher, re.IGNORECASE | re.DOTALL)
            if match:
                cypher = match.group(1).strip()
                print(f"[DEBUG] 从响应中提取 MATCH 语句: {cypher}")
            else:
                print(f"[ERROR] 无法找到 MATCH 语句，原始内容: {cypher}")
                # 如果完全没有 MATCH，生成一个默认查询（包含关系）
                if not cypher or cypher.lower() in ['none', 'null', '']:
                    print(f"[DEBUG] 生成默认查询")
                    return "MATCH (n)-[r]-(m) RETURN n, r, m LIMIT 50"
                raise ValueError("无效的 Cypher 查询：必须以 MATCH 开头")

        # 确保有 RETURN 语句
        if 'RETURN' not in cypher.upper():
            # 尝试添加默认的 RETURN
            # 提取变量名
            var_match = re.search(r'MATCH\s*\((\w+)', cypher, re.IGNORECASE)
            if var_match:
                var_name = var_match.group(1)
                cypher += f' RETURN {var_name}'
            else:
                raise ValueError("无效的 Cypher 查询：缺少 RETURN 语句")

        # 获取最大限制配置
        nl2cypher_config = self._get_nl2cypher_config()
        max_limit = nl2cypher_config.get("max_limit", 100)

        # 确保有 LIMIT
        if 'LIMIT' not in cypher.upper():
            cypher += f' LIMIT {max_limit}'

        # 验证 LIMIT 不超过最大值
        limit_match = re.search(r'LIMIT\s+(\d+)', cypher, re.IGNORECASE)
        if limit_match:
            limit_value = int(limit_match.group(1))
            if limit_value > max_limit:
                cypher = re.sub(
                    r'LIMIT\s+\d+',
                    f'LIMIT {max_limit}',
                    cypher,
                    flags=re.IGNORECASE
                )

        # 验证括号匹配
        if cypher.count('(') != cypher.count(')'):
            raise ValueError("无效的 Cypher 查询：括号不匹配")
        if cypher.count('[') != cypher.count(']'):
            raise ValueError("无效的 Cypher 查询：方括号不匹配")
        if cypher.count('{') != cypher.count('}'):
            raise ValueError("无效的 Cypher 查询：花括号不匹配")

        return cypher

    def _inject_kb_scope(self, cypher: str, authorized_kb_ids: List[str]) -> str:
        """在生成的 Cypher 中注入知识库作用域（M4 / M4-R1 FIX #5）。

        受限只读策略：仅支持单一 `MATCH ... [RETURN]` 形态，并对其中每个节点变量
        追加 `kb_id IN $authorized_kb_ids`。任何无法证明“所有匹配都带作用域”的复杂
        结构（UNION / OPTIONAL MATCH / 多段 MATCH / CALL{} 子查询 / 模式推导式等）
        一律拒绝，而不是尽力注入后放行（旧正则只处理首个 MATCH 片段，UNION 等后续
        分支可能没有注入，属于安全边界漏注入）。执行方必须绑定 $authorized_kb_ids
        参数（见 convert() 返回的 scope_parameters）。
        """
        if not authorized_kb_ids:
            raise ValueError("NL2Cypher 缺少授权 kb 作用域，拒绝执行")

        compact = re.sub(r"\s+", " ", cypher)
        upper = compact.upper()

        # 1) 结构拒绝：无法证明完整作用域覆盖的复杂形态直接拒绝（fail-closed）。
        unsupported_patterns = (
            (r"\bUNION\b", "UNION 多分支"),
            (r"\bOPTIONAL\b", "OPTIONAL MATCH"),
            (r"\bCALL\s*\{", "CALL 子查询"),
            (r"\bLOAD\s+CSV\b", "LOAD CSV"),
            (r"\bFOREACH\b", "FOREACH"),
            (r"\bCOUNT\s*\{", "COUNT 计数子查询"),
            (r"\[\s*\(", "模式推导式（pattern comprehension）"),
        )
        for pattern, reason in unsupported_patterns:
            if re.search(pattern, upper):
                raise ValueError(f"NL2Cypher 作用域注入不支持 {reason}，已拒绝执行")
        if len(re.findall(r"\bMATCH\b", upper)) != 1:
            raise ValueError("NL2Cypher 作用域注入只支持单一 MATCH，已拒绝执行")

        # 2) 定位单一 MATCH 片段（到第一个 RETURN/WITH/ORDER BY/SKIP/LIMIT/UNWIND）。
        end_match = len(cypher)
        for keyword in ("WITH ", "RETURN ", "ORDER BY", "SKIP ", "LIMIT ", "UNWIND "):
            idx = upper.find(keyword)
            if idx != -1:
                end_match = min(end_match, idx)
        match_section = cypher[:end_match]

        # 3) 收集节点变量；无节点变量时退化为关系变量；仍为空则无法限定 -> 拒绝。
        variables = sorted(
            {v for v in re.findall(r"\((\w+)", match_section) if v.lower() != "match"}
        )
        if not variables:
            variables = sorted(set(re.findall(r"\[(\w+)", match_section)))
        if not variables:
            raise ValueError("NL2Cypher 作用域注入未能识别任何可限定变量，已拒绝执行")

        # 4) 注入谓词（若已存在 $authorized_kb_ids 则不重复注入，稍后仍校验覆盖度）。
        if "$authorized_kb_ids" not in cypher:
            predicate = " AND ".join(f"{var}.kb_id IN $authorized_kb_ids" for var in variables)
            if re.search(r"\bWHERE\b", match_section, re.IGNORECASE):
                injected = cypher[:end_match].rstrip() + f" AND {predicate} " + cypher[end_match:].lstrip()
            else:
                injected = cypher[:end_match].rstrip() + f" WHERE {predicate} " + cypher[end_match:].lstrip()
        else:
            injected = cypher

        # 5) 覆盖度自校验：每个匹配变量都必须真正带上作用域谓词，否则拒绝。
        for var in variables:
            if re.search(
                rf"\b{re.escape(var)}\.kb_id\s+IN\s+\$authorized_kb_ids",
                injected,
                re.IGNORECASE,
            ) is None:
                raise ValueError(f"NL2Cypher 作用域注入未能覆盖变量 {var}，已拒绝执行")

        return injected

    @lru_cache(maxsize=100)
    def get_cached_conversion(self, natural_language: str, kb_ids: Tuple[str, ...] = ()) -> Optional[Dict]:
        """获取缓存的转换结果（M4：缓存键包含 kb 作用域，禁止跨 KB 复用）。"""
        # 这个方法会被 lru_cache 装饰器自动缓存
        return None
