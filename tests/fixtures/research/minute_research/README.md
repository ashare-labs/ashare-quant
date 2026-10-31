# 分钟研究策略夹具

`shared_cash_t1.py` 与 `visible_momentum.py` 是用于受控分钟研究回归测试的策略夹具，配合 A数达 `export_minute_research` 导出的 `input.json` 使用。

```sh
python -m ashare_quant.backtests.minute --help
```

零成交分钟、手续费、T+1 交割与价格限制包络按输入契约校验，详见 [回测引擎与异步任务](../../../../docs/backtests.md)。
