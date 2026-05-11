# ADR-009: Module Boundary and Import Split for Hot Files

- **Status:** Accepted
- **Date:** 2026-05-11
- **Driver:** TMEAAA-335/343 — v0.9.0 "质量与收敛"

## Context

v0.9.0 四个热点文件超出可维护范围：

| File | Lines | Primary Content |
|------|-------|-----------------|
| `core/utils.py` | 1544 | MemoryLogger + PluginHelpersMixin (~810) + PluginHandlersMixin (~662) |
| `core/admin_service.py` | 1003 | AdminService 应用服务 (~936) + 4 个模块级纯函数 |
| `core/consolidation.py` | 1049 | EpisodeManager + SemanticExtractor + ConsolidationRuntimeMixin + ProfileExtractor + ProfileExtractionRuntimeMixin |
| `web_server.py` | 742 | JWT 工具 + TMemoryWebServer (~656, 含 ~30 个路由处理器) |

现有的 `MODULE_SPLIT_PLAN.md` 偏阶段二愿景（将 mixin 转为独立 DI 服务），不适合 v0.9.0 直接执行。核心制约：
- **Mixin 模式深度绑定**：`PluginHelpersMixin`、`PluginHandlersMixin`、`ConsolidationRuntimeMixin`、`ProfileExtractionRuntimeMixin` 通过 `self._*` 直接访问 `TMemoryPlugin` 的状态（`_cfg`、`_db()`、`_normalize_text()`、`self.context`等）。改成 DI 服务超出本任务范围。
- **AdminService 虽非 mixin，但其 20+ 方法共享 `_db_mgr`、`_cfg`、`_identity_mgr` 等实例引用**，从一个类拆多个 service 需要接口变更。

## Decision

采用 **物理拆分 + 保持 mixin/facade 模式不变** 的策略。每步只做"代码搬迁 + 逆向 facade"，不做行为重构。

### 总体原则

1. **物理隔离**：每个逻辑单元独立文件，降低合并冲突概率和认知负载。
2. **逆向兼容 facade**：原文件保留为薄层 re-export，`from .core.utils import PluginHelpersMixin` 等外部引用不中断。
3. **不走 DI 重构**：mixin 访问 `self._*` 的模式保留，不做构造函数注入。
4. **去重**：利用搬迁机会消除 `_CMD_FIRST_WORDS` 在 `main.py` 和 `utils.py` 间的重复。
5. **四阶段执行**：每阶段独立可验证。

---

## 1. core/utils.py (1544 → 4 文件)

### 子模块边界

| 目标文件 | 职责 | 提取来源 | 估算行数 |
|----------|------|----------|----------|
| `core/memory_logger.py` | MemoryLogger：记忆审计日志写入 | utils.py:1-37 | ~35 |
| `core/helpers.py` | PluginHelpersMixin：DB CRUD、向量操作、事件内省、文本工具、上下文构建、统计 | utils.py:44-881 | ~580 |
| `core/handlers.py` | PluginHandlersMixin：消息采集 hook、AI tool handler、全部 `_handle_tm_*` 命令实现 | utils.py:883-1544 | ~520 |
| `core/utils.py` | **Facade**：re-export MemoryLogger、PluginHelpersMixin、PluginHandlersMixin + 保留 `_CMD_FIRST_WORDS` | — | ~20 |

### 需要解决的重复

`_CMD_FIRST_WORDS` 当前同时定义在 `main.py:20-42` 和 `core/utils.py:64-69`。搬迁时将 `utils.py` 中的定义删除，`PluginHandlersMixin._handle_on_any_message` 内部改为引用 `self._cmd_first_words` ——插件构造函数统一在一处构造。

### 导入方向

```
main.py → core/memory_logger.py     (OK, usings)
main.py → core/helpers.py           (OK, mixin inheritance)
main.py → core/handlers.py          (OK, mixin inheritance)
main.py → core/utils.py             (facade, 与上面等价)
core/helpers.py → core/memory_logger.py  (NO, helpers 不用 MemoryLogger)
core/handlers.py → core/*           (NO, 仅 astrbot API + stdlib)
```

