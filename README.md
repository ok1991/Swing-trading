# Swing Trading：已验收ETF轮动执行端

本项目只执行 ETF-main 发布的 rotation V2 风险预算目标，不再使用旧的 V4 事件信号开仓。非现金目标必须来自已通过 Walk-Forward 验收的轮动模型；模型资格被撤销时执行受信任的现金目标。

默认目标：

```text
https://etf.imlam.com/etf_rotation_latest.json
```

ETF-main 与 Swing-trading 同级部署时，Swing 默认优先读取兄弟目录 `../ETF-main/public/etf_rotation_latest.json`，合约、时效和策略指纹通过后才刷新本地缓存；这样远程发布端点暂时滞后时不会执行旧模型。独立部署仍使用 `ROTATION_URL`。可用空的 `LOCAL_ROTATION_SOURCE` 禁用本地来源，或设置 `ROTATION_SOURCE_PRIORITY=remote_first` 显式改为远程优先；无论来源顺序如何，所有目标都必须通过相同 rotation V2 合约和时效校验。

执行约束：

- 仅接受 `approved=true` 的轮动模型。
- 严格校验 `target_weights` 之和等于 `max_exposure_ratio`，剩余资金按 `cash_weight` 保留现金。
- `risk_control_only=true` 的现金目标只允许减仓/清仓，用于撤销失效模型的旧风险暴露。
- 计划指纹不再包含周编号；ETF 列表、权重、模型和风险预算均未变化时，新的一周不会触发无效再平衡。
- 验收与实盘佣金必须同时为万1.5、免最低5元，否则合约校验失败并保持原持仓。
- 每个新轮动计划只执行一次，不会因每天价格波动重复再平衡。
- 按目标权重先卖后买，使用100份整手约束。
- 远程目标失败时回退到未过期的有效缓存；无有效目标时不调仓。
- rotation 的 `data_date` 最多允许滞后 2 个交易日，`generated_at` 最多允许滞后 96 小时；缺失、未来时间戳或任一门槛超限都会独立阻断调仓。
- 价差、滑点和成交冲击仍作为真实执行成本单独计入。
- 实时报价使用新浪行情，并记录每个代码的报价日期和时间；明确禁止接入东方财富接口。
- 新浪报价只有在上海系统时钟处于连续交易时段、全部报价不超过120秒、任意标的时间差不超过30秒且不存在超过5秒的未来时间戳时才可执行；午休、收盘后、延迟或异步横截面报价全部 fail-closed，`--run-date` 不能替代真实时钟校验。
- 报价校验失败后价格会从交易和估值链路同时清空：不得更新总资产、峰值、最大回撤或 `last_run`，不得覆盖正式组合状态，HTML也不得把无效价格显示为当前估值。
- `--no-realtime` 是强隔离研究模式：显式搭配 `--publish` 会直接拒绝；即使环境设置了 `AUTO_GIT_PUSH=true` 也只生成本地dry-run产物，不得进入任何正式发布链路。
- ETF-main 收盘后发布的数据包含明确的下一交易日 `execution_date`；实盘模式只允许在该指定日期执行。
- 执行引擎自身会再次校验 rotation V2 完整合约与 `execution_date`，直接调用引擎也不能提前、延后或用不同的佣金、价差、滑点、冲击、参与率和整手口径绕过授权。
- 执行端只接受 `acceptance_policy_version=rolling-excess-stability-v1`；验收政策变化会改变计划指纹并立即废止同周旧状态，即使ETF和权重暂时未变化也不得沿用旧授权。
- 每个新增风险目标必须携带与 `data_date` 一致的20日平均成交额、10% ADV新增风险额度及参与率上限；任一字段缺失或相互不一致即拒绝调仓。执行成本按订单金额/ADV计算参与率与冲击，并在报告中展示 ADV、参与率、冲击 bps、剩余容量和总成本。风险减仓缺少历史 ADV 时仍可按最保守参与率估算，避免阻断退出。
- 新增买量按10% ADV实施单日硬上限并取100份整手；超过容量时只执行可成交部分，留下现金并报告 `LIQUIDITY_CAP_REACHED`。风险减仓不因容量上限被阻断，但超限会在订单报告中标记。
- 每次执行发布 `capacity_summary`，汇总已发布容量、请求金额、实际成交、未成交金额、利用率、剩余额度和截断订单数；只接受 `single-exposure-authority-v4` 执行政策及明确的唯一仓位权威，模型权威变化后不得沿用旧袖套计划。
- 公共目标必须声明与当前生产账户一致的1万元 `capacity_reference_capital`；每个目标发布的新增风险额度必须覆盖该参考资金下的目标权重。Swing 仍按账户实时资产和单日10% ADV硬上限执行，账户资金变化后须先重校准模型。
- 必须取得目标 ETF 与现有持仓的全部实时价格，且报价日期和运行日都等于 `execution_date`，报价时间位于 09:30–11:30 或 13:00–15:00。缺价、错日、盘后旧报价或网络失败会阻断整单，绝不使用候选价格或持仓买入价替代实时价。
- `--no-realtime` 仅用于零交易联调：即使轮动目标已批准且文件内带有候选价格，也会强制阻断调仓，不保存组合状态，也不覆盖正式报告；诊断写入 `runtime/reports/dry_run.html`。持仓缺实时价时不更新总资产、实际权重或回撤，报告明确显示“缺失/不可估值”。

