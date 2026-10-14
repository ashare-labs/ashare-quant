# 策略编写

当前任务协议为 `m2-jobs.v6`。注册冻结策略版本，任务固定策略、参数、数据与运行时。编辑原文件不会改变已创建任务；修改后需重新注册并创建新任务。

## 可信 Python

一个 UTF-8 `.py` 文件，最大 64 KiB，提供同步函数 `on_decision(context, data, parameters)`。恰好三个参数，无默认参数、装饰器或变长参数；可在同文件定义辅助函数。依赖声明支持 `decimal`、`math` 和 `ashare_quant.strategy_api`，不支持额外包或多文件导入。

```python
from ashare_quant.strategy_api import NoChange, TargetShares


def on_decision(context, data, parameters):
    if data.previous_close > 0:
        return TargetShares(100, "持有100股")
    return NoChange("保持现状")
```

返回 `TargetShares(quantity, reason)`，quantity 为非负整百股且不超过二进制浮点可精确表达的整数范围 或 `NoChange(reason)`；reason 最长 256 字符。目标是信号，订单、成交、费用、T+1 与账户由原生引擎处理。

| 输入 | 内容 |
| --- | --- |
| `context` | 带时区逻辑钟、交易日、前一开市日、从零开始的日索引、证券 |
| `context.prior_account` | 上次提交日终现金与数量、快照时刻与摘要；首日为空仓初始账户 |
| `data` | 授权的前一交易日收盘、价格日期、绑定与读取证据摘要 |
| `parameters` | 最多 32 键、8 KiB；值允许 str/int/bool/null，金额用字符串 |

输入 dataclass 冻结，参数只读。策略不获得数据客户端或任意查询接口。日频范围是固定证券与窗口。每个未提交决策创建新命名空间，没有持久全局变量或可恢复自定义状态；执行时模块顶层语句也会运行。注册只检查语法与声明，不加载文件。仅运行可信自有代码，独立进程不是恶意代码沙箱。

默认模块加载与单次回调各限 5 秒，单 attempt 120 秒，准备和检查各 60 秒；合并 stdout/stderr 限 1 MiB。这些预算可在新库的 `ASHARE_QUANT_JOB_POLICY` 中设置（`module_load_seconds`、`callback_seconds`、`execution_seconds` 等），catalogue 返回实际值；阶段仍受总时长和 CPU 预算约束，没有硬内存沙箱。错误或超时记录为失败。恢复不重跑已提交决策；崩溃前未提交的回调可能再执行，外部副作用不保证恰好一次。

```sh
.venv/bin/ashare-quant --db .state/research.sqlite3 job-strategy-register-python --file examples/python/hold.py --descriptor examples/python/descriptor.json
```

Python 注册仅限 CLI；HTTP `/api/v1/m2/python-strategies` 及 `/{version_id}` 只读元数据。示例：[hold.py](../examples/python/hold.py)、[cash.py](../examples/python/cash.py)、[staged.py](../examples/python/staged.py)、[descriptor](../examples/python/descriptor.json)。

## JSON 规则

```sh
.venv/bin/ashare-quant --db .state/research.sqlite3 job-strategy-register --request examples/rules/hold.json
```

格式为 `decision_rules.v1`，每条含唯一 `rule_id`、`when_all`、`target_shares`。最多 16 条，每条最多 8 条件；顺序选择首个全部条件成立的规则，无匹配保持不变，空条件只允许最后一条。

输入为 `previous_close`、`prior_cash`、`prior_quantity`、`day_index`，操作为 lt/le/eq/ne/ge/gt。目标为非负整百股。常量为非负十进制字符串，最多 8 位小数、24 字符；不接受 float、科学计数、NaN 或 Python 表达式。

规则没有外部 IO、eval、递归、任意行情查询或可变状态。HTTP `/api/v1/m2/strategies` 可注册与查询。任务选择 `registered_rules.v1` 和注册返回的版本摘要；同幂等键不能替换版本。

初始资金不再限于演示的 1000～10000 元；任务参数接受正数字符串，最多两位小数及 12 字符。股数、现金和比较阈值是用户模型参数；股票、日期、数据来源、T+1、资金冻结、成交检查和幂等约束仍由各自契约校验。目录中的 `target_shares` 保留常用例子，并用 `target_shares_are_examples` 及最小值、步长、数值表示上限说明实际范围。
