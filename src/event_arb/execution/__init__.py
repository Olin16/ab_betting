"""Execution interfaces. **Live trading is not implemented in this build.**

The only concrete adapter here is :class:`~event_arb.execution.disabled.DisabledExecutionAdapter`,
which refuses every submission unconditionally.
"""
