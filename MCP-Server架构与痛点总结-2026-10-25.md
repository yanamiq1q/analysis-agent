# MCP Server 架构与痛点总结：以 AtlasCloud 为样本

> 创建日期：2026-10-25
> 分析样本：`C:\Users\k\IdeaProjects\Data-Analysis-Agent\MCP\AtlasCloud`
> 配套阅读：`MCP学习指南-2026-10-23.md`（协议与创造流程）、`Agent/mcp_manager.py`（Host 侧怎么接）
> 这份文档回答三件事：**它是什么 → 怎么搭的 → 每个设计是为了堵哪个坑**

---

## 0. 一句话定位

**它是一个 MCP Server，不是 Agent。** 它自己不做决策、不编排步骤、不保存会话——它只把 Atlas Cloud 平台的能力包装成一组"LLM 能看懂、能安全调用"的工具，供宿主里的模型挑选。

| 角色 | 在这套体系里是谁 | 干什么 | 有没有"智能" |
|---|---|---|---|
| Host | Cursor / Claude Desktop | 提供 LLM、管权限、把工具转成 function calling | 有 |
| Client | Host 内部的连接器 | 起子进程、发 JSON-RPC | 无 |
| **Server** | **AtlasCloud 这个项目** | **暴露 9 个工具，调外部 API** | **无** |
| 外部系统 | Atlas Cloud REST API | 真正干活（生成图/视频） | 无 |

关键认知：**Server 越"笨"越好**。所有判断都放在宿主模型的提示词和工具描述里，Server 只负责"说清自己能做什么"和"把结果如实返回"。

---

## 1. 架构：四层 + 两个数据源

```
┌──────────────────────────────────────────────────────────┐
│ 宿主 LLM（Cursor / Claude）                               │
│   决定调用哪个工具、传什么参数，自己完成多步编排            │
└──────────────────────┬───────────────────────────────────┘
                       │ JSON-RPC over stdio（一行一条 JSON）
┌──────────────────────▼───────────────────────────────────┐
│ ① 入口层  src/index.ts（47 行）                            │
│   new McpServer → 注册 7 个模块 → connect(StdioTransport)  │
└──────────────────────┬───────────────────────────────────┘
┌──────────────────────▼───────────────────────────────────┐
│ ② 工具层  src/tools/（7 个模块，9 个工具）                  │
│   docs / models / image / video / llm / quick-generate / upload │
│   每个工具 = 名称 + 描述 + zod schema + annotations + handler │
└──────────────────────┬───────────────────────────────────┘
┌──────────────────────▼───────────────────────────────────┐
│ ③ 服务层  src/services/ + src/utils/                      │
│   api-client（超时/重试/鉴权/proxy/Abort）                 │
│   doc-fetcher（/models + 5min 缓存 + 模糊匹配）            │
│   error-handler（HTTP 码 → 人话）  formatter（截断/排版）   │
└──────────────────────┬───────────────────────────────────┘
┌──────────────────────▼───────────────────────────────────┐
│ ④ 外部  Atlas Cloud REST API + 模型文档                    │
└──────────────────────────────────────────────────────────┘
```

