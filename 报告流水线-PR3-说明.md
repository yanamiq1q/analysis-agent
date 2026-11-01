# 报告流水线 PR3：WorkBuddy 技能（把报告包变成 PPT / 飞书文档）

> 时间：2026-11-02
> 目标：在 WorkBuddy 里说一句话就能出报告，并且**不会乱改数字**。
> 交付：`workbuddy-skill/report-producer/`（可直接导入）+ `report-producer.zip`
> 关联：[PR1](报告流水线-PR1-说明.md)、[PR2](报告流水线-PR2-说明.md)

---

## 1. 为什么有了工具还要技能

MCP 工具给的是**能力**（提交任务、取报告）。但宿主面对 12 个工具时，LLM 会：

- 用 `query_sql` 自己重算一遍数字（和报告对不上）；
- 拿到 `report.json` 只挑一段贴进 PPT；
- 忘了把口径（caveats）放上去；
- 报告还在跑的时候重复调 `build_report`。

**技能解决的是"怎么用"**：固定流程、固定页序、固定护栏。
这和数据-agent 自己的 `skills/metrics` 是同一个思想——**能力归工具，方法归技能**。

---

## 2. 交付物

```
workbuddy-skill/
├── report-producer.zip              ← 直接导入这个（约 5 KB）
└── report-producer/
    ├── SKILL.md                     ← 自检 + 流程 + 八条铁律（约 100 行）
    └── references/
        ├── report-spec.md           ← 字段含义与取用规则
        └── slide-template.md        ← PPT 页序与字段映射
```

> **v1.1 已删除 `examples/report-sample.json`**：那份样例里有真实量级的数字
> （1,846,900 元 / 1,357 单），宿主 agent 明确指出"拿它出稿最省事，但一页都不敢发"。
> 任何"看起来像真数据"的样例都是风险，见第 9 节。

**渐进式披露**：`SKILL.md` 只放"必须先看"的流程；字段细节、排版规则放 `references/`，
宿主需要时才读。这样常驻上下文的只有 80 行，不会一开始就塞满。

---

## 3. 技能里写了什么

### 3.1 流程（五步）

```
确认周期/模板 → build_report → 轮询 get_report → get_report_spec → 渲染/交付
```

### 3.2 六条铁律（针对真实会犯的错）

| 铁律 | 不写会怎样 |
|---|---|
| 数字只引用，不重算 | 宿主把 1,846,900 写成"约 185 万"，和 SQL 对不上 |
| `narrative` 可原样用，但不改数字 | 改一个数字，整份报告的可信度归零 |
| `caveats` 必须出现在页面 | 读者不知道这是"下单金额"还是"支付金额" |
| `skipped` 必须明说缺哪张表 | 用户以为这个月没数据，其实是表没加载 |
| 不去 `query_sql` "核对" | 出现两个数字源，必然打架 |
| `schema_version != 1.0` 就停 | 字段变了还在硬套模板，排版会乱 |

### 3.3 失败处理表

工具不存在 / `failed` / 图表缺失 / 用户要改数字——四种情况都给了明确动作，
避免宿主"自己想办法"（通常等于编数据）。

### 3.4 第 0 步：工具自检（v1.1 新增）

技能开头先检查 4 个工具在不在：

```
list_report_templates / build_report / get_report / get_report_spec
```

缺任何一个就**停下来让用户重启**，并明确列出**四种禁止的替代路径**：

1. 用 `query_sql` 自己算一组数字（会和报告包对不上）；
2. 读旧 `report.json` / 旧产物（**旧数据比没数据更危险**）；
3. 跑本地 CLI（会和服务抢 DuckDB 单进程锁）；
4. 去查进程启动时间、翻代码找"代码缺陷"（浪费时间，重启是唯一解）。

这四条不是凭空写的——它们是宿主在真实现场踩出来的（见第 9 节）。

---

## 4. 配套新增的工具：`get_report_spec`

PR2 只返回了 `spec_path`。这隐含一个假设：**宿主能读本地文件**。
如果它读不了（或路径权限不对），技能就卡在"拿到了路径但看不到内容"。

所以加了第 12 个工具：

```python
get_report_spec(job_id) -> str   # 返回 report.json 原文，超过 25000 字符截断
```

- 优先用它（内容直接进上下文）；
- 被截断或要插图时，再回落到 `spec_path` / `artifacts` 里的文件路径。

这样技能对宿主的文件访问能力**没有硬依赖**，只是"有更好"。

---

## 5. 导入步骤（对应截图里的对话框）

1. WorkBuddy → 技能 → **导入技能**；
2. 把 `workbuddy-skill/report-producer` **文件夹**拖进去，或选 `report-producer.zip`；
3. 弹出"导入技能"时：
   - **非高风险自动安装**：这个技能只调用 data-agent 的只读工具 + 生成文件，
     可以勾；不勾的话每次都问一次，也没问题。
4. 文件要求（截图里写的）：文件夹/zip 里要有 `SKILL.md`，且 md 头部是 YAML
   （`name` + `description`）——我们这两个都满足。

