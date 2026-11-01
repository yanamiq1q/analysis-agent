# 报告流水线 PR5：环比 / 同比

> 时间：2026-11-02
> 目标：业务报告最常见的"较上月 / 较去年同期"，**由数据侧算，渲染方只展示**。
> 状态：已完成，`326 passed`

---

## 1. 为什么必须由我们算

上一轮真机生成的 PPT 里，宿主明确说了：

> 没有客单价、没有合计、**没有环比**，这些报告包本身不给，我没有补算。

这是对的——但业务报告不能没有环比。问题在于**谁来算**：

| 谁算 | 会发生什么 |
|---|---|
| 渲染方（LLM） | 每份 PPT 可能用不同基数、不同四舍五入；上期缺失时会"估算"或干脆编 |
| 我们（数据侧） | 口径、区间、除零、缺失全部可控；渲染方只读 `pct` |

和"数字不可篡改"是同一条原则：**能算的只有产出方，展示方只排版。**

---

## 2. 模板怎么声明

```yaml
- id: kpi
  compare: [mom]        # 环比；yoy 也支持，可同时写 [mom, yoy]
```

只有声明了的节才会跑对比查询（多一次同样的 SQL，换区间）。
不声明 = 不对比，老模板行为不变。

---

## 3. 对比区间怎么算（这个必须写清楚）

`shift_period(start, end, kind)`：

| 场景 | 规则 | 例子 |
|---|---|---|
| 环比 · 整月 | 前推相同月数 | `2026-10-01 ~ 11-01` → `2026-09-01 ~ 10-01` |
| 环比 · 整季 | 前推 3 个月 | `2026-07-01 ~ 10-01` → `2026-04-01 ~ 07-01` |
| 环比 · 非整月 | **紧邻的等长区间** | `2026-10-01 ~ 10-16`（15 天）→ `09-16 ~ 10-01` |
| 同比 | 整体前推一年 | `2026-10-01 ~ 11-01` → `2025-10-01 ~ 11-01` |
| 闰日 | 2-29 → 2-28 | `2024-02-29` → `2023-02-28` |

**非整月为什么不对齐自然月**：15 天的数字和 30 天的数字比，不叫环比，叫误导。

月末偏移用 `min(day, 当月天数)`：1-31 往前一个月 → 2-28/29。

---

## 4. 三条计算规则

```python
def compare_metric(current, previous):
    cur, prev = _num(current), _num(previous)
    if cur is None or prev is None:
        return {"prev": None, "delta": None, "pct": None, "reason": "上期无数据"}
    delta = _num(cur - prev)
    if prev == 0:
        return {"prev": prev, "delta": delta, "pct": None,
                "reason": "基期为 0，比率不可比"}
    return {"prev": prev, "delta": delta, "pct": _num((cur - prev) / abs(prev))}
```

1. **任一侧缺失 → `pct=null` + 必填 `reason`**：渲染方要能解释"为什么是 —"，
   所以 `validate()` 会检查"没有 pct 却没有 reason"。
2. **基期为 0 → 只给 delta，不给比率**。+500 元是事实，"增长 ∞%" 不是。
3. **分母用 `|prev|`**：上期为负（亏损）时，涨跌方向不会被符号翻转。

---

## 5. 契约新增

```jsonc
"metrics": [{
  "name": "gmv", "value": 1846900,
  "compare": {
    "mom": {
      "prev": 1700000, "delta": 146900, "pct": 0.0864,
      "period": { "start": "2026-09-01", "end": "2026-10-01" }
    }
  }
}],
"meta": {
  "compare": { "periods": { "mom": { "start": "2026-09-01", "end": "2026-10-01" } } },
  "warnings": ["环比不可用：2026-09-01 ~ 2026-10-01 无数据"]
}
```

- `compare` 是**可选字段**，`schema_version` 仍是 `1.0`（向后兼容）；
- `meta.compare.periods` 给附录页标注"对比区间"；
- 上期无数据时写进 `meta.warnings`，和 coverage 的警告共用同一个数组。

---

## 6. 踩坑：聚合查询在空区间返回"一行 NULL"

第一版用 `prev_df.empty` 判断"上期无数据"，结果测试挂了：

```
SUM(gmv) over 空区间 → 返回 1 行，值为 NULL
→ df.empty == False → 判定"有数据" → 不发警告
```

**行数判断在这里是错的，只能看值**。现在改为看 `compare_metric` 的 `reason`：

```python
if entry.get("reason") == "上期无数据":
    missing_kinds.add(kind)
```

这条坑很典型：`COUNT(*)` 空区间返回 0，`SUM/AVG` 返回 NULL——两种聚合的空集语义不同。

---

## 7. 测试（新增 6 条）

| 测试 | 断言 |
|---|---|
| `test_mom_uses_previous_month` | 9 月 30,000 → 10 月 62,000：`delta=32000`、`pct≈106.67%`、`period` 正确、无警告 |
| `test_missing_previous_month_is_explained` | 只有 10 月数据：`pct=null`、`reason=上期无数据`、`meta.warnings` 含"环比不可用" |
| `test_period_shifts` | 整月 / 整季 / 非整月 / 同比 / 闰日五种区间 |
| `test_zero_base_keeps_delta_but_drops_ratio` | 基期 0：`delta=500`、`pct=null`、reason 说明 |
| `test_negative_base_uses_abs` | -100 → -50：`delta=50`、`pct=+50%` |
| `test_compare_without_pct_needs_reason` | 契约校验：没有 pct 却缺 reason 直接报错 |

---

## 8. 真机预期（当前数据只有 2026-10）

跑出来的报告会是这样：

```jsonc
"gmv": { "value": 1846900,
  "compare": { "mom": { "prev": null, "delta": null, "pct": null,
                        "reason": "上期无数据",
                        "period": {"start": "2026-09-01", "end": "2026-10-01"} } } }
```

KPI 卡片会显示 `较上月 —`，页脚写"环比不可用：2026-09-01 ~ 2026-10-01 无数据"。
**这不是 bug**——没有 9 月数据就不该编一个环比。等 9 月数据入库，同一份模板自动就有环比。

---

## 9. 技能同步更新（zip 已重打）

- `SKILL.md` 铁律第 10 条：涨跌只读 `compare`，`pct=null` 显示 `—` 并写 `reason`，
  **不许自己用 `value/prev-1` 重算**；
- `references/report-spec.md` 新增 `compare` 字段表与展示规则；
- `references/slide-template.md` KPI 卡片增加"环比行"（`较上月 +8.6%`）。

---

## 10. 下一步

| 优先级 | 项 | 说明 |
|---|---|---|
| 高 | 模板引用指标注册表（`metric: gmv`） | 复用口径锁表，减少手写 SQL |
| 中 | `report.md` 人读版 | 给飞书文档 / 不擅长读 JSON 的渲染器 |
| 低 | PR6 `approve_report` | 报表模板已预授权，价值在"临时节" |
| 可选 | 覆盖度可视化 | 趋势图把缺失日期画成灰条 |
