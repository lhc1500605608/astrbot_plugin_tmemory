# AstrBot 记忆插件重构技术方案

> ADR-001 | 状态：提议中 | 日期：2026-04-17

---

## 1. 背景与问题陈述

当前 `astrbot_plugin_tmemory` 是一个 3472 行的巨石单文件插件，存在以下架构级缺陷：

| # | 问题 | 影响 | 严重度 |
|---|------|------|--------|
| P1 | `_db()` 每次调用 `sqlite3.connect()`，`with conn` 不关闭连接 | 连接泄漏，fd 耗尽 | 🔴 高 |
| P2 | `__init__()` 中 `importlib` 加载 WebUI，失败则整插件不可用 | 单点故障 | 🔴 高 |
| P3 | `__init__()` 250+ 行无异常保护 | 任何配置错误 → 插件完全失败 | 🔴 高 |
| P4 | 3472 行单文件、80+ 方法、18 个命令处理器 | 不可维护 | 🟡 中 |
| P5 | 同步 SQLite 操作在 async 方法中直接调用 | 阻塞事件循环 | 🟡 中 |
| P6 | Reranker 引用未初始化属性 | 运行时 AttributeError | 🔴 高 |

**核心矛盾**：所有功能耦合在一个类中，任何子模块故障都导致整个插件不可用。

---

## 2. 设计目标与约束

### 2.1 设计目标

1. **故障隔离**：WebUI 崩溃不影响记忆采集；向量检索失败自动降级到 FTS
2. **模块可维护性**：单文件拆分为 < 500 行的模块，新人 1 小时可定位任意功能
3. **连接安全**：消除 DB 连接泄漏，引入连接池
4. **事件循环安全**：DB 操作不阻塞 asyncio
5. **100% 向后兼容**：现有命令、配置项、数据库 schema 全部保留

### 2.2 不做什么

- **不换数据库**：继续使用 SQLite，不引入 PostgreSQL/Redis
- **不换框架**：继续使用 AstrBot Star API，不引入 FastAPI 等
- **不拆微服务**：单进程模块化，不引入 RPC/消息队列
- **不新增功能**：纯重构，不加新特性

### 2.3 AstrBot 插件 API 约束

```python
# 必须保留：单一入口点
@register("tmemory", "shangtang", "...", "0.5.0")
class TMemoryPlugin(Star):
    # AstrBot 只认这一个类，它必须是 Star 的子类
    # 所有 @filter 装饰器必须在这个类的方法上
```

这意味着**插件入口类不可拆分**，但可以将实现委托给内部模块。

---

## 3. 新架构设计

### 3.1 目录结构

```
astrbot_plugin_tmemory/
├── main.py                    # 插件入口（~200 行）：注册 + 钩子 + 命令路由
├── _conf_schema.json          # 配置 schema（不变）
├── metadata.yaml              # 插件元数据（不变）
├── hybrid_search.py           # 混合检索（不变）
├── web_server.py              # WebUI 服务器（不变）
│
├── core/                      # 核心业务模块
│   ├── __init__.py
│   ├── config.py              # 配置解析与验证（~200 行）
│   ├── database.py            # DB 连接池 + schema 迁移（~300 行）
│   ├── identity.py            # 用户身份解析 + 跨适配器合并（~150 行）
│   ├── capture.py             # 消息采集 + 过滤器（~120 行）
│   ├── distill.py             # LLM 蒸馏引擎（~250 行）
│   ├── retrieval.py           # 记忆召回 + 注入构建（~200 行）
│   ├── vector.py              # 向量检索封装（~200 行）
│   └── purify.py              # 记忆提纯/合并/拆分（~250 行）
│
├── data/                      # 运行时数据（不变）
├── templates/                 # WebUI 模板（不变）
└── tools/                     # 辅助工具脚本（不变）
```

