# 分钟研究示例

shared_cash_t1.py 与 visible_momentum.py 是受限分钟研究策略。输入必须是 A数达公开 API 生成并保留来源和研究假设的 input.json；不随包提供真实行情或历史回执。

```sh
python examples/minute_research/run_demo.py --input /path/to/input.json --output /path/to/new-output --accept-research
```

输出目录须为新目录。该模型不证明完整交易时钟或全平台等价；零量记录、费用、T+1 和执行域按输入契约验证。参考 [回测指南](../../../../docs/backtests.md)。