| 层 | 职责 | 代码位置 | 一句话 |
|---|---|---|---|
| ① 入口 | 建 server、装配工具、挂传输 | [index.ts](file:///C:/Users/k/IdeaProjects/Data-Analysis-Agent/MCP/AtlasCloud/src/index.ts) | 只有 47 行，协议细节全部交给 SDK |
| ② 工具 | 定义"LLM 能看到什么" | `src/tools/*.ts` | 这一层决定 LLM 用得对不对 |
| ③ 服务 | 所有跨工具的公共逻辑 | `src/services/`、`src/utils/` | 缓存、重试、错误翻译住在这里 |
| ④ 外部 | 真正的业务能力 | Atlas Cloud API | 换成自家数据源即可复用整套结构 |

这四层是**可迁移的骨架**：换一个业务，只改 ② 和 ④，①③ 基本不动。

---

## 2. 工具层：9 个工具按"认知 / 执行 / 支撑"分三层

| 层 | 工具 | 干什么 | 副作用 | 关键注解 |
|---|---|---|---|---|
| 认知 | `atlas_search_docs` | 关键词搜模型/文档/API 参考 | 只读 | `readOnlyHint` |
| 认知 | `atlas_list_models` | 列模型，按 Text/Image/Video 过滤 | 只读 | `readOnlyHint` |
| 认知 | `atlas_get_model_info` | 某模型的完整文档、schema、价格 | 只读 | `readOnlyHint` |
| 执行 | `atlas_generate_image` | 文生图 / 图生图 | **计费** | `openWorldHint` |
| 执行 | `atlas_generate_video` | 文生视频 | **计费** | `openWorldHint` |
| 执行 | `atlas_quick_generate` | 一步生成：找模型→取 schema→拼参数→提交 | **计费** | `openWorldHint` |
| 执行 | `atlas_chat` | OpenAI 兼容 chat | 计费 | `openWorldHint` |
| 支撑 | `atlas_get_prediction` | 按 id 查生成任务状态/结果 | 只读 | `readOnlyHint` |
| 支撑 | `atlas_upload_media` | 本地文件 → 公网 URL | 写远端 | `openWorldHint` |

**三条设计原则（比工具本身更值得抄）**：

1. **认知层和执行层分家**。查文档、列模型不花钱，可以随便调；生成花钱，必须"先查后做"。LLM 一旦把模型名背错，认知层能立刻纠偏，而不是直接发出一个 404 请求。
2. **提供一步式工具，但保留细粒度工具**。`quick_generate` 把四步合成一步（省 3 次往返、省 token），`generate_image` 保留全参数控制。**简单路径和可控路径并存**，让模型按场景选。
3. **注解要诚实**：`readOnlyHint / destructiveHint / idempotentHint / openWorldHint` 不是装饰，宿主会据此决定要不要弹确认框。把计费工具标成只读，是自找麻烦。

---

## 3. 单个工具的四段式契约

任何工具都逃不出这五块（以 `image.ts` 为模板）：

```ts
server.registerTool(
  "atlas_generate_image",              // ① 名字：动宾，snake_case，全局唯一
  {
    title: "Generate Image",           // ② 人看的短标题
    description: `...`,                // ③ 写给 LLM 的说明书（决定调用准确率）
    inputSchema: { prompt: z.string().describe("...") },  // ④ 参数
    annotations: { readOnlyHint: false, destructiveHint: false,
                   idempotentHint: false, openWorldHint: true },
  },
  async ({ prompt }) => {              // ⑤ handler：返回 content + isError
    try {
      ...
      return { content: [{ type: "text", text }] };
    } catch (error) {
      return { isError: true, content: [{ type: "text", text: handleError(error) }] };
    }
  }
);
```

**写描述的五条硬规则**（这是 MCP 里最容易做差、也最影响效果的地方）：

| 规则 | 反例 | 正例 |
|---|---|---|
| 写触发条件，不只写功能 | "生成图片" | "当用户要求生成/编辑图片时调用；只能用于图像，不要用于视频" |
| 写清楚参数取值范围 | `size: string` | `z.enum([...]).describe("宽×高，必须是 1024*1024 这类格式")` |
| 写返回值形态 | "返回结果" | "成功返回 markdown，含任务 id；失败返回错误原因" |
| 写清禁止事项 | 无 | "不要编造模型名，不确定时先调用 atlas_search_docs" |
| 给示例 | 无 | `"a cat" -> 生成一只猫的图` |

---

## 4. 服务层：四个横切关注点

### 4.1 缓存：把"每次都查"变成"5 分钟查一次"

`getModels()` 打 `/models`，**该接口免鉴权**（`requireAuth: false`），结果在内存缓存 **5 分钟**，之后靠 `normalize()`（去掉 `-_./`、压缩空格）+ 逐词包含做模糊匹配。模型 ID、displayName、归一化形式三种都能命中。

**为什么重要**：LLM 常把 `deepseek-v3` 说成 `deepseek v3` / `DeepSeek-V3`。没有归一化匹配，用户一句自然语言就会变成一堆 404。

### 4.2 超时与重试：知道什么该重试、什么绝对不能

| 配置 | 值 | 说明 |
|---|---|---|
| 请求超时 | 30s | 普通 API |
| 上传超时 | 60s | 大文件单独放宽 |
| 重试次数 | 3 次，1s→2s→4s | 指数退避 |
| 可重试 | 无状态码（网络错）、429、5xx | |
| **不可重试** | **所有 POST** | 会创建计费任务，重试 = 重复扣费 |

```ts
// POST requests should not retry - they may create billable tasks
const effectiveMaxRetries = method === "POST" ? 0 : maxRetries;
```

这是整份代码里最该被记住的一行。**非幂等操作不做自动重试**，这是 MCP Server 和普通 REST 封装最大的区别之一——重试的代价由用户的钱包承担。

### 4.3 错误翻译：把 HTTP 码变成 LLM 能自纠的话

`handleError()` 把机器错误翻成人话，返回的是**给模型看的建议**，不是给运维看的日志：

| 状态码 | 返回给 LLM 的文本 | 模型下一步会做什么 |
|---|---|---|
| 402 / "insufficient" | Insufficient balance. Please top up at `{BILLING_URL}` | 停下来告诉用户充值，而不是反复重试 |
| 401 | Invalid or expired API key. Check `ATLASCLOUD_API_KEY` | 提示用户配置环境变量 |
| 403 | Permission denied | 换方案，不硬刚 |
| 429 | Rate limit exceeded. Please wait | 等待或降低频率 |
| 404 | Resource not found. Check your parameters | **自己改参数重试** |
| AbortError | Request timed out after retries | 提示网络问题 |

**错误不是终点，是下一轮的输入。** 这跟你在 data-agent 里"校验失败把错误文本回喂模型"是同一个机制。

### 4.4 截断：给上下文留余量

`CHARACTER_LIMIT = 25000`，所有大结果经 `truncate()` 处理后才返回。一次灌进 10 万字符的结果，轻则挤掉对话历史，重则让模型"读丢了"关键数字。

---

## 5. 八类痛点 → 解法对照

这是本文的核心：**每个设计都能对应一个具体会翻车的场景**。

| # | 痛点 | 不处理的症状 | 解法 | 落在哪 |
|---|---|---|---|---|
| P1 | LLM 记不住几十个模型的参数 schema | 编造模型名、参数名写错、返回 404/400 | 认知层工具：`search_docs` / `list_models` / `get_model_info`，把知识从"模型记忆"改成"实时查询" | `tools/docs.ts`、`tools/models.ts` |
| P2 | 视频生成要几分钟，工具调用早超时 | 宿主报 timeout，任务其实在跑，用户重复提交 | 两阶段：提交返回 id → `atlas_get_prediction` 轮询（3000ms × 最多 200 次 ≈ 10 分钟） | `tools/llm.ts`、`constants.ts` |
| P3 | 一次生成要连调 4 个工具 | token 翻倍、中间任何一步错了全盘重来 | `atlas_quick_generate` 一步完成"找模型→取 schema→拼参数→提交" | `tools/quick-generate.ts` |
| P4 | 传参类型错 / 必填缺失 | 服务端 500，模型看不懂为什么 | zod 定义 schema + `describe()` 写清约束 + `.min/.max/.enum`，错误码翻译后回喂 | 各工具 `inputSchema` + `error-handler.ts` |
| P5 | 计费接口被自动重试 | 用户被重复扣费 | POST 一律 `maxRetries = 0` | `api-client.ts` |
| P6 | 错误信息是 stack trace | 模型只能瞎猜或放弃 | 按 HTTP 码翻译成"可执行的建议" | `error-handler.ts` |
| P7 | 大结果撑爆上下文 | 历史被挤掉、后续对话失忆 | 所有输出过 `truncate()`，上限 25000 字符 | `utils/formatter.ts` |
| P8 | 每个 AI 应用都要重写一遍对接 | N 个应用 × M 个工具 = N×M 份适配 | 只写这一个 Server，所有 MCP 宿主复用 → N+M | 整个项目 |

再补两个**不在代码里、但会咬人**的坑：

- **stdout 污染**：stdio 传输下 stdout 是协议通道，任何 `console.log` 都会让宿主解析失败。这个项目所有日志都走 `console.error`。
- **工具数量膨胀**：工具清单常驻上下文，9 个是个合理量级。再加 20 个，选择准确率会明显下降——那时候该拆 server，而不是继续加。

---

## 6. 可迁移的 7 个设计模式

| 模式 | 一句话 | 在你自己项目怎么落 |
|---|---|---|
| 认知/执行分层 | 先免费查，再花钱做 | `get_metric_definition`、`list_tables` 与 `query_sql` 分开 |
| 一步式工具 | 高频组合流程封成一个工具 | "查询某指标"封装"匹配口径→生成 SQL→执行" |
| 注解声明副作用 | 只读/破坏性/幂等必须标真 | `query_sql` 标 `readOnlyHint: true` |
| 幂等边界 | 非幂等不重试，报错让人决定 | 任何写库/计费工具 `maxRetries = 0` |
| 结果截断 | 服务端截断 + 分页参数 | 查询结果超过 N 行落盘，只回摘要 + 路径 |
| 错误人话 | 错误文本是给模型看的 | 校验失败返回"哪条规则没过"，不是异常栈 |
| 统一出口 | 所有外部调用过一个 client | 超时、重试、鉴权、日志只写一遍 |

---

## 7. 如果 data-agent 也做一个，工具清单应该长什么样

| 建议工具 | 对应现有能力 | 副作用 | 描述要点 |
|---|---|---|---|
| `list_tables` | 现有 `list_tables` | 只读 | "任何 SQL 问题前先调用" |
| `get_metric_definition` | M8 `match_metrics` + metrics.md | 只读 | 返回责任表 + 口径 SQL，明确"不得换表" |
| `query_sql` | 现有 `query_sql` + `validate_sql` | 只读 | "只允许 SELECT；只允许已加载的表" |
| `plot_chart` | 现有 `plot_chart` | 无 | "只在用户要求可视化时调用" |

四条落地约束：**只读优先**、**复用 `validate_sql` 做 AST 校验**（不要在新 Server 里重写一遍）、**结果截断**、**日志走 stderr**。具体骨架见 `MCP学习指南-2026-10-23.md` 的 8.7 节。

---

## 8. 面试怎么讲（3 分钟版）

> 我读过一个 Atlas Cloud 的 MCP Server，它本身的代码只有几百行，但把"给 LLM 用"这件事拆得很清楚。
>
> **架构是四层**：47 行的入口只负责建 server 和挂 stdio；工具层 9 个工具分认知、执行、支撑三类；服务层统一管缓存、超时重试、错误翻译、结果截断；最外面是它包装的 REST API。换业务只改工具层和外部源，另外两层能直接复用。
>
> **最关键的设计是认知层和执行层分开**。查模型文档不花钱，可以反复调；生成图视频花钱，必须先查后做。这直接治了 LLM 编造模型名和参数的问题——知识不在模型记忆里，在实时查询里。
>
> **第二个是长任务的解法**：提交返回任务 id，再用查询工具轮询，3000 毫秒一次、最多 200 次。因为 MCP 的单次调用等不了几分钟。
>
> **第三个我认为最值得说**：它对 POST 请求完全禁用自动重试，注释写的是"可能创建计费任务"。普通 HTTP 封装会无脑重试三次，但在这里重试的代价是用户被扣三次钱。幂等边界必须由工具作者显式声明。
>
> 错误处理也一样，它把 402/401/429/404 翻译成"去充值""检查 API Key""稍后重试""改参数"，因为错误文本的读者是模型，不是运维。

**可能被追问**：

- *"这不就是给 API 包一层吗？"* → 包一层是工作量，难点在**把 API 改造成适合 LLM 决策的形态**：合并步骤、标注副作用、错误可自纠、结果可控长度。
- *"为什么不用 function calling 直接调？"* → 只有自家一个应用时确实不需要 MCP；MCP 的价值是同一个工具被多个宿主复用。
- *"工具越多越好吗？"* → 不是。工具清单常驻上下文，9 个是合理量级，膨胀就拆 server。

---

## 9. 边界：这个 Server 不做什么

| 不做 | 为什么 | 谁来做 |
|---|---|---|
| 多步编排、任务规划 | 那是 Agent 的职责 | 宿主里的 LLM |
| 保存对话、记忆状态 | Server 应无状态，重启即恢复 | Host |
| 检索增强（RAG） | 协议不管检索策略 | 工具内部自己实现 |
| 鉴权宿主用户 | 协议层没有用户身份概念 | Host / 部署层 |
| 重试计费操作 | 重复扣费风险 | 交给用户或上层流程 |

**记住这条边界，就不会把 Server 写成半个 Agent**——这是这类项目最常见的过度设计。