**规则 1**：`core/handlers.py` 禁止 import 任何 `core/` 内部模块。消息 handlers 只能通过 `self._*` 间接调用来自其他 mixin 的方法。

**规则 2**：`core/helpers.py` 禁止 import `core/handlers.py`。helper 是 handler 的调用目标，不是相反。

---

## 2. core/admin_service.py (1003 → 5 文件)

### 子模块边界

AdminService 虽然是一个类，但其方法按领域自然分组。采用 **mixin 拆分** 保持 `from .core.admin_service import AdminService` 不变：

| 目标文件 | Mixin / 内容 | 关键方法 | 估算行数 |
|----------|-------------|----------|----------|
| `core/admin_service.py` | **Facade**：re-export + `_now`, `_normalize_text`, `_safe_memory_type`, `_clamp01` 纯函数 + AdminService 基类（`__init__`, `_db`, 通用查询） | `get_users`, `get_global_stats`, `count_pending_users`, `get_events`, `get_identities` | ~120 |
| `core/admin_memory_mixin.py` | `AdminMemoryMixin`：记忆 CRUD 用于 WebUI | `get_memories`, `get_mindmap_data`, `add_memory`, `update_memory`, `delete_memory`, `set_pinned` + 私有查询辅助方法 | ~210 |
| `core/admin_profile_mixin.py` | `AdminProfileMixin`：画像管理 | `get_profile_summary`, `get_profile_items`, `get_profile_item_evidence`, `update_profile_item`, `archive_profile_item`, `merge_profile_items` | ~200 |
| `core/admin_identity_mixin.py` | `AdminIdentityMixin`：身份与用户管理 | `merge_users`, `rebind_user`, `export_user`, `purge_user` | ~200 |
| `core/admin_distill_mixin.py` | `AdminDistillMixin`：蒸馏状态 | `get_pending`, `get_distill_history`, `get_distill_budget_info`, `set_distill_pause` | ~50 |

### Base AdminService 签名

```python
class AdminService(AdminMemoryMixin, AdminProfileMixin, AdminIdentityMixin, AdminDistillMixin):
    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._db_mgr = plugin._db_mgr
        self._identity_mgr = plugin._identity_mgr
        self._memory_logger = plugin._memory_logger
        self._cfg = plugin._cfg
```

### 导入方向

```
AdminService mixins → core/db.py        (TYPE_CHECKING only, 保持现状)
AdminService mixins → core/config.py      (TYPE_CHECKING only)
AdminService mixins → core/identity.py    (TYPE_CHECKING only)
AdminService mixins → core/memory_logger.py  (TYPE_CHECKING only)
web_server.py → core/admin_service.py     (OK, 一层向下)
```

**规则 3**：AdminService mixins 禁止 import `web_server.py`、`main.py` 或其他上层模块。

**规则 4**：AdminService 各 domain mixin 之间不相互 import。共享类型通过基类或 `TYPE_CHECKING` 解决。

---

## 3. core/consolidation.py (1049 → 6 文件)

### 子模块边界

| 目标文件 | 类 / 函数 | 职责 | 估算行数 |
|----------|-----------|------|----------|
| `core/episode_manager.py` | `EpisodeManager` | Stage B：会话分组 + 情节摘要 prompt 构建 + JSON 解析 + 抽取式回退 + 内置模块级工具函数 | ~160 |
| `core/semantic_extractor.py` | `SemanticExtractor` | Stage C：语义提取 prompt 构建 + JSON 解析 + DB 私有方法（`_insert_episode` 等归属问题见备注） | ~85 |
| `core/consolidation_runtime.py` | `ConsolidationRuntimeMixin` + 模块级工具函数 (`_strip_think_tags`, `_extract_json_object`, `_clamp`, `_parse_iso_timestamp`, `_derive_session_key`, `_build_transcript`) | 编排 B 阶段和 C 阶段 + LLM 调用 + 回退 + DB 写入 | ~320 |
| `core/profile_extractor.py` | `ProfileExtractor` | 画像提取 prompt + JSON 解析 + 类型验证 | ~105 |
| `core/profile_extraction_runtime.py` | `ProfileExtractionRuntimeMixin` | 编排画像提取循环 + LLM 调用 + `profile_items` 写入 | ~195 |
| `core/consolidation.py` | **Facade**：re-export 所有类 | — | ~20 |

