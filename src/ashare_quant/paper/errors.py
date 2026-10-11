"""Stable machine codes with readable recovery boundaries for the local UI."""

MESSAGES = {
    "PAPER_DATA_STALE_OR_MISSING": "前收数据缺失或水位过期，日意图未进入执行。",
    "PAPER_REPLAY_DATA_MISSING": "研究包存在字段缺口，不能填零或跳过证券。",
    "PAPER_CLOSE_PENDING": "当日执行数据尚未封存，账户未推进。",
    "PAPER_CLOSE_DATA_INCOMPLETE": "当日行情缺失、过期、冲突或成交量为零。",
    "PAPER_MISSED_DECISION": "收到意图已超过模拟决策截止时刻。",
    "PAPER_CLOSE_DEADLINE": "收盘输入超过模拟截止时刻。",
    "PAPER_DATA_NOT_VISIBLE": "数据在决策时刻尚不可见。",
    "PAPER_TRADING_STATE_BLOCKED": "停牌、风险警示或交易状态未知，当前日停止。",
    "PAPER_ACTION_COVERAGE_UNSUPPORTED": "公司行动覆盖未知或存在尚未支持的行动。",
    "PAPER_ACTION_TIMING_UNSUPPORTED": "公司行动登记、除权或到账阶段未通过连续恢复验证。",
    "PAPER_IDEMPOTENCY_CONFLICT": "同一天已有不同冻结请求，拒绝重复执行。",
    "PAPER_PARENT_CONFLICT": "账户已推进或父检查点不匹配，请重新读取。",
    "PAPER_NATIVE_INTERRUPTED": "原生进程中断；确认后恢复同一冻结日。",
    "PAPER_RESUME_REQUIRED": "请先给出原因并恢复，再重试相同冻结日。",
    "PAPER_REPLAY_INPUT_CHANGED": "冻结研究包缺失或已变化，拒绝继续。",
    "PAPER_STRATEGY_RECEIPT_CHANGED": "策略收据、历史输入或目标与固定代码计算不符。",
    "PAPER_REPLAY_CLOSE_CHANGED": "当日行情与冻结来源不符。",
    "PAPER_EVIDENCE_MODE_MISMATCH": "合成时钟和历史研究时钟不能混用。",
}


def error_value(exc):
    code = getattr(exc, "code", "PAPER_FAILURE")
    risk = code.startswith("PAPER_") and (code.endswith("_LIMIT") or code == "PAPER_CASH_BUFFER")
    message = MESSAGES.get(code, "独立风控拒绝当前目标；账户未提交。" if risk else str(exc))
    return {
        "code": code,
        "message": message,
        "detail": str(exc),
        "retryable": False,
        "next_action": (
            "核对冻结风控与策略参数；不能在原会话中放宽风控后重试。"
            if risk
            else "检查数据/版本与已提交账户。中断可先恢复同一冻结日；修改输入或策略须新建会话。"
        ),
    }