## 安装和配置

```bash
python -m pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，核心配置：

```bash
ROTATION_URL=https://etf.imlam.com/etf_rotation_latest.json
COMMISSION_RATE=0.00015
MINIMUM_COMMISSION=0
MAX_ROTATION_AGE_TRADING_DAYS=2
MAX_ROTATION_GENERATED_AGE_HOURS=96
```

## 运行

```bash
python main.py
```

青龙发布报告：

```bash
python main.py --publish
```

青龙生产任务见 `deploy/qinglong.md`；建议工作日 09:35、13:05 与 14:55 各运行一次。14:55 仅用于近收盘估值和实盘绩效记录，不增加调仓权限；同一 rotation 计划不会重复执行。

本地联调轮动文件：

```bash
python main.py --rotation ../ETF-main/public/etf_rotation_latest.json
```

默认状态和输出：

```text
runtime/state/portfolio_state.json
runtime/cache/etf_rotation_latest.json
runtime/logs/swing_trading.log
public/index.html
```

旧 schema V4 状态会自动迁移；不在新轮动目标中的旧持仓将在首个有效轮动计划中卖出。

## 测试

```bash
python -m unittest discover -s tests -v
```
## 结构化执行反馈

每次正式运行都会生成并发布 `execution_feedback_latest.json`。没有券商成交文件时，反馈只能是 `MODEL_ESTIMATE_ONLY` 或 `NO_ORDERS`，不能用于反向证明回测成本有效。

券商成交文件通过 `python main.py --publish --broker-fills /path/fills.json` 接入。文件必须包含 `schema_version=1`、`broker_confirmed=true`、券商名称、当前 `plan_id`、`execution_date` 和 `fills`；每笔成交必须给出代码、方向、数量、价格、佣金、其他费用和成交日期。代码、方向、总数量、计划或日期不一致时，证据会标记为 `BROKER_EVIDENCE_REJECTED`，不会进入ETF成本审计样本。

正式订单会同时保存为带SHA-256指纹的 `runtime/state/execution_plan_latest.json`，并按计划归档。券商回报晚于订单生成时，使用 `python main.py --feedback-only --broker-fills /path/fills.json --publish`；该模式不会请求行情、读取或改写组合状态，也不会再次生成订单。需要核对较早计划时，可通过 `--execution-plan` 指定归档文件。

虚拟盘可在青龙环境中设置 `VIRTUAL_BROKER_CONFIRM=true`（或本地传入 `--virtual-confirm`）。订单生成后会立即生成 `virtual-paper` 成交证据，并通过原有的执行反馈校验和状态对账升级为 `BROKER_CONFIRMED`；真实券商账户不要开启该开关。

所有反馈事件还会去重写入最多100条的 `execution_feedback_history.json`。ETF端读取该账本并补录所有未见过的有效券商样本，因此后续 `NO_ORDERS`、任务漏跑或最新文件被新计划覆盖，都不会丢失已经确认的真实成本证据。

ETF端会独立登记每份已批准rotation的预期执行场次。Swing即使无需下单，也必须在指定 `execution_date` 使用有效实时行情完成正式运行并发布 `NO_ORDERS`；完全漏跑、错日运行、行情不可交易或状态禁止写入都不能核销场次，执行日结束后将触发ETF端 `EXECUTION_SESSION_MISSED` 现金保护。

含订单但尚未提供券商文件时，`MODEL_ESTIMATE_ONLY` 只代表待确认计划，不能验证回测成本。ETF端会跟踪对应 `plan_id`；超过7个自然日仍没有匹配的完整、部分或未成交确认时，将暂停rotation权限直至有效证据补齐。

如需将组合现金、买入成本和累计执行费用从模型估算纠正为券商实际值，显式增加 `--apply-broker-state`。该操作只接受当前组合的最新计划和已确认的完整成交，并以计划ID与券商文件指纹作为幂等键；重复运行不会重复调整。默认不加此参数时，`feedback-only` 仍保持完全只读。

部分成交时，券商文件增加 `order_outcomes`，逐项声明 `FILLED`、`PARTIALLY_FILLED` 或 `UNFILLED`，并提供 `filled_shares` 与 `unfilled_shares`。系统验证二者之和必须等于计划数量，成本样本只计算实际成交部分；启用状态对账时，则从V2执行前快照重建真实现金和持仓。未提供 `order_outcomes` 的旧格式继续按全部成交处理。

每次有效实时估值还会记录组合与510300的同步净值，发布为 `live_performance_latest.json`。同一交易日重复运行只替换当天记录，不增加伪样本；输出包含策略收益、基准收益、相对净值、策略/基准/相对最大回撤以及20日和60日滚动超额。券商状态对账产生的现金修正也会同步修正当天实盘净值。

有效执行场次必须同时产生当天实盘绩效观察，并归属实际写入组合状态的rotation模型。ETF端在执行日后检查该观察；缺失净值文件或仍归属旧模型都会撤销后续rotation权限，因此不能只发布执行反馈而跳过组合与沪深300的同步估值。
