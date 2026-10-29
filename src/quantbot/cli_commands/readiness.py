"""Read-only strategy readiness command."""

import json

from ..readiness import evaluate_strategy_readiness
from .context import CLIContext
from .support import parse_date


def handle_strategy_readiness(args, context: CLIContext) -> int:
    report = evaluate_strategy_readiness(
        strategy=args.strategy,
        access_mode=args.access_mode,
        start=parse_date(args.start),
        end=parse_date(args.end),
        warmup_start=parse_date(args.warmup_start) if args.warmup_start else None,
        warmup_end=parse_date(args.warmup_end) if args.warmup_end else None,
        bundle_root=args.bundle_root,
        adapter_spec=args.adapter,
        adapter_loader=context.normal_experiment_loader,
        universe_manifest=args.universe_manifest,
        cache_dir=args.cache_dir,
        exposure_ledger=args.exposure_ledger,
        environ=context.environ,
        import_checker=context.import_checker,
    )
    context.stdout.write(json.dumps(report.as_payload(), sort_keys=True) + "\n")
    return 0 if report.ready_to_claim else 1


__all__ = ("handle_strategy_readiness",)
