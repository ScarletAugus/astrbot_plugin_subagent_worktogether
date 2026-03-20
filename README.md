# SubAgent WorkTogether

更好的（也许）Agent间通信。让 AstrBot 中的任意 Agent 都能将任务委派给其他 Agent，实现多 Agent 协作。

## 功能概述

本插件为 LLM 注册了两个 function-calling 工具和一个聊天指令，使 Agent 在对话过程中可以自主判断并将子任务委派给更合适的 Agent 处理（原本只支持主 Agent 向 SubAgent 派发任务），最终汇总结果返回给用户。

### LLM 工具

| 工具名 | 说明 |
|---|---|
| `delegate_task_to_agent` | 将任务委派给指定 Agent 并获取其回复。目标可以是任意已配置的子代理，也可以使用 `main` 委派给主代理。 |
| `list_available_agents` | 列出所有可委派的 Agent 及其描述、可用工具，以及当前的委派深度和调用计数信息。 |
| `send_delegation_summary` | 将本次委派过程的追踪报告以图片/文本形式发送给用户。报告通过 `event.send()` 直发，**不进入 LLM 对话上下文**。仅当 `auto_send_trace` 启用时对 LLM 可见，且只能由顶层 Agent（depth=0）调用。 |

### 聊天指令

| 指令 | 说明 |
|---|---|
| `/agents` | 列出所有已配置的子代理信息，包括名称、描述、Provider 和可用工具。 |
| `/trace` | 查看当前会话最近一次委派协作的追踪记录（图片优先，t2i 不可用时回退为纯文本）。 |

## 使用场景

- **专业分工**：为不同领域（翻译、代码、搜索等）配置专用子代理，主代理根据用户问题自动选择最合适的子代理处理。
- **链式协作**：Agent A 完成初步分析后，将结果交给 Agent B 做进一步处理。
- **多跳委派**：子代理可以委派给主代理，主代理在深度允许时还可以继续委派给其他子代理（如 老板 → 秘书 → 执行员）。
- **主代理回调**：子代理在执行过程中如遇到超出自身能力范围的问题，可通过 `main` 将任务回传给主代理。

## 配置项

通过 AstrBot WebUI 的插件配置页面修改以下参数（部分配置可能要重启生效）：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_delegation_depth` | int | `3` | 最大委派递归深度。防止 Agent 之间无限互相委派。 |
| `max_calls_per_agent` | int | `3` | 单次事件中同一 Agent 的最大被调用次数（全局，跨所有调用者累计）。 |
| `max_calls_per_pair` | int | `3` | 单个 Agent 在其当前执行上下文中向同一目标 Agent 的最大委派次数。当 Agent 通过委派链重新进入时计数自动刷新。 |
| `max_total_delegations` | int | `10` | 单次事件中所有委派调用的总次数上限（全局熔断器）。 |
| `delegation_timeout` | float | `120.0` | 单次委派的超时时间（秒）。 |
| `max_task_length` | int | `4000` | 委派任务描述的最大字符数。 |
| `auto_send_trace` | bool | `false` | 启用后，LLM 在完成首次委派时会收到提示，指引它调用 `send_delegation_summary` 发送追踪报告。关闭时该工具对 LLM **完全不可见**。 |
| `disable_native_handoffs` | bool | `false` | 启用后，禁用原生 `transfer_to_*` HandoffTools，强制所有委派通过 `delegate_task_to_agent`，确保 trace 完整记录。插件卸载或重载时会自动恢复原生工具。 |

## 安全机制

本插件采用五层防护体系，确保委派链路安全可控：

```
                        ┌─────────────────────────┐
                        │   ① 递归深度保护         │
                        │   max_delegation_depth   │
                        └────────────┬────────────┘
                                     ▼
                        ┌─────────────────────────┐
                        │   ② 全局委派熔断器       │
                        │   max_total_delegations  │
                        └────────────┬────────────┘
                                     ▼
                        ┌─────────────────────────┐
                        │  ③ 全局单代理调用限制     │
                        │   max_calls_per_agent    │
                        └────────────┬────────────┘
                                     ▼
                        ┌─────────────────────────┐
                        │   ④ 自委派阻止           │
                        │   A 不能委派给 A 自己     │
                        └────────────┬────────────┘
                                     ▼
                        ┌─────────────────────────┐
                        │  ⑤ 调用者-目标配对限制    │
                        │   max_calls_per_pair     │
                        └─────────────────────────┘