**备注**：`_insert_episode`、`_pending_consolidation_users`、`_fetch_pending_*`、`_mark_episode_*` 这些方法当前是 `ConsolidationRuntimeMixin` 的方法（访问 `self._db()`），搬迁时随 mixin 一起移动，不独立。

### 工具函数归属

`_strip_think_tags`、`_extract_json_object`、`_clamp`、`_parse_iso_timestamp`、`_derive_session_key`、`_build_transcript` 当前是模块级函数。搬迁方案：

- `_build_transcript` 和 `_derive_session_key` → `core/consolidation_runtime.py`（仅在运行时编排中使用）
- `_strip_think_tags`、`_extract_json_object`、`_clamp`、`_parse_iso_timestamp` → 分散至使用方文件（每个引用点不超过 1 处），不做共享

### 导入方向

```
consolidation_runtime.py → core/episode_manager.py      (OK, 构造 + 调用)
consolidation_runtime.py → core/semantic_extractor.py    (OK, 构造 + 调用)
consolidation_runtime.py → core/distill_errors.py        (OK, 保持现状)
consolidation_runtime.py → core/config.py                (OK, 构造时)

profile_extraction_runtime.py → core/profile_extractor.py   (OK)
profile_extraction_runtime.py → core/config.py              (OK)
profile_extraction_runtime.py → core/memory_ops.py          (OK, 在方法内部 late import)

episode_manager.py → core/config.py     (OK)
semantic_extractor.py → core/config.py  (OK)
profile_extractor.py → core/config.py   (OK)

facade consolidation.py → 上述所有      (OK, 仅 re-export)
```

**规则 5**：`core/episode_manager.py`、`core/semantic_extractor.py`、`core/profile_extractor.py` 这几个纯抽取类只 import `core/config.py` 和 `core/distill_errors.py`（必要时），禁止 import 任何运行时 mixin。

**规则 6**：`ConsolidationRuntimeMixin` 和 `ProfileExtractionRuntimeMixin` 之间禁止互相 import。两者都通过 `TMemoryPlugin` 的 `self` 命名空间共享，但文件层面应保持独立。

---

## 4. web_server.py (742 → 4 文件包)

### 子模块边界

当前 `web_server.py` 是单文件，改为 `web_server/` 包：

| 目标文件 | 内容 | 估算行数 |
|----------|------|----------|
| `web_server/__init__.py` | **Facade**：`from .server import TMemoryWebServer` | ~5 |
| `web_server/_jwt.py` | JWT 极简实现：`_b64url_encode`, `_b64url_decode`, `jwt_encode`, `jwt_decode` | ~50 |
| `web_server/_handlers.py` | WebServerHandlersMixin：全部 ~30 个 `_handle_*` 路由处理方法 | ~450 |
| `web_server/server.py` | `TMemoryWebServer` 基类：`__init__`, `start`, `stop`, `_setup_routes`, `_middleware`, `_require_positive_int`, `_require_distinct_positive_ints`, `_validate_config_patch`, `_get_client_ip`, `_require_json_object` | ~250 |

```python
# web_server/server.py
class TMemoryWebServer(WebServerHandlersMixin):
    """tmemory 独立 Web 面板服务器。"""
    # 生命周期 + 路由设置 + 中间件 + 参数校验
```

```python
# web_server/__init__.py
from .server import TMemoryWebServer
__all__ = ["TMemoryWebServer"]
```

