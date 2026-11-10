# 外部宿主交互控制

> 已实现显式关闭和环境规则并通过隔离构建与回归；日常环境的安装和配置仍待授权。
> 原有隐式委派判定见 [Codex 委派运行静默](codex-delegated-run-silence.md)。

## 问题与依据

Lody 创建的独立子会话通过 Codex ACP adapter 的 `threadStart` 启动，Codex 看见的是普通 app-server
顶层线程，而不是它自己 spawn 的原生 subagent。抽查本机两条子会话，rollout 为 `source=vscode`、
`originator=acp-extension-codex`，有持久化 transcript，因此既有 exec、ephemeral、特定 originator 信号不命中。
主会话也使用该 originator，不能整类屏蔽。

本轮调查版本为 [Lody c044f932](https://github.com/LodyAI/Lody/tree/c044f93225348f72f631ea2cdfdbe3170de7347e)
和其 [Codex adapter f2a140b5](https://github.com/LodyAI/acp-extension-codex/tree/f2a140b5a630454f3707c48d93ea162ecbad82e7)。
`apps/cli/src/session/session.ts::buildShellEnv` 设置 `LODY_SESSION_ID` 与 `LODY_OPENED_BY_SESSION_ID`；
前者主子会话都有，后者表示创建者。`LODY_PARENT_SESSION_ID` 表示 Tab 包含关系，不等价于委派创建者。
隔离真实 Codex 0.164.0 的 SessionStart Hook 已确认能继承创建者变量；没有发起计费模型调用。

Codex 会过滤 MCP 子进程环境，所以只在 MCP 里读 Lody 变量不足以覆盖实际场景。Hook 将判断关联到
native session ID，MCP 按每次调用的 session/thread ID 读取同一标记。

## 控制契约

- `ASKHUMAN_INTERACTION=off` 禁用交互。精确匹配小写 `off`；其他值不强制打开。
- `interaction.suppressWhen` 默认空数组；任一条件命中即禁用，多条规则为 OR。
- `{"env":"NAME"}`：变量存在且值非空；不裁剪值中的空格。
- `{"env":"NAME","equals":"value"}`：变量存在且精确相等，区分大小写。`equals:""` 可匹配已设置的空值；
  未设置的变量不匹配。省略或 `null` 的 equals 表示非空条件。
- 变量名必须匹配 `[A-Za-z_][A-Za-z0-9_]*`；配置命令拒绝非法条件与拼错的条件字段。
- 显式关闭和规则优先于“允许被委派的 Codex 运行使用 AskHuman”，没有强制打开来覆盖静默的入口。
- 不审计环境变量值。配置读取失败沿用现有默认回退；显式 `off` 不依赖配置可读性。

配置归属为 AskHuman，通常位于 `~/.askhuman/config.json`，可用 `AskHuman config path` 查询实际路径。
Lody 提供环境变量，AskHuman 读取自己的规则决定是否交互；不需要在 Lody 或 Codex 配置中添加该字段。
手工编辑时将 `interaction` 合并到现有配置并保留其他字段。

配置示例（需要本地修复版程序，当前发行版不支持）：

```sh
AskHuman config get interaction.suppressWhen
AskHuman config set interaction.suppressWhen '[{"env":"LODY_OPENED_BY_SESSION_ID"}]'
AskHuman config unset interaction.suppressWhen
```

`set` 替换整个数组；已有规则时应合并后再写。本轮只有配置文件/config 命令，无 UI 编辑器。
程序不默认启用 Lody 规则，用户日常配置必须单独授权和验证。

## 各入口行为

| 入口 | 命中显式策略后的行为 |
| --- | --- |
| Codex SessionStart | 不注入交互协议；按 native session ID 写静默标记 |
| MCP ask / whats_next / todo_add / todo_update | 参数校验与任何副作用前返回 `isError:true`、清楚说明交互禁用 |
| 直接 CLI 提问 / whats-next | 读取 stdin、解析附件、自动待办出队、启动 daemon 前退出，stderr 说明禁用，退出码 1 |
| CLI todo 写操作 | add/rm/clear/attach/detach 在同步和写入前报禁用 |
| Codex Stop / PermissionRequest | 不给决定，让 Codex 原生流程处理；不伪造批准、回答或结束同意 |
| Codex 失败确认卡 | daemon 读取静默标记，不创建错误确认卡 |
| 会话跟踪、status、show_last、todo_list | 保留；配置管理入口也可用 |

原有系统后台线程与隐式委派策略保持自身边界，包括它们历史上对 MCP show_last 的限制。
显式外部策略自身允许 show_last。显式策略在 MCP 启动环境可见时保留工具目录，按调用拦写，供只读工具继续使用；
原有 exec 空目录策略不变。

Codex 全链路在本轮范围内。其他 Agent 的直接 CLI/MCP 若继承显式变量或规则环境也可被拦；
不承诺它们的协议注入、Hook、环境过滤后的会话绑定已完整适配。

## 标记、恢复与接管

复用 `state/codex-delegated/<session>.json`，新增信号 `external_interaction_policy`，不修改 IPC 或 SQLite 恢复结构。
SessionStart 在 startup/clear/resume/fork/compact 都重新读取当前环境：仍匹配则写外部静默；
离开宿主后不再匹配则清旧外部标记，继续原有隐式判定与协议交付。compact 只保留旧隐式委派信号。
Stop/权限 Hook 若读到当前外部条件，也会补写标记。

MCP 没有宿主环境时读取标记；支持 namespaced turn metadata 中的 session ID，以及 plain `_meta.sessionId/threadId`。
直接 CLI 从可信原生环境或 MCP 每次调用的绑定获得 Codex session ID，仅外部标记拦直接 CLI。
daemon 不用自身进程环境去静默所有会话，只查请求对应标记。

按 Lody 创建者变量配置，含义是“该子会话在 Lody 内持续静默”，即使用户后来手动进入它也不例外。
若宿主要支持原地人工接管，应提供当前运行模式或接管状态作为规则变量。

正在运行的旧 MCP 进程不会自动升级守卫；特别是 todo 写操作仍在旧进程内执行。
安装后须核对 Hook、MCP、CLI 的真实二进制入口，并重建旧 MCP 连接/会话。旧历史可能保留已注入协议，
工具错误会明确要求不要重试或升级，不能把“禁用”理解为人类同意。

## 验证入口

`interaction` 条件测试、`agents::delegated` 分类测试、Codex SessionStart 流程测试和 MCP 守卫测试覆盖决策。
`scripts/verify-interaction-silence.py <修复版二进制绝对路径>` 驱动真实 CLI/MCP/Hook，用私有配置、会话标记和
禁用弹窗的临时实例验证：子会话拦截、只读保留、环境过滤、CLI、自动待办不出队、原生 Hook 无决定、接管清标记、
主会话不受影响、显式 off 和 config 命令匹配语义。旧版会在第一项断言失败且不会产生问题。

本地验证结论与安装状态在 [PROGRESS](../PROGRESS.md) 记录；本轮未替换日常发行版。
可加入 `--codex <Lody自带Codex路径>` 运行真实可信 Hook 链路。该测试只配置关闭的本机推理端口，
不读取真实模型凭据或调用模型服务；macOS 使用缩短且规范化的临时路径，兼容 Hook 信任哈希及 Unix socket 长度限制。
