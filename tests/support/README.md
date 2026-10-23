# 开发验证支撑

此目录仅用于低层引擎、存储和历史格式回归，不安装到 ashare_quant wheel，不由产品导入。app.py 是开发验证用 HTTP fixture。退役的固定窗口、预设算法、合成输入与 runner 放在此处，以便验证旧封存格式和共享引擎行为；不是可部署的兼容服务。

正式产品退役边界由 tests/test_formal_backend.py 直接针对 ashare_quant.api 验证。新 Python 日频/分钟、因子和 MCP 测试始终使用真实产品应用，不能改为这个 fixture 来使测试通过。需要原生引擎的开发 runner 仅在测试的临时目录运行。

退役产品 CLI 的五个专用验收脚本已删除；底层算法/账本的回归保留在测试中。正式入口只通过当前产品 API/CLI 和 tests/test_formal_backend.py 验证。
