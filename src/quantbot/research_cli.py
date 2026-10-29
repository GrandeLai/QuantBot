"""Command-line entrypoint for local StockBot operations."""

import sys

from .cli_commands.context import CLIContext
from .cli_commands.registry import build_parser


def main(
    argv=None,
    stdout=None,
    environ=None,
    import_checker=None,
    runtime_smoke_suite_factory=None,
    oos_price_loader=None,
    daytrade_minute_loader=None,
    daytrade_order_submitter=None,
    paper_account_value_loader=None,
    daytrade_session_loader=None,
    normal_experiment_loader=None,
) -> int:
    """Parse one command and dispatch it through an injected execution context."""

    context = CLIContext(
        stdout=stdout or sys.stdout,
        environ=environ,
        import_checker=import_checker,
        runtime_smoke_suite_factory=runtime_smoke_suite_factory,
        oos_price_loader=oos_price_loader,
        daytrade_minute_loader=daytrade_minute_loader,
        daytrade_order_submitter=daytrade_order_submitter,
        paper_account_value_loader=paper_account_value_loader,
        daytrade_session_loader=daytrade_session_loader,
        normal_experiment_loader=normal_experiment_loader,
    )
    args = build_parser().parse_args(argv)
    return args._handler(args, context)


if __name__ == "__main__":
    raise SystemExit(main())
