import argparse
import json
from decimal import Decimal
from pathlib import Path

from .engine import SimulationEngine
from .io import load_prices, load_signals
from .models import RiskConfig


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deterministic API-free demo trading simulation")
    p.add_argument("--signals", type=Path, required=True)
    p.add_argument("--prices", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("output"))
    p.add_argument("--initial-cash", type=Decimal, default=Decimal("100000"))
    p.add_argument("--max-order-notional", type=Decimal, default=Decimal("10000"))
    p.add_argument("--max-position-quantity", type=Decimal, default=Decimal("100"))
    p.add_argument("--max-total-loss", type=Decimal, default=Decimal("1000"))
    p.add_argument("--commission-rate", type=Decimal, default=Decimal("0.001"))
    p.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"))
    return p


def main() -> int:
    args = parser().parse_args()
    config = RiskConfig(args.initial_cash, args.max_order_notional, args.max_position_quantity,
                        args.max_total_loss, args.commission_rate, args.slippage_bps)
    engine = SimulationEngine(load_prices(args.prices), config, args.output)
    result = engine.run(load_signals(args.signals))
    engine.write_outputs(result, args.output)
    print(json.dumps({"mode": result["mode"], "run_id": result["run_id"], "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