新代码中 `import` 路径：
```python
# 方案一（推荐，不改变 import 路径）
from .web_server import TMemoryWebServer        # 当 web_server 目录作为包导入时

# 方案二（兼容旧引用 — 用于 main.py 内部延迟加载）
# 当前 _safe_load_web_server() 已经用了延迟 import，不需改
```

### 导入方向

```
web_server/server.py → aiohttp           (OK, 外部依赖)
web_server/server.py → web_server/_jwt.py    (OK)
web_server/server.py → web_server/_handlers.py  (OK, 继承关系)

web_server/_handlers.py → core/admin_service.py  (OK, 在 _get_admin 内延迟 import)
web_server/_handlers.py → web_server/_jwt.py  (NO, handlers 不需要 JWT)

main.py → web_server/...  (仅通过生命周期 hook 延迟加载)
```

**规则 7**：`web_server/` 包禁止 import `main.py`（TYPE_CHECKING 例外）。`TMemoryWebServer` 通过构造函数接收 `plugin` 实例，不静态依赖 `main.py`。

**规则 8**：`web_server/_handlers.py` 中延迟加载 `AdminService`：仅在 `_get_admin()` 中 import，避免 import 时级联加载。

---

## 循环导入防御规则汇总

| # | 规则 | 违反后果 |
|---|------|----------|
| R1 | `core/handlers.py` 不 import 任何 `core/` 模块 | 冷启动时循环 import |
| R2 | `core/helpers.py` 不 import `core/handlers.py` | 逆依赖循环 |
| R3 | AdminService mixins 不 import `web_server.*`、`main` | 层间循环 |
| R4 | AdminService domain mixin 间不相互 import | 兄弟循环 |
| R5 | 纯抽取类（EpisodeManager/SemanticExtractor/ProfileExtractor）只 import `core/config.py`，不 import 运行时 mixin | 运行时 mixin 反向引用 |
| R6 | `ConsolidationRuntimeMixin` 和 `ProfileExtractionRuntimeMixin` 文件不互相 import | 跨管道循环 |
| R7 | `web_server/` 不静态 import `main.py` | 入口→服务器→入口循环 |
| R8 | `web_server/_handlers.py` 用 late import 加载 `AdminService` | 包初始化时递归 |

### 反循环模式

如果 A 需要 B 的类型信息而 B 需要 A 的，使用 `TYPE_CHECKING`：

```python
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .some_module import SomeClass
```

---

## 实施顺序

### Phase 1 — core/utils.py 拆分（独立，零外部影响）

```
步骤 1.1: 创建 core/memory_logger.py，搬迁 MemoryLogger
  验证: pytest tests/ -k "memory_logger or utils" 通过
步骤 1.2: 创建 core/helpers.py，搬迁 PluginHelpersMixin
  验证: python -c "from core.helpers import PluginHelpersMixin" 成功
步骤 1.3: 创建 core/handlers.py，搬迁 PluginHandlersMixin
  验证: python -c "from core.handlers import PluginHandlersMixin" 成功
步骤 1.4: 重写 core/utils.py 为 facade，删除 _CMD_FIRST_WORDS（已在 main.py 定义）
  验证: from core.utils import MemoryLogger, PluginHelpersMixin, PluginHandlersMixin 均通过
步骤 1.5: 更新 main.py import（从 core/utils → core/helpers + core/handlers）
  验证: python main.py 启动无 import error
```

**行数变化**：1544 → utils.py ~20 + memory_logger.py ~35 + helpers.py ~580 + handlers.py ~520

### Phase 2 — core/admin_service.py 拆分（独立）

```
步骤 2.1: 创建 core/admin_memory_mixin.py
步骤 2.2: 创建 core/admin_profile_mixin.py
步骤 2.3: 创建 core/admin_identity_mixin.py
步骤 2.4: 创建 core/admin_distill_mixin.py
步骤 2.5: 重写 core/admin_service.py 为基类 + re-export facade
  验证: python -c "from core.admin_service import AdminService" 成功
  验证: 所有 tests/ 中引用 AdminService 的导入不中断
```

### Phase 3 — core/consolidation.py 拆分（独立）

