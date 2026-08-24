from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class MarketDataProvider(Protocol):
    name: str
    def symbols(self, index: str) -> list[str]: ...
    def snapshot(self, symbols: list[str]) -> dict[str, dict]: ...


class ReportProvider(Protocol):
    name: str
    def generate(self, market: str, snapshots: dict[str, dict]) -> str: ...


@dataclass(frozen=True)
class UnconfiguredProvider:
    """Fail-closed adapter used until an explicitly supplied provider is configured."""
    kind: str
    name: str = "unconfigured"

    @property
    def status(self) -> dict[str, str | bool]:
        return {"configured": False, "name": self.name,
                "state": "not_configured", "kind": self.kind}

    def symbols(self, index: str) -> list[str]:
        raise ProviderUnavailable(f"{self.kind} provider is not configured")

    def snapshot(self, symbols: list[str]) -> dict[str, dict]:
        raise ProviderUnavailable(f"{self.kind} provider is not configured")

    def generate(self, market: str, snapshots: dict[str, dict]) -> str:
        raise ProviderUnavailable(f"{self.kind} provider is not configured")


class ProviderUnavailable(RuntimeError):
    pass


def provider_status(provider: object, kind: str) -> dict[str, str | bool]:
    allowed = ("configured", "name", "state", "kind")
    try:
        status = getattr(provider, "status", None)
        if isinstance(status, dict):
            safe = {key: status[key] for key in allowed if key in status}
            return {"configured": bool(safe.get("configured", True)),
                    "name": str(safe.get("name", type(provider).__name__)),
                    "state": str(safe.get("state", "ready")), "kind": kind}
        return {"configured": True, "name": str(getattr(provider, "name", type(provider).__name__)),
                "state": "ready", "kind": kind}
    except Exception:
        return {"configured": False, "name": type(provider).__name__,
                "state": "error", "kind": kind}