### 3.2 架构分层图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        main.py (Plugin Shell)                       │
│  @register + Star 子类                                              │
│  职责：生命周期、@filter 钩子路由、命令分发                              │
│  规则：不包含业务逻辑，只做 delegate                                   │
├─────────────┬──────────┬──────────┬──────────┬──────────────────────┤
│  capture    │ distill  │ retrieval│ purify   │ commands (in main)   │
│  消息采集    │ LLM 蒸馏  │ 记忆召回  │ 记忆提纯  │ 18 个 tm_* 命令     │
├─────────────┴──────────┴──────────┴──────────┴──────────────────────┤
│                    identity (用户身份层)                              │
│  canonical_id 解析、跨适配器绑定、合并                                 │
├────────────────────────────────────────────────────────────────────┤
│              database (数据访问层)                                    │
│  ConnectionPool + Repository 方法 + Schema 迁移                     │
│  ┌──────────┐  ┌──────────┐                                        │
│  │ vector   │  │ hybrid   │  (可选依赖，降级安全)                     │
│  │ sqlite-  │  │ _search  │                                        │
│  │ vec      │  │ .py      │                                        │
│  └──────────┘  └──────────┘                                        │
├────────────────────────────────────────────────────────────────────┤
│              config (配置层)                                        │
│  解析 + 校验 + 默认值 + 兼容旧名                                      │
├────────────────────────────────────────────────────────────────────┤
│              web_server (可选，隔离加载)                              │
│  独立 aiohttp 进程，失败不影响主插件                                   │
└────────────────────────────────────────────────────────────────────┘
```

### 3.3 依赖方向

```
main.py
  ├──→ core/config.py        （无其他依赖）
  ├──→ core/database.py      （依赖 config）
  ├──→ core/identity.py      （依赖 database）
  ├──→ core/capture.py       （依赖 database, identity, config）
  ├──→ core/distill.py       （依赖 database, identity, config）
  ├──→ core/retrieval.py     （依赖 database, identity, vector, config）
  ├──→ core/vector.py        （依赖 database, config）  ← 软依赖
  ├──→ core/purify.py        （依赖 database, identity, vector, config）
  └──→ web_server.py         （依赖 main.py 引用）      ← 隔离加载

✅ 单向依赖，无循环
✅ config / database 是底层，业务模块在上层
✅ vector 是软依赖，不可用时业务模块自动降级
```

---

## 4. 核心接口定义

### 4.1 配置层：`core/config.py`

```python
from dataclasses import dataclass, field
from typing import List, Optional
import re

@dataclass
class CaptureConfig:
    enable_auto_capture: bool = True
    capture_assistant_reply: bool = True
    capture_skip_prefixes: List[str] = field(default_factory=lambda: ["提醒 #"])
    capture_skip_regex: Optional[re.Pattern] = None

@dataclass
class DistillConfig:
    interval_sec: int = 17280
    min_batch_count: int = 20
    batch_limit: int = 80
    model_id: str = ""
    provider_id: str = ""
    pause: bool = False

@dataclass
class RetrievalConfig:
    enable_memory_injection: bool = True
    inject_position: str = "system_prompt"  # system_prompt | user_message_before | user_message_after | slot
    inject_slot_marker: str = "{{tmemory}}"
    inject_memory_limit: int = 5
    inject_max_chars: int = 0
    memory_scope: str = "user"  # user | session
    private_memory_in_group: bool = False

@dataclass
class VectorConfig:
    enable_vector_search: bool = False
    embed_provider_id: str = ""
    embed_model_id: str = ""
    embed_dim: int = 1536
    vector_weight: float = 0.4
    min_vector_sim: float = 0.15

@dataclass
class RerankerConfig:
    enable_reranker: bool = False
    rerank_provider_id: str = ""
    rerank_model_id: str = ""
    rerank_top_n: int = 5

@dataclass
class PurifyConfig:
    interval_days: int = 0
    model_id: str = ""
    min_score: float = 0.0
    default_mode: str = "both"   # merge | split | both
    default_limit: int = 20

