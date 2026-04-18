# 模块拆分方案（阶段二）

## 目标目录结构

```
astrbot_plugin_tmemory/
├── main.py                    # 插件入口，仅保留 @register + __init__ + 生命周期
├── core/
│   ├── __init__.py
│   ├── db.py                  # DB 连接管理、schema 初始化、迁移
│   ├── config.py              # 配置解析 + 安全默认值
│   ├── capture.py             # 消息采集 hooks + 过滤器
│   ├── distill.py             # 蒸馏调度 + LLM 蒸馏逻辑
│   ├── recall.py              # 记忆召回 + 注入逻辑
│   ├── identity.py            # 用户身份解析 + 跨适配器合并
│   ├── purify.py              # 记忆提纯 + 质量评分
│   └── commands.py            # /记忆 等用户命令处理
├── search/
│   ├── __init__.py
│   ├── fts.py                 # FTS5 全文检索
│   ├── vector.py              # sqlite-vec 向量检索 + embedding
│   ├── rerank.py              # Reranker 调用
│   └── hybrid.py              # RRF 融合排序（现 hybrid_search.py）
├── web_server.py              # WebUI（保持独立）
├── _conf_schema.json
└── metadata.yaml
```

## 各模块职责与接口

### `core/db.py` — 数据库管理

```python
class DatabaseManager:
    """管理 SQLite 持久连接、schema 初始化和迁移。"""

    def __init__(self, db_path: str, vec_module=None):
        ...

    def get_connection(self) -> sqlite3.Connection:
        """获取持久连接（线程安全）。"""

    def close(self):
        """关闭连接。terminate() 时调用。"""

    def init_schema(self):
        """创建所有表和索引。"""

    def migrate(self):
        """执行 schema 迁移。"""
```

### `core/config.py` — 配置管理

```python
@dataclass
class PluginConfig:
    """强类型配置对象，替代 dict 取值。"""

    cache_max_rows: int = 20
    memory_max_chars: int = 220
    enable_auto_capture: bool = True
    distill_interval_sec: int = 17280
    # ... 所有配置字段

    @classmethod
    def from_dict(cls, raw: dict) -> "PluginConfig":
        """从字典安全解析，失败字段使用默认值。"""
```

### `core/capture.py` — 消息采集

```python
class MessageCapture:
    """消息采集管理器：三层过滤 + 写入 conversation_cache。"""

    def __init__(self, config: PluginConfig, db: DatabaseManager): ...
    def should_skip(self, text: str) -> bool: ...
    def insert_conversation(self, canonical_id, role, content, **kw): ...
```

### `core/distill.py` — 蒸馏引擎

```python
class DistillEngine:
    """定时蒸馏调度 + LLM 批量蒸馏。"""

    def __init__(self, config: PluginConfig, db: DatabaseManager, llm_fn): ...
    async def worker_loop(self): ...
    async def distill_user(self, canonical_id: str): ...
```

### `core/recall.py` — 记忆召回

```python
class MemoryRecall:
    """记忆检索 + 注入 system prompt。"""

    def __init__(self, config: PluginConfig, db: DatabaseManager, search_engine): ...
    async def build_injection_block(self, canonical_id, query, limit, **kw) -> str: ...
    def inject_to_request(self, req: ProviderRequest, block: str): ...
```

### `core/identity.py` — 身份管理

```python
class IdentityResolver:
    """跨适配器用户身份解析与合并。"""

    def __init__(self, db: DatabaseManager): ...
    def resolve(self, event: AstrMessageEvent) -> Tuple[str, str, str]: ...
    def merge_users(self, from_id: str, to_id: str): ...
```

### `search/vector.py` — 向量检索

```python
class VectorSearch:
    """sqlite-vec 向量存储与检索。"""

    def __init__(self, db: DatabaseManager, config: PluginConfig): ...
    async def embed_text(self, text: str) -> Optional[List[float]]: ...
    async def search(self, query_vec, canonical_id, limit) -> List[dict]: ...
    def upsert_vector(self, memory_id: int, vector: List[float]): ...
```

### `search/rerank.py` — Reranker

```python
class Reranker:
    """调用外部 Rerank API 对候选记忆重排序。"""

    def __init__(self, config: PluginConfig): ...
    async def rerank(self, query: str, documents: List[str], top_n: int) -> List[int]: ...
```

## 迁移策略

1. **逐模块抽取**：每次只抽取一个模块，保证 main.py 始终可运行
2. **接口适配层**：抽取后在 main.py 中用 `self._xxx = XxxModule(...)` 代理调用
3. **向后兼容**：所有 public 方法签名不变，仅内部实现重构
4. **测试覆盖**：每个模块抽取后运行完整测试确认无回归

## 推荐迁移顺序

1. `core/db.py` — 最独立，零业务依赖
2. `core/config.py` — 消除 dict 取值散布
3. `core/identity.py` — 纯 DB 操作，依赖少
4. `core/capture.py` — 依赖 config + db + identity
5. `search/*` — 检索模块整体迁出
6. `core/distill.py` — 依赖 LLM 调用，最复杂
7. `core/recall.py` + `core/commands.py` — 最后迁出
