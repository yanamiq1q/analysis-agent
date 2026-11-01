# PPT 页序与字段映射

原则：**一页一个结论**。数字放大，口径放小，来源放页脚。

## 页序

| # | 页面 | 取哪些字段 | 版式建议 |
|---|---|---|---|
| 1 | 封面 | `title`、`period`、`meta.generated_at`、`meta.warnings` | 主标题 + 副标题"数据周期：2026-10-01 ~ 2026-10-31"；有警告时加一行醒目标注 |
| 2 | 核心指标 | `sections[id=kpi].metrics`（最多 3~4 个） | 三张卡片：大数字 + label + 单位 + **环比行** + 口径小字 |
| 3 | 趋势 | `sections[id=gmv_trend].charts[0]` + `narrative` | 上图下结论，图占 2/3 |
| 4 | 结构 | `sections[id=order_status].charts[0]` + `tables[0]` | 左图右表，或上图下表 |
| 5 | 明细 / TopN | `sections[id=top_users].tables[0]` | 表格，最多 10 行；超出说明"仅展示前 N 行" |
| 6 | 口径与来源 | 所有 `caveats` + `meta.tables_used` + `meta.queries` | 小字号；SQL 折行显示，别贴到正文 |
| 7 | （可选）附录 | `metrics[].definition`、`meta.trace_id` | 需要审计时加 |

> 章节顺序以 `sections` 数组为准；上面是按 `monthly_ecom` 模板给的默认排版。
> 模板换了就按 `sections[].heading` 通用排版：有 `charts` 的出图页，有 `tables` 的出表页，只有 `metrics` 的出指标卡。

## 每页的固定元素

- **页脚**：`数据来源：data-agent · 生成时间 {meta.generated_at} · {job_id 前 12 位}`
- **口径**：该节 `caveats` 至少一条放页脚；没有 caveats 就放 `metrics[].definition`
- **环比**：KPI 卡片第二行，`compare.mom.pct` → `较上月 +8.6%`；
  `pct` 为 `null` 时显示 `较上月 —`，并在页脚写 `compare.mom.reason`（如"上期无数据"）
- **单位**：跟着数值走，不要在图例里省略

## 数字格式化

| `format` | 展示 | 例 |
|---|---|---|
| `money` | 千分位 + 单位 | `1,846,900 元` |
| `int` | 千分位 | `1,357 单` |
| `percent` | `value × 100` + `%` | `0.7112 → 71.1%` |
| `auto` | 原样 | `3` |

`value` 是 `null` 时显示 `—`，并在页脚注明"该项本期无数据"，**不要填 0**。

## 排版禁忌

1. 不要把 `meta.queries` 贴到正文——它是溯源材料，放附录。
2. 不要给数字加"约""左右"——`narrative` 里已经有措辞，指标值必须精确。
3. 不要跨页重复同一张图。
4. 不要用 3D 饼图 / 环形图，`charts[].kind` 已经定好了图型。
5. 不要删掉 `caveats` 来"让页面干净"。
