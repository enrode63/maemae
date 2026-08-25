from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from .providers import ProviderUnavailable, UnconfiguredProvider, provider_status
from .schedule import schedule_state
from .scoring import TEAM_SCORE_CONFIGS, score_signals


@dataclass(frozen=True)
class AutomationConfig:
    rationale_min_length: int = 60


TRADING_TEAMS = frozenset({"scalping", "day", "swing", "longterm"})


def normalize_team(team: str | None) -> str | None:
    """Validate a reporting team and map the explicit overall scope to None."""
    if team is None or team == "all":
        return None
    if team not in TRADING_TEAMS:
        raise ValueError("invalid team")
    return team


class AutomationService:
    """Report and paper-ledger orchestration. It deliberately has no broker adapter.

    Atomic file replacement protects each save, while callers are expected to
    use one process per state directory; the in-process lock serializes workers.
    """
    def __init__(self, state_dir: Path, market_provider: object | None = None,
                 report_provider: object | None = None, config: AutomationConfig | None = None):
        self.path = Path(state_dir) / "automation-state.json"
        self.market = market_provider or UnconfiguredProvider("market_data")
        self.reporter = report_provider or UnconfiguredProvider("llm_report")
        self.config = config or AutomationConfig()
        self._lock = RLock()
        self._state = {"completed_jobs": {}, "reports": [], "trades": [], "evaluations": [], "decisions": []}
        if self.path.exists():
            self._state.update(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        return {"mode": "simulation", "live_ordering": False,
                "providers": {"market_data": provider_status(self.market, "market_data"),
                              "llm_report": provider_status(self.reporter, "llm_report")},
                "schedule": schedule_state(now), "rationale_min_length": self.config.rationale_min_length}

    def team_configs(self) -> dict[str, dict[str, Any]]:
        return {team: config.json() for team, config in TEAM_SCORE_CONFIGS.items()}

    def score(self, team: str, signals: object) -> dict[str, Any]:
        return {"team": team, "mode": "simulation", "live_ordering": False,
                **score_signals(team, signals)}

    def decide(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Persist paper eligibility only; this method has no order/fill adapter."""
        team = str(raw.get("team", ""))
        if team not in TRADING_TEAMS:
            raise ValueError("invalid team")
        symbol = str(raw.get("symbol", "")).upper().strip()
        if not symbol:
            raise ValueError("symbol is required")
        configured = provider_status(self.market, "market_data")["configured"]
        universe = self.universe()
        allowed_symbols = set(universe["us_equities"]) | set(universe["crypto"])
        if symbol not in allowed_symbols:
            raise ValueError("symbol is outside the S&P500, Nasdaq100, BTC/ETH universe")
        result = score_signals(team, raw.get("signals") if configured else None)
        quality = str(raw.get("data_quality", "realtime")).lower()
        if quality not in {"realtime", "delayed", "free"}:
            result["decision"] = "NO_TRADE"
            result["missing_signals"] = sorted(set(result["missing_signals"] + ["valid_data_quality"]))
        if quality == "free":
            result["decision"] = "NO_TRADE"
            result["missing_signals"] = sorted(set(result["missing_signals"] + ["realtime_data"]))
        if not configured:
            result["decision"] = "NO_TRADE"
            result["missing_signals"] = sorted(set(result["missing_signals"] + ["market_data_provider"]))
        rationale = str(raw.get("rationale", "")).strip()
        if not rationale:
            state = "met" if result["decision"] == "PAPER_TRADE" else "did not meet"
            rationale = (f"The {team} ensemble score {state} its configured threshold; the decision uses "
                         f"{quality} market data and remains simulation-only with no live order route.")
        if len(rationale) < self.config.rationale_min_length:
            raise ValueError(f"decision rationale must be at least {self.config.rationale_min_length} characters")
        decision = {"id": uuid4().hex, "symbol": symbol, "team": team, "mode": "simulation",
                    "live_ordering": False, "data_quality": quality, **result, "rationale": rationale,
                    "created_at": str(raw.get("created_at") or datetime.now(timezone.utc).isoformat())}
        with self._lock:
            self._state["decisions"].append(decision)
            self._save()
        return decision

    def decisions(self, team: str | None = None) -> list[dict[str, Any]]:
        selected = normalize_team(team)
        with self._lock:
            return [dict(item) for item in self._state["decisions"]
                    if selected is None or item["team"] == selected]

    def universe(self) -> dict[str, Any]:
        if not provider_status(self.market, "market_data")["configured"]:
            return {"status": "not_configured", "us_equities": [], "crypto": ["BTC", "ETH"],
                    "rule": "S&P500 union Nasdaq100, duplicates removed"}
        sp = self.market.symbols("sp500")
        ndx = self.market.symbols("nasdaq100")
        return {"status": "ready", "us_equities": sorted(set(sp) | set(ndx)), "crypto": ["BTC", "ETH"],
                "counts": {"sp500": len(set(sp)), "nasdaq100": len(set(ndx)), "deduplicated": len(set(sp) | set(ndx))}}

    def run_due(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        schedule = schedule_state(now)
        results = []
        with self._lock:
            for market in ("us", "crypto"):
                key = f"{market}:{schedule[market]['local_date']}"
                if not schedule[market]["due"] or key in self._state["completed_jobs"]:
                    results.append({"market": market, "state": "not_due_or_already_completed"})
                    continue
                try:
                    universe = self.universe()
                    symbols = universe["us_equities"] if market == "us" else universe["crypto"]
                    if not symbols:
                        raise ProviderUnavailable("market universe unavailable")
                    snapshots = self.market.snapshot(symbols)
                    if not isinstance(snapshots, dict):
                        raise ProviderUnavailable("market snapshot unavailable")
                    report_status = "ready"
                    try:
                        content = self.reporter.generate(market, snapshots)
                    except ProviderUnavailable:
                        content, report_status = "Report provider is not configured.", "not_configured"
                    except Exception:
                        content, report_status = "Report provider failed safely.", "provider_error"
                    cycle_decisions, cycle_trades = self._process_snapshots(
                        market, key, symbols, snapshots, now)
                    report = {"id": uuid4().hex, "market": market, "scheduled_date": schedule[market]["local_date"],
                              "created_at": now.isoformat(), "symbols": symbols, "content": str(content),
                              "status": report_status,
                              "decision_ids": [item["id"] for item in cycle_decisions],
                              "trade_ids": [item["id"] for item in cycle_trades]}
                    self._state["reports"].append(report)
                    self._state["completed_jobs"][key] = report["id"]
                    results.append({"market": market, "state": "generated", "report_id": report["id"],
                                    "decisions": len(cycle_decisions), "trades": len(cycle_trades)})
                except ProviderUnavailable as exc:
                    state = ("provider_unavailable" if provider_status(self.market, "market_data")["configured"]
                             else "blocked_not_configured")
                    results.append({"market": market, "state": state,
                                    "decision": "NO_TRADE", "trades": 0, "reason": str(exc)})
                except Exception:
                    results.append({"market": market, "state": "provider_error",
                                    "decision": "NO_TRADE", "trades": 0})
            self._save()
        return {"mode": "simulation", "jobs": results}

    def _process_snapshots(self, market: str, cycle_key: str, symbols: list[str],
                           snapshots: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Select one candidate per team and append simulation-only ledger entries."""
        decisions, trades = [], []
        for team in sorted(TRADING_TEAMS):
            candidates = []
            for symbol in symbols:
                snapshot = snapshots.get(symbol)
                snapshot = snapshot if isinstance(snapshot, dict) else {}
                teams = snapshot.get("teams") if isinstance(snapshot.get("teams"), dict) else {}
                team_value = teams.get(team, snapshot.get(team, {}))
                team_value = team_value if isinstance(team_value, dict) else {}
                signal_sets = snapshot.get("signals") if isinstance(snapshot.get("signals"), dict) else {}
                signals = team_value.get("signals", signal_sets.get(team))
                scored = score_signals(team, signals)
                candidates.append((scored["score"], symbol, snapshot, team_value, scored))
            _, symbol, snapshot, team_value, scored = max(candidates, key=lambda value: (value[0], value[1]))
            quality = str(team_value.get("data_quality", snapshot.get("data_quality", "realtime"))).lower()
            side = str(team_value.get("side", snapshot.get("side", ""))).upper()
            if quality != "realtime":
                scored["decision"] = "NO_TRADE"
                scored["missing_signals"] = sorted(set(scored["missing_signals"] + ["realtime_data"]))
            if side not in {"BUY", "SELL"}:
                scored["decision"] = "NO_TRADE"
                scored["missing_signals"] = sorted(set(scored["missing_signals"] + ["buy_or_sell_side"]))
            rationale = (f"The {team} ensemble selected {symbol} as the highest-scoring {market} candidate "
                         f"for this scheduled simulation cycle; data checks and threshold rules produced {scored['decision']}.")
            decision = {"id": uuid4().hex, "cycle_key": cycle_key, "market": market, "symbol": symbol,
                        "team": team, "mode": "simulation", "live_ordering": False,
                        "data_quality": quality, "action": side if scored["decision"] == "PAPER_TRADE" else "NO_TRADE",
                        **scored, "rationale": rationale, "created_at": now.isoformat()}
            self._state["decisions"].append(decision)
            decisions.append(decision)
            if scored["decision"] != "PAPER_TRADE":
                continue
            price = team_value.get("price", snapshot.get("price", snapshot.get("close")))
            quantity = team_value.get("quantity", snapshot.get("quantity", 1))
            try:
                price_value, quantity_value = Decimal(str(price)), Decimal(str(quantity))
            except Exception:
                decision["decision"], decision["action"] = "NO_TRADE", "NO_TRADE"
                decision["missing_signals"] = sorted(set(decision["missing_signals"] + ["valid_price_quantity"]))
                continue
            if (not price_value.is_finite() or not quantity_value.is_finite()
                    or price_value <= 0 or quantity_value <= 0):
                decision["decision"], decision["action"] = "NO_TRADE", "NO_TRADE"
                decision["missing_signals"] = sorted(set(decision["missing_signals"] + ["valid_price_quantity"]))
                continue
            trade = {"id": uuid4().hex, "cycle_key": cycle_key, "decision_id": decision["id"],
                     "mode": "simulation", "live_ordering": False, "market": market, "symbol": symbol,
                     "team": team, "side": side, "quantity": str(quantity_value), "price": str(price_value),
                     "score": scored["score"], "signal_breakdown": scored["signal_breakdown"],
                     "rationale": rationale, "risk": "0", "reward": "0", "pnl": "0", "return_pct": "0",
                     "created_at": now.isoformat()}
            self._state["trades"].append(trade)
            trades.append(trade)
        return decisions, trades

    def trades(self, team: str | None = None) -> list[dict[str, Any]]:
        selected = normalize_team(team)
        with self._lock:
            return [dict(item) for item in self._state["trades"]
                    if selected is None or item["team"] == selected]

    def reports(self, team: str | None = None) -> dict[str, Any]:
        with self._lock:
            selected = normalize_team(team)
            performance = self.performance(selected)
            allocation_values = {key: Decimal(value) for key, value in performance["asset_allocation"].items()}
            allocation_total = sum(allocation_values.values(), Decimal("0"))
            allocation = [{"asset": asset, "weight": float(value / allocation_total * 100)}
                          for asset, value in allocation_values.items()] if allocation_total else []
            evaluations = [item for item in self._state["evaluations"]
                           if selected is None or item["team"] == selected]
            latest = evaluations[-1] if evaluations else {}
            report_teams = [selected] if selected else sorted(TRADING_TEAMS)
            teams = []
            for name in report_teams:
                item = self.performance(name)
                teams.append({"team": name, "teamName": name, "title": "주간 리포트",
                              "returnRate": float(item["return_pct"]),
                              "summary": f"{item['trade_count']} paper trades recorded."})
            ensemble_teams = []
            if provider_status(self.market, "market_data")["configured"]:
                latest_decisions: dict[str, dict[str, Any]] = {}
                for decision in reversed(self._state["decisions"]):
                    decision_team = decision.get("team")
                    if (selected is None or decision_team == selected) and decision_team not in latest_decisions:
                        latest_decisions[decision_team] = decision
                for name in report_teams:
                    decision = latest_decisions.get(name)
                    if decision is None:
                        continue
                    ensemble_teams.append({
                        key: decision[key] for key in (
                            "id", "symbol", "team", "score", "decision", "threshold", "timeframe",
                            "signal_breakdown", "rationale", "missing_signals", "created_at",
                        ) if key in decision
                    })
                    ensemble_teams[-1]["status"] = (
                        decision["data_quality"] if decision.get("data_quality") in {"delayed", "free"} else "ready"
                    )
            ensemble_updated_at = max(
                (item.get("created_at", "") for item in ensemble_teams), default=""
            ) or None
            decision_items = [dict(item) for item in self._state["decisions"]
                              if selected is None or item["team"] == selected]
            trade_items = [dict(item) for item in self._state["trades"]
                           if selected is None or item["team"] == selected]
            return {"kpis": {"cumulativePnl": float(performance["pnl"]),
                              "averageRr": float(performance["rr"]) if performance["rr"] is not None else None,
                              "positions": sum(Decimal(value) != 0 for value in performance["positions"].values()),
                              "winRate": float(Decimal(performance["win_rate"]) * 100) if performance["win_rate"] is not None else None,
                              "returnRate": float(performance["return_pct"])},
                    "teams": teams, "assetAllocation": allocation,
                    "weekly": {"period": latest.get("week", "최근 7일"),
                               "returnRate": float(latest.get("performance", {}).get("return_pct", 0)),
                               "wins": latest.get("strengths", []),
                               "improvements": latest.get("improvements", [])},
                    "sentiment": {"crypto": {}, "usStocks": {}},
                    "ensemble": {"updatedAt": ensemble_updated_at, "teams": ensemble_teams},
                    "trades": trade_items, "decisions": decision_items, "performance": performance,
                    "updatedAt": datetime.now(timezone.utc).isoformat()}

    def record_trade(self, raw: dict[str, Any]) -> dict[str, Any]:
        required = ("symbol", "team", "side", "quantity", "price", "rationale")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"missing trade fields: {', '.join(missing)}")
        side = str(raw["side"]).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        rationale = str(raw["rationale"]).strip()
        if len(rationale) < self.config.rationale_min_length:
            raise ValueError(f"entry rationale must be at least {self.config.rationale_min_length} characters")
        team = str(raw["team"])
        if team not in TRADING_TEAMS:
            raise ValueError("invalid team")
        symbol = str(raw["symbol"]).upper()
        universe = self.universe()
        allowed_symbols = set(universe["us_equities"]) | set(universe["crypto"])
        if symbol not in allowed_symbols:
            raise ValueError("symbol is outside the S&P500, Nasdaq100, BTC/ETH universe")
        quantity, price = Decimal(str(raw["quantity"])), Decimal(str(raw["price"]))
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be finite and positive")
        numeric = {}
        for key in ("risk", "reward", "pnl", "return_pct"):
            numeric[key] = Decimal(str(raw.get(key, "0")))
            if not numeric[key].is_finite() or (key in {"risk", "reward"} and numeric[key] < 0):
                raise ValueError(f"{key} must be finite and non-negative where applicable")
        trade = {"id": uuid4().hex, "mode": "simulation", "live_ordering": False, "symbol": symbol,
                 "team": team, "side": side, "quantity": str(quantity), "price": str(price),
                 "rationale": rationale, **{key: str(value) for key, value in numeric.items()},
                 "created_at": str(raw.get("created_at") or datetime.now(timezone.utc).isoformat())}
        with self._lock:
            self._state["trades"].append(trade)
            self._save()
        return trade

    def record_simulation_fill(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Idempotently append a fill produced by the local simulation engine."""
        required = ("fill_id", "symbol", "team", "side", "quantity", "price", "rationale")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"missing simulation fill fields: {', '.join(missing)}")
        fill_id = str(raw["fill_id"]).strip()
        symbol = str(raw["symbol"]).upper().strip()
        team = str(raw["team"])
        side = str(raw["side"]).upper()
        rationale = str(raw["rationale"]).strip()
        if not fill_id or not symbol:
            raise ValueError("fill_id and symbol must be non-empty")
        if team not in TRADING_TEAMS:
            raise ValueError("invalid team")
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if len(rationale) < self.config.rationale_min_length:
            raise ValueError(f"entry rationale must be at least {self.config.rationale_min_length} characters")
        quantity, price = Decimal(str(raw["quantity"])), Decimal(str(raw["price"]))
        pnl = Decimal(str(raw.get("pnl", "0")))
        if (not quantity.is_finite() or not price.is_finite() or not pnl.is_finite()
                or quantity <= 0 or price <= 0):
            raise ValueError("quantity and price must be finite and positive; pnl must be finite")
        with self._lock:
            existing = next((item for item in self._state["trades"]
                             if item.get("source_fill_id") == fill_id), None)
            if existing is not None:
                return dict(existing)
            trade = {"id": uuid4().hex, "source_fill_id": fill_id, "mode": "simulation",
                     "live_ordering": False, "symbol": symbol, "team": team, "side": side,
                     "quantity": str(quantity), "price": str(price), "rationale": rationale,
                     "risk": "0", "reward": "0", "pnl": str(pnl), "return_pct": "0",
                     "created_at": str(raw.get("created_at") or datetime.now(timezone.utc).isoformat())}
            self._state["trades"].append(trade)
            self._save()
            return dict(trade)

    def performance(self, team: str | None = None) -> dict[str, Any]:
        selected = normalize_team(team)
        trades = [t for t in self._state["trades"] if selected is None or t["team"] == selected]
        pnl = sum((Decimal(t["pnl"]) for t in trades), Decimal("0"))
        returns = sum((Decimal(t["return_pct"]) for t in trades), Decimal("0"))
        closed = [t for t in trades if Decimal(t["pnl"]) != 0]
        wins = sum(Decimal(t["pnl"]) > 0 for t in closed)
        risk = sum((Decimal(t["risk"]) for t in trades), Decimal("0"))
        reward = sum((Decimal(t["reward"]) for t in trades), Decimal("0"))
        positions: dict[str, Decimal] = {}
        allocation: dict[str, Decimal] = {}
        for t in trades:
            signed = Decimal(t["quantity"]) * (1 if t["side"] == "BUY" else -1)
            positions[t["symbol"]] = positions.get(t["symbol"], Decimal("0")) + signed
            allocation[t["symbol"]] = allocation.get(t["symbol"], Decimal("0")) + abs(Decimal(t["quantity"]) * Decimal(t["price"]))
        return {"scope": "team" if selected else "overall", "team": selected, "pnl": str(pnl),
                "rr": str(reward / risk) if risk else None, "positions": {k: str(v) for k, v in sorted(positions.items())},
                "win_rate": str(Decimal(wins) / len(closed)) if closed else None, "return_pct": str(returns),
                "asset_allocation": {k: str(v) for k, v in sorted(allocation.items())}, "trade_count": len(trades)}

    def weekly_evaluation(self, team: str | None, week: str, strengths: list[str], improvements: list[str]) -> dict[str, Any]:
        selected = normalize_team(team)
        value = {"team": selected or "all", "week": week, "performance": self.performance(selected),
                 "strengths": [str(x) for x in strengths], "improvements": [str(x) for x in improvements]}
        with self._lock:
            self._state["evaluations"].append(value)
            self._save()
        return value

    def evaluations(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._state["evaluations"])
