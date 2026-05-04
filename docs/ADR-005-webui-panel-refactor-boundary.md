# ADR-005 WebUI 面板重构边界与演进策略

- 状态：Accepted
- 日期：2026-04-24
- 关联：`web_server.py`、`templates/dashboard.html`、`main.py`、`core/db.py`、`docs/ADR-002-main-boundary-tightening.md`、`docs/ADR-004-config-compatibility-boundary.md`

## 背景

当前 WebUI 是一个可选启用的独立管理面板：`main.py` 在插件启动时安全加载 `web_server.py`，`web_server.py` 使用 `aiohttp` 暴露独立端口，`templates/dashboard.html` 内联 HTML/CSS/JavaScript 并调用 `/api/*` 接口。

这个设计的优点是部署成本低、失败隔离明确、对 AstrBot 主流程影响小。但随着面板功能增加，当前实现已经暴露出 4 个架构风险：

1. `web_server.py` 同时承担认证、HTTP 路由、请求校验、SQL 查询、领域操作编排和 JSON DTO 转换。
2. WebUI 直接调用 `TMemoryPlugin` 的私有方法，等价于把插件内部方法当作长期 API 契约。
3. `dashboard.html` 内联了全部 UI 状态、请求封装、渲染逻辑和 D3 视图逻辑，后续任何 UI 改动都难以局部测试。
4. 面板已经包含高风险管理操作，如合并用户、改绑身份、清除用户数据、手动精馏；如果继续按“页面按钮直接映射私有方法”的方式增长，会使权限、审计、幂等性和回滚边界越来越模糊。

本 ADR 的目标不是立即引入前端框架或微服务，也不是一次性重写面板，而是定义 WebUI 重构的技术边界，让后续工程师可以按小步迁移，同时保持插件核心记忆能力稳定。

## 决策

采用“WebUI 作为管理适配层，核心记忆域通过应用服务暴露”的重构方向。

短期继续保留：

- 独立 `aiohttp` WebUI 服务器。
- 单进程内访问插件核心能力。
- SQLite 作为默认本地数据源。
- `templates/dashboard.html` 作为第一阶段页面入口。

短期必须收紧：

- WebUI 不再新增直接 SQL 查询。
- WebUI 不再新增对 `TMemoryPlugin` 私有方法的直接调用。
- 新增 WebUI 能力必须先定义应用层方法和 JSON 契约，再接入页面。
- 高风险写操作必须经过统一的管理用例边界，并记录审计事件。

## 领域边界

### 核心记忆域

核心记忆域负责长期稳定的业务规则：

- `Identity`：`adapter + adapter_user_id -> canonical_user_id` 的身份映射与合并。
- `Memory`：长期记忆条目、类型、分数、置信度、常驻状态和失活状态。
- `ConversationCache`：待蒸馏短期消息队列。
- `MemoryEvent`：合并、删除、改绑、清除、WebUI 编辑等审计事件。
- `DistillRun`：蒸馏历史与处理结果。

这些概念不属于 WebUI。WebUI 只表达“管理员想执行什么管理动作”，不能拥有这些业务规则。

### WebUI 管理上下文

WebUI 管理上下文负责管理员体验和 HTTP 适配：

- 登录、token 校验、IP 白名单、反向代理 IP 解析。
- 把 HTTP 请求转换成应用层命令。
- 把应用层查询结果转换成稳定 JSON DTO。
- 渲染管理面板，并处理浏览器端交互状态。

WebUI 管理上下文可以知道“用户列表、记忆列表、待蒸馏队列、审计日志、系统健康”等视图模型，但不能直接决定“合并身份如何迁移数据”“删除记忆是否同步向量索引”“精馏如何调用 LLM”。

## 后端技术边界

### 保留在 `web_server.py` 的职责

第一阶段继续让 `web_server.py` 保留以下职责：

- WebUI 生命周期：`start()`、`stop()`。
- HTTP 服务器装配：`web.Application`、routes、middleware。
- 认证与访问控制：管理员登录、JWT 校验、IP 白名单。
- HTTP 层错误包装：把异常转成 JSON 响应并记录日志。

这些职责与 `aiohttp` 强绑定，抽离成通用层收益有限。

### 移出 `web_server.py` 的职责

后续重构应把以下职责迁出 `web_server.py`：

