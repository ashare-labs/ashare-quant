# 开发验证支撑

此目录仅包含当前正式产品测试所需的测试辅助工具与回归夹具（如真实发布模拟、MCP 传输、在线模拟辅助等），不安装到 `ashare_quant` wheel，也不包含任何已退役 API 或旧执行引擎的平行实现。

正式产品与退役边界由测试套件直接针对 `ashare_quant.api` 与 `ashare_quant.paper.api` 验证。