> 如果 WorkBuddy 只认"zip 根目录直接放 SKILL.md"，把 zip 换成把
> `report-producer/` 里的**内容**打包（SKILL.md 在根层）。两种结构我都留了文件夹，
> 现场重打一次 zip 即可。

---

## 6. 验收：一段对话 + 期望的调用序列

在 WorkBuddy 里说：

> 用 monthly_ecom 出 2026 年 10 月的经营报告，做成 PPT

**期望的工具调用顺序**：

```
1. list_report_templates
2. build_report(period="2026-10", template="monthly_ecom")
   → status=succeeded 或 running+job_id
3. （若 running）get_report(job_id) × N
4. get_report_spec(job_id)
5. 读 assets/*.png 插图 → 生成 PPT
```

**检查点**（也就是"这份 PPT 合不合格"）：

- [ ] 封面有标题 + 数据周期（`period.end` 记得减一天）
- [ ] 核心指标页的三个数字，和 `report.json` 的 `metrics[].value` **完全一致**
- [ ] 趋势页用的是 `assets/gmv_trend.png`，不是宿主自己画的
- [ ] 每页页脚有 `caveats` 或 `definition`
- [ ] 附录有 `meta.queries`（至少一条），但不占正文
- [ ] 没出现的节在附录里写明 `skipped` 原因

---

## 7. 踩坑与注意事项

| # | 事项 | 说明 |
|---|---|---|
| 1 | **图片路径是相对路径** | `image_path` 相对 job 目录，实际文件是 `spec_path` 所在目录 + `image_path` |
| 2 | **`period.end` 是开区间** | 写"至今"或"10 月 31 日"时要减一天，否则会多算一天 |
| 3 | **desc 长度** | 技能描述控制在 120 字内（多数宿主的目录行会截断） |
| 4 | **别把指标定义塞进 references** | 我们的 `references/*.md` 会被 data-agent 自己的 skill 解析器当指标解析——但这是 WorkBuddy 的技能目录，不受影响；只是提醒别和 `skills/metrics` 混放 |
| 5 | **zip 层级别搞错** | 拖文件夹最省事；zip 的话注意 SKILL.md 的层级 |
| 6 | **工具清单要重启才更新** | 加了 `get_report_spec` 后，宿主必须重连才能看到第 12 个工具 |

---

## 8. 已知限制

- **没有审批**：报告模板里的 SQL 是预授权的；临时问题仍走聊天 HITL。PR4 再把
  `needs_approval` 接进报告任务。
- **PPT 模板由 WorkBuddy 决定**：我们只给"页序 + 字段映射"，具体版式归它。
  如果它自带模板不接受我们的页序，就退化成"按 section 逐页"。
- **飞书文档没单独验证**：文档型输出和 PPT 用同一份 `report.json`，
  差异只在排版，字段映射是共用的。

---

## 9. v1.1 修正：删掉数据样例（2026-11-02）

### 发生了什么

第一次真机跑，宿主（WorkBuddy）没有出 PPT，而是停下来报告：

> data-agent 的 MCP 工具清单是旧的，只暴露了 8 个基础工具……再往下走只能靠编数据了。
> 另外：技能自带的 `examples/report-sample.json` 里有一份 GMV 1,846,900 元 / 1,357 单的样例。
> 那是格式示例，不是 10 月真实数据——拿它出稿是最省事的做法，但那份 PPT 你一页都不敢发，
> 所以我没有用。

它做对了三件事：**拒绝编数据**、**定位到进程过期**、**指出样例是个陷阱**。

### 三处修正

| 问题 | 修正 |
|---|---|
| 样例里的数字是真实量级，可能被直接拿去出稿 | **删除整个 `examples/`**；技能里明说"刻意不提供数据样例" |
| 宿主为了诊断，翻了进程启动时间、读了代码、跑了本地 CLI | `SKILL.md` 加"第 0 步自检" + 四条禁止的替代路径 |
| "面板里没有新工具"没法快速确认 | 新增 `GET /api/mcp/tools`，浏览器一看就知道 |

### 为什么不是"把样例改成 123"

任何**形状正确**的样例都会被复制粘贴。字段说明已经写在 `references/report-spec.md` 里，
足够宿主理解结构；真实结构用 `get_report_spec` 看本次任务的返回即可。
**样例的价值（省一次理解）远小于它的风险（一份假报告）。**

### 新增的排查入口

```bash
curl http://127.0.0.1:8000/api/mcp/tools
# {"count": 12, "tools": ["ask_data", "build_report", "describe_table", ...]}
```

返回里有 `build_report` = 新代码已生效；只有 8 个 = 进程没重启。
这比让宿主去用 `ps` 查启动时间快得多。

> 副作用：宿主这次的表现，恰好证明了"数字不可篡改 + 没有 report.json 就不出稿"
> 这两条铁律是有效的——它宁愿停下来，也没有拿 1,846,900 去凑一页 PPT。

---

## 10. 下一步

| 项 | 说明 |
|---|---|
| PR4 | `approve_report`：高风险节触发 `needs_approval`，复用 HITL 的 interrupt |
| 可选 | 生成 `report.md`（人读版）——有些 PPT 技能读 markdown 比读 JSON 更稳 |
| 可选 | 报告模板可视化编辑（现在是 YAML 手写） |