@dataclass
class PluginConfig:
    """统一配置对象，从 dict 解析，带校验和默认值。"""
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    distill: DistillConfig = field(default_factory=DistillConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    vector: VectorConfig = field(default_factory=VectorConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    purify: PurifyConfig = field(default_factory=PurifyConfig)
    cache_max_rows: int = 20
    memory_max_chars: int = 220
    db_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict, plugin_data_dir: str) -> "PluginConfig":
        """从原始配置 dict 解析，异常安全，任何字段解析失败用默认值。"""
        ...
```

**设计决策**：

- 使用 `dataclass` 而非散装属性，IDE 可自动补全、类型检查
- `from_dict()` 内部逐字段 try/except，单个字段解析失败不影响其他字段
- 保留旧配置名兼容（`embed_model` → `embed_model_id`）

### 4.2 数据库层：`core/database.py`

```python
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional, List, Dict

class DatabasePool:
    """线程安全的 SQLite 连接池，解决连接泄漏问题。"""

    def __init__(self, db_path: str, max_connections: int = 4, vec_module=None):
        self._db_path = db_path
        self._max_connections = max_connections
        self._vec_module = vec_module
        self._pool: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._init_schema_done = False

    @contextmanager
    def connection(self) -> sqlite3.Connection:
        """获取连接，用完归还。保证 with 块结束后连接回池或关闭。

        用法:
            with db.connection() as conn:
                conn.execute("SELECT ...")
        """
        conn = self._acquire()
        try:
            yield conn
        finally:
            self._release(conn)

    def _acquire(self) -> sqlite3.Connection:
        with self._lock:
            if self._pool:
                return self._pool.pop()
        return self._create_connection()

    def _release(self, conn: sqlite3.Connection):
        with self._lock:
            if len(self._pool) < self._max_connections:
                self._pool.append(conn)
                return
        conn.close()  # 超出池大小，关闭多余连接

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        if self._vec_module:
            try:
                conn.enable_load_extension(True)
                self._vec_module.load(conn)
                conn.enable_load_extension(False)
            except Exception:
                pass
        return conn

    def close_all(self):
        with self._lock:
            for conn in self._pool:
                conn.close()
            self._pool.clear()

    def init_schema(self):
        """建表 + 迁移，只执行一次。"""
        ...

    def migrate_schema(self):
        """增量迁移（ALTER TABLE ADD COLUMN 等）。"""
        ...


class MemoryRepository:
    """记忆数据的 CRUD 操作，封装 SQL 细节。"""

    def __init__(self, db: DatabasePool):
        self._db = db

    def insert_memory(self, canonical_id: str, memory: str, **kwargs) -> int:
        ...

    def delete_memory(self, memory_id: int) -> bool:
        ...

    def list_memories(self, canonical_id: str, scope: str = "", ...) -> List[Dict]:
        ...

    def fetch_memory_by_id(self, memory_id: int) -> Optional[Dict]:
        ...

    def update_memory_text(self, memory_id: int, text: str):
        ...


class ConversationRepository:
    """对话缓存的 CRUD 操作。"""

    def __init__(self, db: DatabasePool):
        self._db = db

    def insert_conversation(self, canonical_id: str, role: str, content: str, **kwargs):
        ...

    def fetch_recent(self, canonical_id: str, limit: int) -> List[Dict]:
        ...

    def pending_distill_users(self, min_count: int) -> List[str]:
        ...

    def fetch_pending_rows(self, canonical_id: str, limit: int) -> List[Dict]:
        ...

    def mark_rows_distilled(self, ids: list):
        ...
```

**设计决策**：

| 方案 | 优点 | 缺点 | 决策 |
|------|------|------|------|
| A: 连接池 (threading.Lock + list) | 简单，无外部依赖 | 无连接健康检查 | ✅ 选用 |
| B: aiosqlite | 真正异步 | 需新依赖，API 变更大 | ❌ 延后 |
| C: SQLAlchemy | 功能强大 | 过重，插件场景不适合 | ❌ 否决 |

**关键改进**：
- `_db()` 每次新建连接 → `connection()` 上下文管理器，用完归还
- `with conn:` (只 commit/rollback) → `with db.connection() as conn:` (获取+归还)
- 所有 SQL 操作通过 Repository 类，main.py 不直接写 SQL

### 4.3 身份层：`core/identity.py`

```python
class IdentityService:
    """用户身份解析与跨适配器合并。"""

    def __init__(self, db: DatabasePool):
        self._db = db

    def resolve_identity(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        """返回 (canonical_id, adapter_name, adapter_user_id)。"""
        ...

    def bind_identity(self, adapter: str, adapter_user: str, canonical_id: str):
        ...

    def merge_identity(self, from_id: str, to_id: str) -> int:
        ...

    def get_memory_scope(self, event: AstrMessageEvent, scope_mode: str) -> str:
        ...

    def is_group_event(self, event: AstrMessageEvent) -> bool:
        ...
```

### 4.4 蒸馏引擎：`core/distill.py`

```python
class DistillEngine:
    """LLM 蒸馏：从对话缓存中提取长期记忆。"""

    def __init__(
        self,
        db: DatabasePool,
        conv_repo: ConversationRepository,
        mem_repo: MemoryRepository,
        config: DistillConfig,
        context: Context,          # AstrBot Context，用于 llm_generate
        vector_svc: Optional["VectorService"] = None,
    ):
        ...

    async def worker_loop(self):
        """后台蒸馏循环，替代原 _distill_worker_loop。"""
        ...

    async def run_distill_cycle(self, canonical_id: str = None):
        """执行一次蒸馏周期。"""
        ...

    async def distill_rows_with_llm(self, rows: List[Dict]) -> List[Dict]:
        """调用 LLM 从对话记录中提取记忆。"""
        ...

    def build_distill_prompt(self, transcript: str) -> str:
        ...
```

### 4.5 记忆召回：`core/retrieval.py`

```python
class RetrievalService:
    """记忆召回 + 注入构建。"""

    def __init__(
        self,
        db: DatabasePool,
        mem_repo: MemoryRepository,
        config: RetrievalConfig,
        vector_svc: Optional["VectorService"] = None,
        reranker_config: Optional[RerankerConfig] = None,
    ):
        ...

    async def retrieve_memories(
        self, query: str, canonical_id: str, scope: str, **kwargs
    ) -> List[Dict]:
        """检索相关记忆，自动选择 FTS / 向量 / 混合模式。"""
        ...

    async def build_injection_block(
        self, canonical_id: str, query: str, event: AstrMessageEvent
    ) -> str:
        """构建注入到 LLM 请求的记忆文本块。"""
        ...
```

### 4.6 向量服务：`core/vector.py`

```python
class VectorService:
    """向量检索封装，软依赖 sqlite-vec。

    如果 sqlite-vec 不可用，所有方法返回空结果 / False，不抛异常。
    """

    def __init__(self, db: DatabasePool, config: VectorConfig, context: Context):
        self._available = False
        try:
            import sqlite_vec
            self._sqlite_vec = sqlite_vec
            self._available = True
        except ImportError:
            pass
        ...

    @property
    def available(self) -> bool:
        return self._available

    async def embed_text(self, text: str) -> Optional[List[float]]:
        ...

    async def upsert_vector(self, memory_id: int, text: str) -> bool:
        ...

    def delete_vector(self, memory_id: int):
        ...

    async def rebuild_index(self) -> tuple[int, int]:
        ...
```

---

## 5. 错误处理与降级策略

### 5.1 分层防御

```
Layer 0: __init__() 防御
  ├─ 配置解析：每个字段独立 try/except，用默认值兜底
  ├─ WebUI 加载：try/except，失败只 log warning，self._web_server = NullWebServer()
  └─ sqlite-vec 加载：try/except，失败设 _available = False

Layer 1: initialize() 防御
  ├─ DB init/migrate：失败 → 插件 raise（数据库不可用确实不应继续）
  ├─ WebUI start：try/except，失败 log error，插件继续
  └─ Worker 启动：asyncio.create_task，任务内部自带重试

Layer 2: 运行时防御
  ├─ 消息采集（on_any_message）：外层 try/except，失败 log，不影响消息流
  ├─ 记忆注入（on_llm_request）：失败 → 不注入，不影响 LLM 调用
  ├─ 蒸馏循环：单用户失败 → 跳过，不影响其他用户
  └─ 向量操作：失败 → 降级到纯 FTS
```

### 5.2 NullObject 模式

```python
class NullWebServer:
    """WebUI 加载失败时的替身，所有方法静默返回。"""
    async def start(self): pass
    async def stop(self): pass

class NullVectorService:
    """向量不可用时的替身。"""
    available = False
    async def embed_text(self, text): return None
    async def upsert_vector(self, memory_id, text): return False
    def delete_vector(self, memory_id): pass
    async def rebuild_index(self): return (0, 0)
```

### 5.3 回滚策略

| 场景 | 回滚方式 |
|------|---------|
| 蒸馏失败（LLM 超时） | 不 `mark_rows_distilled`，下次重新蒸馏 |
| 向量写入失败 | 记忆本身已写入 memories 表，向量丢失不影响 FTS 召回 |
| 记忆合并/拆分失败 | 事务回滚，原记忆不变 |
| WebUI 崩溃 | 自动重启（可选），或等管理员手动检查 |
| DB 迁移失败 | ALTER TABLE 是 DDL，SQLite 自动回滚失败的 statement |

---

## 6. 组装方式：依赖注入

### 6.1 手动构造注入（无框架）

```python
# main.py 中的组装逻辑

@register("tmemory", "shangtang", "...", "0.5.0")
class TMemoryPlugin(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)

        # Layer 0: 配置（异常安全）
        self.cfg = PluginConfig.from_dict(config or {}, self._get_data_dir())

        # Layer 1: 数据库
        vec_module = self._try_load_sqlite_vec() if self.cfg.vector.enable_vector_search else None
        self._db = DatabasePool(self.cfg.db_path, vec_module=vec_module)

        # Layer 2: Repository
        self._mem_repo = MemoryRepository(self._db)
        self._conv_repo = ConversationRepository(self._db)

        # Layer 3: 服务（按需构建，异常安全）
        self._identity = IdentityService(self._db)
        self._capture = CaptureService(self._db, self._conv_repo, self.cfg.capture)
        self._vector = self._try_build_vector_service()
        self._retrieval = RetrievalService(
            self._db, self._mem_repo, self.cfg.retrieval,
            vector_svc=self._vector, reranker_config=self.cfg.reranker,
        )
        self._distill = DistillEngine(
            self._db, self._conv_repo, self._mem_repo, self.cfg.distill,
            context=self.context, vector_svc=self._vector,
        )
        self._purify = PurifyService(
            self._db, self._mem_repo, self.cfg.purify,
            context=self.context, vector_svc=self._vector,
        )

        # Layer 4: WebUI（隔离加载）
        self._web_server = self._try_build_web_server()

    def _try_build_vector_service(self):
        """向量服务构建失败 → 返回 NullVectorService。"""
        if not self.cfg.vector.enable_vector_search:
            return NullVectorService()
        try:
            return VectorService(self._db, self.cfg.vector, self.context)
        except Exception as e:
            logger.warning("[tmemory] VectorService init failed: %s", e)
            return NullVectorService()

    def _try_build_web_server(self):
        """WebUI 加载失败 → 返回 NullWebServer。"""
        try:
            from .web_server import TMemoryWebServer
            webui_cfg = dict(self.cfg.__dict__)  # 展平传递
            return TMemoryWebServer(self, webui_cfg)
        except Exception as e:
            logger.warning("[tmemory] WebUI load failed (non-fatal): %s", e)
            return NullWebServer()
```

**设计决策**：

| 方案 | 优点 | 缺点 | 决策 |
|------|------|------|------|
| A: 手动构造注入 | 零依赖，显式，易调试 | 组装代码稍长 | ✅ 选用 |
| B: DI 框架 (injector/dependency-injector) | 自动解析依赖图 | 新依赖，团队学习成本 | ❌ 过重 |
| C: Service Locator | 灵活 | 隐式依赖，难测试 | ❌ 否决 |

### 6.2 重构后 main.py 骨架

```python
@register("tmemory", "shangtang", "...", "0.5.0")
class TMemoryPlugin(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # ... 组装代码（见上方）

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def initialize(self):
        self._db.init_schema()
        self._db.migrate_schema()
        self._distill.start_worker()
        await self._web_server.start()

    async def terminate(self):
        self._distill.stop_worker()
        await self._web_server.stop()
        self._db.close_all()

    # ── 消息钩子（纯路由，不含业务逻辑） ──────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        try:
            await self._capture.on_message(event)
        except Exception as e:
            logger.debug("[tmemory] capture error: %s", e)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            await self._capture.on_llm_response(event, resp)
        except Exception as e:
            logger.debug("[tmemory] capture response error: %s", e)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            await self._retrieval.inject_memory(event, req)
        except Exception as e:
            logger.debug("[tmemory] injection error: %s", e)

    # ── 命令（纯路由 → 各服务） ──────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_distill_now")
    async def tm_distill_now(self, event: AstrMessageEvent):
        result = await self._distill.run_distill_cycle()
        event.set_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_stats")
    async def tm_stats(self, event: AstrMessageEvent):
        stats = self._build_stats()  # 聚合各服务的统计信息
        event.set_result(stats)

    # ... 其余 16 个命令同样路由到对应服务
```

---

## 7. 同步 DB → 异步事件循环 的处理策略

### 7.1 方案选择

| 方案 | 改动量 | 风险 | 决策 |
|------|--------|------|------|
| A: `asyncio.to_thread()` 包装同步 DB 调用 | 低 | 低，Python 3.9+ 内置 | ✅ 阶段一选用 |
| B: aiosqlite | 高，所有 DB 调用改 async | 中 | ⏳ 阶段二考虑 |

### 7.2 实施模式

```python
# core/database.py 中提供异步包装

class DatabasePool:
    ...

    async def execute_async(self, func, *args, **kwargs):
        """将同步 DB 操作放到线程池执行，不阻塞事件循环。"""
        return await asyncio.to_thread(func, *args, **kwargs)


# 业务层使用示例
class DistillEngine:
    async def run_distill_cycle(self, ...):
        # 将同步的 DB 查询放到线程池
        users = await self._db.execute_async(
            self._conv_repo.pending_distill_users, self._config.min_batch_count
        )
        ...
```

---

## 8. 向后兼容性保障

### 8.1 配置兼容

```python
# core/config.py 中处理新旧配置名映射
_COMPAT_KEYS = {
    "embed_model": "embed_model_id",
    "rerank_model": "rerank_model_id",
    "refine_quality_interval_days": "purify_interval_days",
    "refine_quality_model_id": "purify_model_id",
    "refine_quality_min_score": "purify_min_score",
    "manual_refine_default_mode": "manual_purify_default_mode",
    "manual_refine_default_limit": "manual_purify_default_limit",
}

@classmethod
def from_dict(cls, raw: dict, ...) -> "PluginConfig":
    # 先做 key 映射
    normalized = {}
    for k, v in raw.items():
        new_key = _COMPAT_KEYS.get(k, k)
        if new_key not in normalized:  # 新名优先
            normalized[new_key] = v
    ...
```

### 8.2 数据库兼容

- 不改表结构，只改访问方式
- `_migrate_schema()` 逻辑原样迁移到 `DatabasePool.migrate_schema()`
- 现有 `data/*.db` 文件无需迁移

### 8.3 命令兼容

- 所有 18 个 `tm_*` 命令保持不变，签名不变
- 内部从 `self._xxx_method()` 改为 `self._service.method()`

### 8.4 WebUI 兼容

- `web_server.py` 对外接口不变
- 将 `importlib` 动态加载改为正常的 `from .web_server import TMemoryWebServer`
- `TYPE_CHECKING` 中的 `from main import TMemoryPlugin` 保持不变

---

## 9. 迁移路径（分阶段实施）

### 阶段一：修复关键缺陷（优先级最高）

**目标**：消除"插件无法使用"的风险

1. 引入 `DatabasePool`，替换 `_db()` 方法 → 修复连接泄漏 (P1)
2. WebUI 加载加 try/except + NullWebServer → 修复单点故障 (P2)
3. `__init__()` 配置解析加逐字段 try/except → 修复初始化崩溃 (P3)
4. 补全 Reranker 缺失属性初始化 → 修复 AttributeError (P6)

**验证标准**：配置错误、WebUI 缺失、sqlite-vec 未安装时插件仍可启动

### 阶段二：模块拆分

**目标**：消除巨石单文件

1. 创建 `core/` 目录
2. 抽取 `config.py` → `database.py` → `identity.py`（底层先行）
3. 抽取 `capture.py` → `distill.py` → `retrieval.py` → `vector.py` → `purify.py`
4. 重写 `main.py` 为纯路由层

**验证标准**：所有命令、钩子功能与重构前行为一致

### 阶段三：性能优化

**目标**：解除事件循环阻塞

1. DB 操作用 `asyncio.to_thread()` 包装
2. embedding 并发控制（已有 `_embed_semaphore`）
3. 连接池健康检查

**验证标准**：高并发下事件循环不阻塞，响应延迟 < 500ms

---

## 10. 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| AstrBot 不识别 `core/` 子包中的模块 | 低 | 高 | 所有 `@filter` 装饰器保留在 main.py 入口类上 |
| 重构引入回归 Bug | 中 | 中 | 逐模块迁移 + 每步手动验证命令 |
| `asyncio.to_thread()` 与 SQLite 线程安全冲突 | 低 | 中 | `check_same_thread=False` + 连接池锁 |
| 配置兼容性遗漏 | 低 | 中 | 枚举所有旧名，写映射表 |

---

## 11. 成功指标

- [ ] 插件在 WebUI 缺失 / sqlite-vec 缺失 / 配置错误时仍可启动和响应消息
- [ ] `main.py` < 300 行，单个核心模块 < 500 行
- [ ] 所有 18 个 `tm_*` 命令功能不变
- [ ] DB 连接数 ≤ 4（连接池上限），无泄漏
- [ ] 现有 `_conf_schema.json` 配置项 100% 向后兼容
- [ ] 现有数据库无需迁移操作