- SQL 查询和表结构知识。
- 记忆增删改、身份合并、用户清除、手动精馏等业务规则。
- WebUI 专用统计聚合。
- JSON DTO 组装中与领域规则相关的字段计算。

推荐新增一个保守的应用服务边界：`core/admin_service.py`。

建议第一阶段接口不要拆成多个 service，以免过早抽象。一个 `AdminService` 足够承载 WebUI 管理用例，并可复用已存在的 repository / distill / retrieval 收口成果。

```text
TMemoryWebServer
  -> AdminService
      -> TMemoryRepository / DatabaseManager
      -> DistillService 或 plugin 现有薄代理
      -> VectorManager soft dependency
      -> MemoryLogger
```

`AdminService` 的输入输出应使用普通 `dict` / `list` 或小型 dataclass，不依赖 `aiohttp.web.Request`，不依赖浏览器字段命名。

### API 契约边界

WebUI API 分为查询和命令两类。

查询类接口应只读、可重复调用：

- `GET /api/users`
- `GET /api/stats`
- `GET /api/memories?user=...`
- `GET /api/events?user=...`
- `GET /api/pending`
- `GET /api/identities`
- `GET /api/distill/history`

命令类接口会改变系统状态，必须走统一应用用例并记录必要审计：

- `POST /api/memory/add`
- `POST /api/memory/update`
- `POST /api/memory/delete`
- `POST /api/identity/merge`
- `POST /api/identity/rebind`
- `POST /api/user/purge`
- `POST /api/distill`
- `POST /api/distill/pause`
- `POST /api/memory/refine`
- `POST /api/memory/merge`
- `POST /api/memory/split`
- `POST /api/memory/pin`

新增 API 时必须先回答 3 个问题：

1. 它是查询还是命令？
2. 它的领域用例名称是什么？
3. 它是否需要审计事件？

如果回答不了，就不应该先加路由。

## 前端技术边界

短期不引入 React/Vue/Svelte 等前端框架。

理由：

- 当前页面是单面板、低频管理员工具，构建链会增加安装、发布和 AstrBot 插件打包复杂度。
- 当前最大风险在后端边界和用例契约，而不是组件复用能力不足。
- 引入框架前应先稳定 API DTO，否则只会把耦合从内联 JS 迁移到组件层。

第一阶段前端改造目标是“拆出清晰模块边界”，而不是“换技术栈”：

```text
templates/dashboard.html
  -> 保留静态页面骨架和样式入口

templates/webui/app.js
  -> 页面启动、全局状态、tab 切换

templates/webui/api.js
  -> token、fetch 封装、错误处理

templates/webui/memories.js
  -> 记忆表格、编辑、pin/delete/add

templates/webui/identity.js
  -> 身份绑定、合并、改绑

templates/webui/distill.js
  -> 待蒸馏队列、手动蒸馏、精馏

templates/webui/mindmap.js
  -> D3 导图渲染和缩放
```

是否引入前端框架应推迟到满足任一条件后再决策：

- 页面数量超过 3 个独立路由。
- 需要复杂表单状态、批量操作、撤销/重做或离线草稿。
- 需要与 AstrBot 或其他插件共享 UI 组件。

## 数据库与外部服务边界

默认数据库仍为本地 SQLite。

当前 WebUI 重构不引入 PostgreSQL / Supabase 作为必需依赖。

原因：

- 插件的主部署形态是本地 AstrBot 插件，SQLite 单文件符合低运维目标。
- WebUI 面板是管理入口，不应通过重构面板来强行改变核心存储架构。
- 当前 `core/db.py` 已经围绕 SQLite、FTS5、WAL 和可选 `sqlite-vec` 建立事实契约，贸然切换会扩大风险面。

但是后续必须为外部数据库保留清晰扩展点：

- WebUI 不直接依赖 SQLite SQL。
- Repository / AdminService 是未来切换 PostgreSQL、Supabase 或远程控制面的唯一候选边界。
- 外部数据库配置必须遵守 `docs/ADR-004-config-compatibility-boundary.md`：进入嵌套配置对象，兼容层集中在边界入口。

如果未来接入 PostgreSQL / Supabase，推荐先作为“可选同步/只读分析后端”，不要直接替换本地 SQLite 主写路径。这样可以避免离线能力、插件启动、数据迁移和用户隐私策略同时变化。

