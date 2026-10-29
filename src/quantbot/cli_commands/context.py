"""Injected dependencies shared by CLI command handlers."""

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, TextIO


@dataclass(frozen=True)
class CLIContext:
    """Immutable command execution context.

    Keeping process I/O and integration factories here makes every handler
    independently testable without importing the CLI composition root.
    """

    stdout: TextIO
    environ: Optional[Mapping[str, str]] = None
    import_checker: Optional[Callable[..., Any]] = None
    runtime_smoke_suite_factory: Optional[Callable[..., Any]] = None
    oos_price_loader: Optional[Callable[..., Any]] = None
    daytrade_minute_loader: Optional[Callable[..., Any]] = None
    daytrade_order_submitter: Optional[Callable[..., Any]] = None
    paper_account_value_loader: Optional[Callable[..., Any]] = None
    daytrade_session_loader: Optional[Callable[..., Any]] = None
    normal_experiment_loader: Optional[Callable[..., Any]] = None
