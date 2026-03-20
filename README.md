# SubAgent WorkTogether

让 AstrBot 中的任意 Agent 都能将任务委派给其他 Agent，实现多 Agent 协作。

## 功能概述

本插件为 LLM 注册了两个 function-calling 工具和一个聊天指令，使 Agent 在对话过程中可以自主判断并将子任务委派给更合适的 Agent 处理（原本只支持主 Agent 向 SubAgent 派发任务），最终汇总结果返回给用户。

### LLM 工具

| 工具名 | 说明 |
|---|---|
| `delegate_task_to_agent` | 将任务委派给指定 Agent 并获取其回复。目标可以是任意已配置的子代理，也可以使用 `main` 委派给主代理。 |
| `list_available_agents` | 列出所有可委派的 Agent 及其描述、可用工具，以及当前的委派深度和调用计数信息。 |

### 聊天指令

| 指令 | 说明 |
|---|---|
| `/agents` | 列出所有已配置的子代理信息，包括名称、描述、Provider 和可用工具。 |

## 使用场景

- **专业分工**：为不同领域（翻译、代码、搜索等）配置专用子代理，主代理根据用户问题自动选择最合适的子代理处理。
- **链式协作**：Agent A 完成初步分析后，将结果交给 Agent B 做进一步处理。
- **多跳委派**：子代理可以委派给主代理，主代理在深度允许时还可以继续委派给其他子代理（如 老板 → 秘书 → 执行员）。
- **主代理回调**：子代理在执行过程中如遇到超出自身能力范围的问题，可通过 `main` 将任务回传给主代理。

## 配置项

通过 AstrBot WebUI 的插件配置页面修改以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_delegation_depth` | int | `3` | 最大委派递归深度。防止 Agent 之间无限互相委派。 |
| `max_calls_per_agent` | int | `3` | 单次事件中同一 Agent 的最大被调用次数（全局，跨所有调用者累计）。 |
| `max_calls_per_pair` | int | `3` | 单个 Agent 在其当前执行上下文中向同一目标 Agent 的最大委派次数。当 Agent 通过委派链重新进入时计数自动刷新。 |
| `max_total_delegations` | int | `10` | 单次事件中所有委派调用的总次数上限（全局熔断器）。 |
| `delegation_timeout` | float | `120.0` | 单次委派的超时时间（秒）。 |
| `max_task_length` | int | `4000` | 委派任务描述的最大字符数。 |

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
- **结构化错误标记**：所有委派失败均以 `[DELEGATION_ERROR]` 前缀返回，使 LLM 能明确区分系统错误与正常回复，触发自我修复逻辑。
- **超时控制**：每次委派调用都有超时保护，超时后自动返回错误信息。
- **任务长度限制**：过长的任务描述会被拒绝，防止 token 浪费。

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