```
步骤 3.1: 创建 core/episode_manager.py，搬迁 EpisodeManager + 相关工具函数
步骤 3.2: 创建 core/semantic_extractor.py，搬迁 SemanticExtractor
步骤 3.3: 创建 core/consolidation_runtime.py，搬迁 ConsolidationRuntimeMixin + 工具函数
步骤 3.4: 创建 core/profile_extractor.py，搬迁 ProfileExtractor
步骤 3.5: 创建 core/profile_extraction_runtime.py，搬迁 ProfileExtractionRuntimeMixin
步骤 3.6: 重写 core/consolidation.py 为 facade
  验证: python -c "from core.consolidation import ConsolidationRuntimeMixin, ProfileExtractionRuntimeMixin, EpisodeManager, SemanticExtractor, ProfileExtractor" 成功
```

### Phase 4 — web_server.py 拆分（涉及目录转换）

```
步骤 4.1: 创建 web_server/ 目录 + __init__.py
步骤 4.2: 创建 web_server/_jwt.py，搬迁 JWT 函数
步骤 4.3: 创建 web_server/_handlers.py，创建 WebServerHandlersMixin，搬迁全部 _handle_* 方法
步骤 4.4: 重写 web_server/server.py，TMemoryWebServer 继承 WebServerHandlersMixin
步骤 4.5: 更新 __init__.py re-export
步骤 4.6: 删除 web_server.py 单体文件
  验证: 主要验证方法参见下文
```

### Phase 5 — 集成验证

```
步骤 5.1: 完整测试套件运行（排除已知跳过项）
步骤 5.2: 人工检查 import 是否符合上述规则
步骤 5.3: ruff format + ruff check — 新文件零告警
```

> **注意**：Phase 1-3 可并发执行（涉及不同目标文件，无共享状态）。Phase 4 依赖前面 phase 的 facade 就位但 代码层面不阻塞。

---

## 验证方式

| 检查点 | 命令 | 预期 |
|--------|------|------|
| 无 import error | `python -c "from main import TMemoryPlugin"` | 0 exit code |
| 无循环导入 | `python -v -c "from core.admin_service import AdminService" 2>&1 \| grep -i "circular\|import error"` | 空输出 |
| 类型检查 | `basedpyright .` | 仅已有 warning |
| 测试通过 | `pytest tests/ -x --ignore=tests/test_real_astrbot_integration.py` | 全部 pass |
| facade 兼容 | `from core.utils import *; from core.admin_service import *; from core.consolidation import *` | 无 AttributeError |
| web_server 启动 | `python -c "from web_server import TMemoryWebServer; print('ok')"` | 输出 ok |
| 行数检查 | `wc -l core/utils.py core/admin_service.py core/consolidation.py web_server/server.py` | 各 ≤250 |

---

## 后果

### 正面

- 每个文件职责单一，新开发者可快速定位
- 降低合并冲突概率（四个大文件是 PR 的热点冲突区域）
- 文件尺寸控制在 ~200-600 行，比 1500/1000 更容易评审
- 逆向 facade 模式确保零外部依赖断崖
- import 方向显式文档化，后续重构有据可依

### 负面

- Mixin 模式保留意味着技术债务未消除 —— 但仍停留在 "文件级债务"（易管理），而非 "模块级债务"
- 部分文件间存在隐式 `self._*` 耦合（mixin 的固有代价）
- facade 文件增加了一小层间接引用
- `web_server/` 从单文件改为包，需要确认 `_safe_load_web_server()` 的加载路径兼容

### 不做的选择

- **不转换为 DI 服务类**：改变函数签名涉及 llm_generate、db() 等核心路径，风险过高
- **不统一 text utils**：`_normalize_text`、`_clamp01` 等在多个 mixin 间重复但经过 `self` 访问，去重需要提取到独立工具类 —— 这是下一阶段的重构项
- **不改变测试结构**：测试文件已按功能组织（`test_consolidation.py` 等），不需要因拆分而变动
