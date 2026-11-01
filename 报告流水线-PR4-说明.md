# 报告流水线 PR4：数据覆盖度与数字精度

> 时间：2026-11-02
> 起因：第一次真机跑通后，宿主（WorkBuddy）自己发现了两处数据问题，但**这本来不该由它发现**。
> 状态：已完成，`320 passed`

---

## 1. 真机跑通的结果（先记一笔，面试可用）

工具序列：

```
list_report_templates  → 确认 monthly_ecom 有 4 节
build_report(2026-10, monthly_ecom) → succeeded（一次跑通，未轮询）
get_report_spec(job_id) → 4 节 / 3 指标 / 2 图 / skipped=0
slidep create / upsert-dsl / lint × 7（页 6 底部溢出 23px → 收紧 SQL 块内边距 → 复检通过）
cp → 成品落到报告目录
```

数字纪律（宿主自述）：

> 全部取自 `metrics[].value`，只做展示格式化（千分位、×100 加 %），未做任何二次计算 ——
> 所以 PPT 里没有客单价、没有合计、没有环比，这些报告包本身不给，我没有补算。

**这段是本轮最有价值的产出**：它证明"数字不可篡改"不是纸面规则，而是可被外部 Agent
执行并主动复述的约束。

---

## 2. 它发现的两个问题

### 2.1 标题是整月，数据只有半个月

> 报告包自带的 `gmv_trend.json` 只有 10-01 ~ 10-16 共 16 天，与 KPI 整月累计口径不一致，
> 页面已明确写"不能用来反推全月走势"。

它是对的：`ads_order_daily_report` 只有 16 天数据，而报告 `period` 是整月。
但**这个判断它只能靠比对图表点数得出**——报告包没告诉它。

### 2.2 浮点尾巴跑进了契约

> 平均支付率 71.1%（0.7112499999999999 → 71.1%）

`0.7112499999999999` 是 Python 浮点累加的正常产物，但它不该出现在对外 JSON 里：
渲染方看到就会抄进 PPT，或者自己 round——**精度策略应该由产出方定**。

---

## 3. 修复一：报告自带覆盖度

### 3.1 模板声明"这一节的数据从哪来"

```yaml
- id: kpi
  coverage:
    table: ads_order_daily_report
    date_column: dt
```

只有声明了 `coverage` 的节才做这件事；没有日期列的表（如 `ods_order_info`）不声明。

### 3.2 流水线算覆盖度

`compute_coverage()`（[pipeline.py](data/report/pipeline.py)）跑一条极轻的查询：

```sql
SELECT MIN(dt) AS min_date, MAX(dt) AS max_date, COUNT(DISTINCT dt) AS days
FROM ads_order_daily_report
WHERE dt >= DATE '2026-10-01' AND dt < DATE '2026-11-01'
```

- 表名/列名走 `^[A-Za-z_][A-Za-z0-9_]*$` 白名单校验，**不拼自由文本**；
- 同一张表 + 同一日期列只查一次（`kpi` 和 `gmv_trend` 共用）；
- 查询失败不拖垮报告，只写 `coverage.error`。

### 3.3 契约里多了三处（都是可选字段）

```jsonc
"meta": {
  "coverage": {
    "ads_order_daily_report.dt": {
      "start": "2026-10-01", "end": "2026-11-01",
      "days": 16, "expected_days": 31, "complete": false,
      "min_date": "2026-10-01", "max_date": "2026-10-16"
    }
  },
  "warnings": [
    "ads_order_daily_report.dt 仅覆盖 2026-10-01 ~ 2026-10-16（16/31 天）：相关指标是该区间累计，不能当作整月口径"
  ]
}
```

- `sections[].coverage`：**每一节也带一份**。渲染方是一节一页，缺数据的那一页要就地标注，
  不能指望它去全局 meta 里找。
- `meta.coverage`：按表聚合，供附录页统一展示。
- `meta.warnings`：人话警告，去重（同表同列只出一条）。

### 3.4 为什么不升 schema 版本

新增的都是**可选字段**，老渲染方忽略即可，属于向后兼容。`schema_version` 保持 `1.0`。

（技能侧同步改了判定：**主版本必须是 1**，`1.x` 内的新字段忽略而不是报错——
原来"不等于 1.0 就停"太脆，一次加法就得改技能。）

---

## 4. 修复二：数字精度

`_num()` 末尾加 `round(number, 10)`：

```python
number = round(number, 10)
return int(number) if number.is_integer() else number
```

- 10 位小数足以去掉二进制噪声（0.7112499999999999 → 0.71125）；
- 业务精度远在 1e-10 之上（金额到分），不会丢信息；
- 整数仍然保形（1846900.0 → 1846900）。

**为什么由产出方 round**：渲染方各自 round 会出现"两个页面同一个小数位数不同"；
精度策略必须只有一处。

---

## 5. 测试（新增 4 条）

| 测试 | 断言 |
|---|---|
| `test_partial_month_is_flagged` | 14 天数据 vs 31 天区间 → `days=14`、`complete=false`、警告含 `14/31`，且同表只出一条警告 |
| `test_full_coverage_has_no_warning` | 区间正好 14 天 → `complete=true`、无 ads 相关警告 |
| `test_bad_coverage_config_is_reported_not_crashing` | 表名注入串 → `coverage.error` 存在，报告仍生成 |
| `test_float_noise_is_trimmed` | `0.7112499999999999 → 0.71125`；`1846900.0 → 1846900`；NaN → None |

---

## 6. 对技能的要求（同步更新）

`SKILL.md` 铁律新增第 9 条：

> `meta.warnings` 必须展示（封面或 KPI 页页脚）；`sections[].coverage.complete == false`
> 的页要标注"数据区间 min ~ max（days/expected_days 天）"。

`slide-template.md` 封面行加入 `meta.warnings`，有警告时加一行醒目标注。

这样"数据只有半个月"就变成**报告包自带的结论**，而不是渲染方的推理。

---

## 7. 还没做（按优先级）

| 优先级 | 项 | 说明 |
|---|---|---|
| 高 | **环比 / 同比** | 业务报告最常被问的指标。应由我们算（`compare: prev_period`），渲染方只展示；缺上期数据时给 `null` + 警告，不能编 |
| 中 | 模板引用指标注册表 | 现在模板 SQL 手写；支持 `metric: gmv` 可复用口径锁表 |
| 中 | `report.md` 人读版 | 有些 PPT 技能读 markdown 比读 JSON 稳 |
| 低 | PR5 审批 | `approve_report` + interrupt；报表模板默认预授权，价值主要在"临时节" |
| 低 | 覆盖度可视化 | 在趋势图里给缺失日期画灰条 |