## 安全与审计边界

WebUI 是管理员能力，不是普通用户能力。后续实现必须保持以下约束：

- `webui_password` 为空时不启动 WebUI。
- 继续支持 IP 白名单和 `webui_trust_proxy`。
- 高风险操作必须记录 `memory_events`，至少包括 actor、目标用户、目标 memory id 或 binding id、操作类型和关键参数摘要。
- `user purge`、`identity merge`、`identity rebind`、`memory split/merge/refine` 必须保持服务端校验，不能只依赖前端确认弹窗。
- JWT secret 继续保持进程级随机生成，重启后 token 失效；除非另开安全 issue，不引入持久会话。

当前认证模型的明确取舍：

- 得到：实现简单、无额外依赖、重启自动失效、适合低频本地管理。
- 放弃：没有多管理员账号、没有细粒度权限、没有跨重启会话保持。

这些放弃项在当前插件场景可接受，不应在本次 WebUI 重构中扩展成完整 RBAC。

## 分阶段演进

### 阶段 1：冻结契约并建立 AdminService

目标：不改变页面功能和 URL，先把后端用例边界建立起来。

范围：

- 新增 `core/admin_service.py`。
- 把 `web_server.py` 中的只读查询和写操作逐步迁移到 `AdminService`。
- `web_server.py` 保留原路由，只做 request parsing 和 response wrapping。
- 为 `AdminService` 补充纯 Python 单元测试。

验收标准：

- 现有 `tests/test_web_server.py` 继续通过。
- 新增测试覆盖用户列表、记忆查询、记忆更新、身份合并输入校验。
- `web_server.py` 不再新增 SQL。

### 阶段 2：拆分前端静态资源

目标：降低 `dashboard.html` 单文件维护成本，但不引入构建链。

范围：

- 新增静态资源路由，例如 `/static/webui/app.js`。
- 将 API 封装、状态管理、表格、身份、蒸馏、导图逻辑从 HTML 中拆出。
- 保持无构建链，浏览器直接加载普通 JS 文件。

验收标准：

- 页面在桌面和移动端仍能加载。
- 登录、用户列表、记忆编辑、导图、待蒸馏队列、身份管理、系统健康功能行为不变。
- 浏览器控制台无加载错误。

### 阶段 3：稳定 API DTO 与文档

目标：让 WebUI API 成为可测试契约，而不是页面私有细节。

范围：

- 在 `docs/` 中补充 WebUI API 契约文档。
- 为命令类接口定义最小错误响应格式。
- 对高风险命令增加审计字段约定。

验收标准：

- 每个 `/api/*` 路由在文档中有请求、响应、错误条件。
- 测试覆盖至少一个查询接口和一个命令接口的 DTO 形状。

### 阶段 4：再评估前端框架或外部数据库

只有当前三阶段完成后，才重新评估是否需要：

- 前端框架。
- OpenAPI 生成。
- PostgreSQL / Supabase。
- 独立管理服务进程。

这些选择都需要新的 ADR，不能作为 WebUI 面板重构的隐含默认项。

## Trade-off

### 选择该方案得到什么

- 保持当前部署简单性，符合 AstrBot 插件低运维场景。
- 将最大风险收敛到应用服务边界，避免 UI 继续扩大核心领域耦合。
- 为未来外部数据库和更复杂前端保留演进路径。
- 后续实现可以按小 PR 推进，每一步都有回归测试锚点。

### 明确放弃什么

- 不立即获得组件化前端工程体验。
- 不立即获得 OpenAPI / 类型生成 / 自动客户端。
- 不立即解决多管理员、细粒度权限和长期会话问题。
- 不把 PostgreSQL / Supabase 作为本次重构的目标交付物。

这些放弃是有意为之：当前系统最需要的是先停止边界扩散，而不是引入更多平台能力。

## 后续约束

后续与 WebUI 相关的实现 issue 必须遵守：

- 新增管理操作先加 `AdminService` 用例，再加 HTTP 路由，再接 UI。
- 新增查询不能在 `web_server.py` 中直接写 SQL。
- 新增配置进入 `webui_settings` 嵌套对象，不新增根级同义字段。
- 涉及存储后端变化必须新开 ADR，不能混入 UI 重构。
- 涉及权限模型变化必须新开安全设计 issue，不能只靠前端隐藏按钮。