```

- **① 递归深度保护**：委派链嵌套层数达到 `max_delegation_depth` 时，拒绝继续委派。
- **② 全局委派熔断器**：单次事件中所有委派调用总数达到 `max_total_delegations` 时触发熔断，防止 token 消耗失控。
- **③ 全局单代理调用限制**：同一 Agent 在整个事件中被调用总次数超过 `max_calls_per_agent` 后被拒绝（无论由谁发起调用）。
- **④ 自委派阻止**：Agent 不能将任务委派给自身。
- **⑤ 调用者-目标配对限制**：当前 Agent 在其单次执行中向同一目标委派超过 `max_calls_per_pair` 次后被拒绝。通过委派链重新进入同一 Agent 时，该计数自动刷新。
- **结构化错误标记**：所有委派失败均以 `[DELEGATION_ERROR]` 前缀返回，使 LLM 能明确区分系统错误与正常回复，触发自我修复逻辑。LLM 的 tool docstring 中包含了对该前缀的处理指引。
- **超时控制**：每次委派调用都有超时保护（`delegation_timeout`），超时后自动取消并返回错误信息。
- **任务长度限制**：过长的任务描述（超过 `max_task_length`）会被直接拒绝，防止 token 浪费。
- **深度感知工具裁剪**：当委派深度即将达到上限时，子代理的工具集中不再包含 `delegate_task_to_agent` 和 `list_available_agents`，避免 LLM 做出必然失败的工具调用。

### 配对限制刷新机制

`max_calls_per_pair` 的计数在 Agent 每次重新进入执行时自动重置：

```
A 执行 (pair_counts = {})
├── A → B  (pair_counts = {B:1})     ✓
├── A → B  (pair_counts = {B:2})     ✓  (假设 limit=2)
├── A → B                            ✘  被拒绝，请换个 Agent
│
│   但如果经过链式回路 A→B→C→A：
│
├── A → B
│   └── B → C
│       └── C → A  ← A 重新进入，pair_counts 刷新为 {}
│           ├── A → B  (pair_counts = {B:1})  ✓  重新可用！
│           └── A → B  (pair_counts = {B:2})  ✓
```

## 委派追踪报告

本插件会自动记录每次委派调用的完整链路信息（调用者、目标、任务、响应、深度、时间戳），并提供两种方式查看报告：

### 自动发送（LLM 引导）

将 `auto_send_trace` 设为 `true` 后：

1. `send_delegation_summary` 工具对 LLM **可见**（关闭时该工具被从请求中物理移除，LLM 无法感知）。
2. LLM 在完成首次委派时收到系统提示，指引它在所有委派完成后调用 `send_delegation_summary`。该提示仅在顶层（depth=0）且首次成功委派后发送一次。
3. 报告通过 `event.send()` 直接发送给用户，**不会进入 LLM 对话上下文**，避免上下文膨胀。
4. 每个事件仅允许发送一次报告，重复调用会被拒绝。

### 手动查看

无论 `auto_send_trace` 的值如何，都可以发送 `/trace` 查看当前会话最近一次委派协作的追踪记录。

### 确保完整追踪

默认情况下，AstrBot 同时注册原生 `transfer_to_*` HandoffTools 和本插件的 `delegate_task_to_agent`。LLM 可能选择使用原生工具进行委派，此时委派过程不会被追踪。

将 `disable_native_handoffs` 设为 `true` 可禁用原生 HandoffTools，强制所有委派走 `delegate_task_to_agent` 路径，保证追踪报告的完整性。插件通过两层机制确保禁用生效：

1. **`active` 标记**：插件初始化时将所有 HandoffTool 的 `active` 标记设为 `False`。
2. **请求拦截**：通过 `on_llm_request` 钩子在每次 LLM 请求前物理移除 HandoffTool，因为默认 schema 模式不检查 `active` 标记。

插件卸载或重载时会自动将所有 HandoffTool 恢复为 `active=True`，不影响其他插件或 AstrBot 本身的功能。

### 报告格式

- **优先渲染为图片**：使用 HTML 模板 + t2i 服务，以对话流形式展示每一步的调用者、目标、任务摘要和响应摘要。
- **文本回退**：当 t2i 服务不可用时，自动回退为结构化纯文本消息。

## 工具集管理

插件根据目标 Agent 类型和委派深度，智能构建工具集：

| 目标 Agent | 工具集来源 | 说明 |
|---|---|---|
| 主代理（`main`） | 全局注册的所有工具 | 自动排除 HandoffTool 和 `send_delegation_summary` |
| 子代理（`tools=null`） | 全局注册的所有工具 | 同上，继承所有非 Handoff 工具 |
| 子代理（`tools=[...]`） | 仅指定的工具 | 按子代理配置的工具白名单过滤 |
| 子代理（`tools=[]`） | 无工具 | 纯对话模式 |

当委派深度即将达到 `max_delegation_depth` 时，`delegate_task_to_agent` 和 `list_available_agents` 会从工具集中移除。

## 前置条件

本插件需要配合 AstrBot 的**子代理编排器（SubAgent Orchestrator）** 使用。请确保：

1. 在 AstrBot WebUI 中已配置至少一个子代理（Handoff Agent）。
2. 各子代理已正确设置名称、描述、Provider 和工具权限。

如果未配置任何子代理，仍可使用 `main` 将任务委派给主代理。

## 示例

用户发送消息后，LLM 可能自动做出如下调用：

```text
User: 帮我把这段话翻译成英文，然后用代码实现其中描述的算法。

[LLM 调用 list_available_agents]
-> 返回：main, translator, coder

[LLM 调用 delegate_task_to_agent(agent_name="translator", task="翻译以下内容为英文：...")]
-> 返回翻译结果

[LLM 调用 delegate_task_to_agent(agent_name="coder", task="根据以下描述实现算法：...")]
-> 返回代码实现

[LLM 汇总结果回复用户]
```

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
