from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import random


@dataclass(frozen=True)
class Tick:
    sequence: int
    symbol: str
    price: Decimal

    def json(self) -> dict[str, object]:
        value = asdict(self)
        value["price"] = format(self.price, "f")
        return value


class DeterministicTickSource:
    """Seeded, synthetic prices. No clock, network, or exchange input is used."""

    def __init__(self, seed: int = 7, symbol: str = "DEMO", start_price: Decimal = Decimal("100")):
        if start_price <= 0:
            raise ValueError("start_price must be positive")
        self._random = random.Random(seed)
        self.symbol = symbol.upper()
        self.price = start_price
        self.sequence = 0

    def next(self) -> Tick:
        self.sequence += 1
        # Integer basis-point steps avoid platform-dependent float arithmetic.
        step_bps = self._random.choice((-12, -8, -4, 4, 8, 12))
        self.price = (self.price * (Decimal(10000 + step_bps) / Decimal(10000))).quantize(Decimal("0.00000001"))
        return Tick(self.sequence, self.symbol, self.price)
