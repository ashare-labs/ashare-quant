# 只读数据诊断

协议 `source-diagnostics.v1` 区分研究值可读、初始化合同和交易事实，不创建任务、订单或账户。HTTP 200 / CLI 0 表示报告生成，报告仍可能含 BLOCKED 或缺项。

## 配置独立 reader

执行环境保持 A数达 0.8.0.dev2。诊断另用 Python 3.12.13 和 A数达 0.8.7.dev2：commit `2b4546712acdbde73d2c1f116b8491e0296b5ad9`，wheel SHA256 `0a0fdac12becf79e86123bd858f32972c15cde069133ecf09ebec562c31e8407`。需要有权使用的 wheel 和包含目标 capture 的已有 Store。

```sh
uv venv --python 3.12.13 .state/research-runtime
uv pip install --python .state/research-runtime/bin/python --no-deps /path/to/ashare_data-0.8.7.dev2-py3-none-any.whl duckdb==1.4.3 pandas==2.3.3 numpy==2.5.3 python-dateutil==2.9.0.post0 pytz==2026.5 tzdata==2026.5 six==1.17.0
```

环境变量 `ASHARE_QUANT_DIAGNOSTICS` 的值为 JSON 字符串，不是文件路径。填写真实路径和自己的 64 位 capture ID：

```sh
export ASHARE_QUANT_DIAGNOSTICS='{"calendar-r1":{"python":"/absolute/path/research-runtime/bin/python","store":"/absolute/path/existing-owner-store","calendar_capture_id":"替换为已有capture的64位ID"}}'
.venv/bin/ashare-quant source-diagnostics-capabilities
.venv/bin/ashare-quant source-diagnostics --request examples/diagnostics/calendar.json
```

示例日期属于固定研究窗口，应按已有 capture 的覆盖编辑请求。接口不自动采集、选源或补日期，公开包不附 capture。

## 请求与解释

CLI 和 `POST /api/v1/source-diagnostics` 请求相同，含 source_alias、证券、1–31 个唯一日期、可选 price/status/preclose 引用和严格布尔 require_complete。引用只含 kind 与 version_id；HTTP 不接受任意 Python/Store 路径。见[个股示例](../examples/diagnostics/preflight-600000.json)。

未绑定来源如实返回缺项；价格、量额和前收保持原值。未配置或请求无效为 HTTP 422 / CLI 2，独立环境缺失或 pin 不符为 HTTP 503 / CLI 2。reader 禁止意外网络和写入；这是可信 reader 的操作限制，不是恶意代码沙箱。

INITIALIZE 是 owner 对阶段、日期、角色和操作的读取合同。研究值可读、日历开市或 DECIDE 权限不替代 INITIALIZE；后端自动核对，不是要求用户逐日审批。
