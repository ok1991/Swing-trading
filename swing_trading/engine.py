"""Target-weight executor for the approved staggered ETF rotation strategy."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .costs import DEFAULT_COST_MODEL, ExecutionCostModel
from .rotation_contract import validate_rotation_contract
from .utils import safe_float


class TradingEngine:
    """Execute only approved rotation targets; the former event-entry path is disabled."""

    def __init__(
        self,
        state: Dict[str, Any],
        rotation: Dict[str, Any],
        prices: Dict[str, float],
        run_date: Optional[str] = None,
        allow_rebalance: bool = True,
        source_diagnostics: Optional[Dict[str, Any]] = None,
        cost_model: ExecutionCostModel = DEFAULT_COST_MODEL,
        allow_valuation: bool = True,
    ) -> None:
        self.state = state
        self.rotation = rotation
        self.prices = {str(code): safe_float(price) for code, price in prices.items()}
        self.run_date = datetime.strptime(run_date, "%Y-%m-%d") if run_date else datetime.now()
        self.allow_rebalance = bool(allow_rebalance)
        self.allow_valuation = bool(allow_valuation)
        self.source_diagnostics = dict(source_diagnostics or {})
        self.cost_model = cost_model
        self.buy_orders: List[Dict[str, Any]] = []
        self.sell_orders: List[Dict[str, Any]] = []
        self.decision_diagnostics: Dict[str, Any] = {
            "mode": "APPROVED_ROTATION_ONLY",
            "rebalance_required": False,
            "plan_id": self._plan_id(),
            "reasons": [],
            "execution_status": "IDLE",
        }

    def _target_weights(self) -> Dict[str, float]:
        return {
            str(code): safe_float(weight)
            for code, weight in (self.rotation.get("target_weights") or {}).items()
            if safe_float(weight) > 0.0
        }

    def _names(self) -> Dict[str, str]:
        return {
            str(item.get("code")): str(item.get("name") or item.get("code"))
            for item in self.rotation.get("top_candidates", [])
            if item.get("code")
        }

    def _average_daily_amount(
        self,
        code: str,
        holding: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        liquidity = self._liquidity(code)
        value = safe_float(liquidity.get("average_daily_amount_20"))
        if value <= 0.0:
            value = safe_float((holding or {}).get("average_daily_amount_20"))
        return value if value > 0.0 else None

    def _liquidity(self, code: str) -> Dict[str, Any]:
        return dict(
            (self.rotation.get("execution_liquidity") or {}).get(str(code)) or {}
        )

    def _plan_id(self) -> str:
        authority = {
            "model_version": self.rotation.get("model_version", ""),
            "execution_policy_version": self.rotation.get("execution_policy_version", ""),
            "acceptance_policy_version": self.rotation.get("acceptance_policy_version", ""),
            "strategy_specification_fingerprint": self.rotation.get(
                "strategy_specification_fingerprint", ""
            ),
            "sleeves": list(self.rotation.get("sleeves") or []),
            "target_weights": self.rotation.get("target_weights", {}),
            "max_exposure_ratio": self.rotation.get("max_exposure_ratio", 0.0),
        }
        digest = hashlib.sha256(
            json.dumps(authority, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"{authority['model_version']}:{digest}"

    def _holdings_by_code(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for item in self.state.get("holdings", []):
            code = str(item.get("code", ""))
            if not code:
                continue
            if code not in result:
                result[code] = dict(item)
                result[code]["shares"] = int(item.get("shares", 0))
                continue
            prior_shares = int(result[code].get("shares", 0))
            added_shares = int(item.get("shares", 0))
            total = prior_shares + added_shares
            if total > 0:
                result[code]["buy_price"] = (
                    safe_float(result[code].get("buy_price")) * prior_shares
                    + safe_float(item.get("buy_price")) * added_shares
                ) / total
            result[code]["shares"] = total
        return result

    def _price(self, code: str, holding: Optional[Dict[str, Any]] = None) -> float:
        del holding
        return safe_float(self.prices.get(code))

    def _total_assets(
        self,
        holdings: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[float]:
        holdings = holdings or self._holdings_by_code()
        has_positions = any(
            int(item.get("shares", 0)) > 0 for item in holdings.values()
        )
        if has_positions and not self.allow_valuation:
            return None
        if any(
            int(item.get("shares", 0)) > 0 and self._price(code) <= 0.0
            for code, item in holdings.items()
        ):
            return None
        return safe_float(self.state.get("cash")) + sum(
            int(item.get("shares", 0)) * self._price(code, item)
            for code, item in holdings.items()
        )

    def _all_prices_available(
        self,
        targets: Dict[str, float],
        holdings: Dict[str, Dict[str, Any]],
    ) -> bool:
        missing = [
            code
            for code in sorted(set(targets) | set(holdings))
            if safe_float(self.prices.get(code)) <= 0.0
        ]
        if missing:
            self.decision_diagnostics["reasons"].append(
                {"code": "MISSING_PRICE", "message": "缺少行情: " + ", ".join(missing)}
            )
            return False
        return True

    def _record_trade(
        self,
        side: str,
        code: str,
        name: str,
        shares: int,
        price: float,
        reason: str,
        execution: Dict[str, float],
    ) -> Dict[str, Any]:
        order = {
            "side": side,
            "code": code,
            "name": name,
            "shares": int(shares),
            "price": round(price, 4),
            "effective_price": round(execution["effective_price"], 6),
            "gross": round(execution["gross"], 4),
            "commission": round(execution["commission"], 4),
            "other_fees": round(execution["other_fees"], 4),
            "slippage": round(execution["slippage"], 4),
            "total_cost": round(execution["total_cost"], 4),
            "average_daily_amount": round(execution.get("average_daily_amount", 0.0), 2),
            "participation_rate": round(execution.get("participation_rate", 0.0), 8),
            "requested_participation_rate": round(
                execution.get("requested_participation_rate", 0.0), 8
            ),
            "capacity_exceeded": bool(execution.get("capacity_exceeded", False)),
            "liquidity_capped": bool(execution.get("liquidity_capped", False)),
            "requested_shares": int(execution.get("requested_shares", shares)),
            "unfilled_shares": int(execution.get("unfilled_shares", 0)),
            "requested_gross": round(execution.get("requested_gross", execution["gross"]), 4),
            "unfilled_gross": round(execution.get("unfilled_gross", 0.0), 4),
            "new_risk_capacity_amount": round(
                execution.get("new_risk_capacity_amount", 0.0), 4
            ),
            "capacity_utilization_rate": round(
                execution.get("capacity_utilization_rate", 0.0), 8
            ),
            "capacity_headroom_amount": round(
                execution.get("capacity_headroom_amount", 0.0), 4
            ),
            "impact_bps": round(execution.get("impact_bps", 0.0), 6),
            "reason": reason,
            "model_version": str(self.rotation.get("model_version", "")),
        }
        self.state["trade_history"].append(
            {**order, "date": self.run_date.strftime("%Y-%m-%d")}
        )
        self.state["cumulative_execution_cost"] = round(
            safe_float(self.state.get("cumulative_execution_cost"))
            + execution["total_cost"],
            4,
        )
        return order

    def _execute_sell(
        self,
        holdings: Dict[str, Dict[str, Any]],
        code: str,
        target_shares: int,
        names: Dict[str, str],
    ) -> None:
        holding = holdings[code]
        current = int(holding.get("shares", 0))
        shares = current - target_shares
        if shares <= 0:
            return
        price = self._price(code, holding)
        execution = self.cost_model.estimate(
            "SELL",
            price,
            shares,
            average_daily_amount=self._average_daily_amount(code, holding),
        )
        self.state["cash"] = round(
            safe_float(self.state.get("cash")) + execution["cash_delta"], 4
        )
        reason = "ROTATION_EXIT" if target_shares == 0 else "ROTATION_REDUCE"
        order = self._record_trade(
            "SELL", code, names.get(code, str(holding.get("name", code))), shares, price, reason, execution
        )
        self.sell_orders.append(order)
        if target_shares > 0:
            holding["shares"] = target_shares
        else:
            holdings.pop(code, None)

    def _execute_buy(
        self,
        holdings: Dict[str, Dict[str, Any]],
        code: str,
        target_shares: int,
        names: Dict[str, str],
    ) -> None:
        current_holding = holdings.get(code, {})
        current = int(current_holding.get("shares", 0))
        requested_shares = target_shares - current
        shares = requested_shares
        if requested_shares <= 0:
            return
        price = self._price(code, current_holding)
        average_daily_amount = self._average_daily_amount(code, current_holding)
        liquidity = self._liquidity(code)
        published_capacity_amount = safe_float(liquidity.get("max_new_risk_amount"))
        capacity_shares = self.cost_model.capacity_lot(price, average_daily_amount)
        shares = min(shares, capacity_shares)
        liquidity_capped = shares < requested_shares
        if liquidity_capped:
            self.decision_diagnostics["reasons"].append(
                {
                    "code": "LIQUIDITY_CAP_REACHED",
                    "message": (
                        f"{code} 新增目标 {requested_shares} 份超过单日10% ADV容量，"
                        f"本次最多执行 {shares} 份"
                    ),
                }
            )
        execution = self.cost_model.estimate(
            "BUY", price, shares, average_daily_amount=average_daily_amount
        )
        while shares > 0 and -execution["cash_delta"] > safe_float(self.state.get("cash")):
            shares -= self.cost_model.lot_size
            execution = self.cost_model.estimate(
                "BUY", price, shares, average_daily_amount=average_daily_amount
            )
        if shares <= 0:
            self.decision_diagnostics["reasons"].append(
                {"code": "INSUFFICIENT_CASH", "message": f"{code} 资金不足一个交易单位"}
            )
            return
        execution["requested_shares"] = requested_shares
        execution["liquidity_capped"] = liquidity_capped
        execution["unfilled_shares"] = max(requested_shares - shares, 0)
        execution["requested_gross"] = max(requested_shares, 0) * price
        execution["unfilled_gross"] = execution["unfilled_shares"] * price
        execution["new_risk_capacity_amount"] = published_capacity_amount
        execution["capacity_utilization_rate"] = (
            execution["gross"] / published_capacity_amount
            if published_capacity_amount > 0.0 else 0.0
        )
        execution["capacity_headroom_amount"] = max(
            published_capacity_amount - execution["gross"], 0.0
        )
        self.state["cash"] = round(
            safe_float(self.state.get("cash")) + execution["cash_delta"], 4
        )
        name = names.get(code, str(current_holding.get("name", code)))
        order = self._record_trade(
            "BUY", code, name, shares, price, "ROTATION_INCREASE", execution
        )
        self.buy_orders.append(order)
        total_shares = current + shares
        prior_cost = safe_float(current_holding.get("buy_price")) * current
        current_holding.update(
            {
                "code": code,
                "name": name,
                "shares": total_shares,
                "buy_price": round((prior_cost + execution["gross"] + execution["total_cost"]) / total_shares, 6),
                "buy_date": current_holding.get("buy_date") or self.run_date.strftime("%Y-%m-%d"),
                "source": "approved_rotation",
                "model_version": str(self.rotation.get("model_version", "")),
                "average_daily_amount_20": round(float(average_daily_amount or 0.0), 2),
            }
        )
        holdings[code] = current_holding

    def _derive_execution_status(self) -> str:
        reasons = self.decision_diagnostics.get("reasons") or []
        codes = {str(item.get("code")) for item in reasons if isinstance(item, dict)}
        awaiting_codes = {
            "PLAN_AWAITING_BROKER_CONFIRMATION",
            "PRIOR_PLAN_AWAITING_BROKER_CONFIRMATION",
        }
        if codes & awaiting_codes:
            return "AWAITING_BROKER_CONFIRMATION"
        if "PORTFOLIO_ALREADY_AT_TARGET" in codes:
            return "NO_ACTION_NEEDED"
        if reasons:
            return "BLOCKED"
        if self.buy_orders or self.sell_orders:
            return "ORDERS_ISSUED"
        return "NO_ACTION_NEEDED"

    def _independent_risk_gate(self, targets: Dict[str, float]) -> Optional[Dict[str, str]]:
        max_exposure_ratio = safe_float(self.rotation.get("max_exposure_ratio"), 1.0)
        total_weight = sum(safe_float(weight) for weight in targets.values())
        if total_weight > max_exposure_ratio + 0.02:
            return {
                "code": "INDEPENDENT_RISK_GATE_FAILED",
                "message": f"目标权重合计 {total_weight:.4f} 超过独立风控上限 {max_exposure_ratio + 0.02:.4f}",
            }
        for code, weight in targets.items():
            if safe_float(weight) > 0.5:
                return {
                    "code": "INDEPENDENT_RISK_GATE_FAILED",
                    "message": f"单一标的目标权重 {code} 达到 {safe_float(weight):.4f}，超过独立风控上限 0.5",
                }
            if safe_float(weight) < 0.0:
                return {
                    "code": "INDEPENDENT_RISK_GATE_FAILED",
                    "message": "目标权重存在负数: " + str(code),
                }
        return None

    def _rebalance(self) -> None:
        plan_id = self._plan_id()
        if not self.allow_rebalance:
            self.decision_diagnostics["reasons"].append(
                {"code": "SOURCE_BLOCKED", "message": "轮动目标不可用于调仓，保持现有持仓"}
            )
            return
        if self.rotation.get("approved") is not True:
            self.decision_diagnostics["reasons"].append(
                {"code": "MODEL_NOT_APPROVED", "message": "轮动模型未通过验收"}
            )
            return
        contract_errors = validate_rotation_contract(self.rotation)
        if contract_errors:
            self.decision_diagnostics["reasons"].append(
                {
                    "code": "INVALID_ROTATION_CONTRACT",
                    "message": "; ".join(contract_errors[:5]),
                }
            )
            return
        execution_date = str(self.rotation.get("execution_date", ""))[:10]
        run_date = self.run_date.strftime("%Y-%m-%d")
        if run_date != execution_date:
            self.decision_diagnostics["reasons"].append(
                {
                    "code": "EXECUTION_DATE_MISMATCH",
                    "message": f"当前日期 {run_date} 不是指定执行日 {execution_date}",
                }
            )
            return
        recorded_cost = (self.rotation.get("walk_forward_metrics") or {}).get("cost_model") or {}
        engine_cost = self.cost_model.to_dict()
        mismatched_cost_fields = []
        for field, expected in engine_cost.items():
            try:
                recorded = float(recorded_cost.get(field))
            except (TypeError, ValueError):
                mismatched_cost_fields.append(field)
                continue
            tolerance = 0.0 if field == "lot_size" else 1e-12
            if abs(recorded - float(expected)) > tolerance:
                mismatched_cost_fields.append(field)
        if mismatched_cost_fields:
            self.decision_diagnostics["reasons"].append(
                {
                    "code": "ENGINE_COST_MODEL_MISMATCH",
                    "message": "执行成本口径不一致: " + ", ".join(mismatched_cost_fields),
                }
            )
            return
        pending_plan_id = str(
            self.state.get("pending_broker_confirmation_plan_id", "")
        )
        if pending_plan_id:
            code = (
                "PLAN_AWAITING_BROKER_CONFIRMATION"
                if pending_plan_id == plan_id
                else "PRIOR_PLAN_AWAITING_BROKER_CONFIRMATION"
            )
            self.decision_diagnostics["reasons"].append(
                {
                    "code": code,
                    "message": "已有模型估算订单等待券商成交确认，冻结后续调仓",
                }
            )
            return
        if (
            plan_id == self.state.get("last_plan_id")
            and plan_id
            != str(self.state.get("last_execution_satisfied_plan_id", ""))
        ):
            self.decision_diagnostics["reasons"].append(
                {
                    "code": "PLAN_STATE_PROVENANCE_MISSING",
                    "message": "计划状态缺少券商确认或明确的零订单满足证据",
                }
            )
            return
        if plan_id == self.state.get("last_plan_id"):
            self.decision_diagnostics["reasons"].append(
                {"code": "PLAN_ALREADY_APPLIED", "message": "本轮目标已执行，不做日内重复再平衡"}
            )
            return

        targets = self._target_weights()
        gate_reason = self._independent_risk_gate(targets)
        if gate_reason is not None:
            self.decision_diagnostics["reasons"].append(gate_reason)
            return
        holdings = self._holdings_by_code()
        if not self._all_prices_available(targets, holdings):
            return
        self.decision_diagnostics["rebalance_required"] = True
        names = self._names()
        total_assets = self._total_assets(holdings)
        if total_assets is None:
            self.decision_diagnostics["reasons"].append(
                {"code": "VALUATION_BLOCKED", "message": "持仓缺少实时行情，无法计算组合资产"}
            )
            return
        desired = {
            code: self.cost_model.round_lot(total_assets * weight / self._price(code, holdings.get(code)))
            for code, weight in targets.items()
        }

        if all(
            int(holdings.get(code, {}).get("shares", 0)) == int(desired.get(code, 0))
            for code in set(holdings) | set(desired)
        ):
            self.decision_diagnostics["reasons"].append(
                {
                    "code": "PORTFOLIO_ALREADY_AT_TARGET",
                    "message": "当前组合已与获批目标仓位一致，无需生成订单",
                }
            )
            self.state["last_plan_id"] = plan_id
            self.state["last_model_version"] = str(
                self.rotation.get("model_version", "")
            )
            self.state["pending_broker_confirmation_plan_id"] = ""
            self.state["last_execution_satisfied_plan_id"] = plan_id
            return

        for code in sorted(set(holdings) | set(desired)):
            current = int(holdings.get(code, {}).get("shares", 0))
            target = int(desired.get(code, 0))
            if current > target:
                self._execute_sell(holdings, code, target, names)

        for code in sorted(desired, key=lambda value: (-targets[value], value)):
            current = int(holdings.get(code, {}).get("shares", 0))
            if desired[code] > current:
                self._execute_buy(holdings, code, desired[code], names)

        self.state["holdings"] = list(holdings.values())
        if self.buy_orders or self.sell_orders:
            self.state["last_plan_id"] = plan_id
            self.state["last_model_version"] = str(
                self.rotation.get("model_version", "")
            )
            self.state["pending_broker_confirmation_plan_id"] = plan_id
            self.state["last_execution_satisfied_plan_id"] = ""

    def _refresh_metrics(self) -> None:
        assets = self._total_assets()
        if assets is None:
            if not any(
                item.get("code") == "VALUATION_BLOCKED"
                for item in self.decision_diagnostics["reasons"]
            ):
                self.decision_diagnostics["reasons"].append(
                    {"code": "VALUATION_BLOCKED", "message": "持仓缺少实时行情，资产与回撤不更新"}
                )
            return
        peak = max(safe_float(self.state.get("peak_capital"), assets), assets)
        drawdown = assets / peak - 1.0 if peak > 0 else 0.0
        self.state["peak_capital"] = round(peak, 4)
        self.state["max_drawdown"] = round(
            min(safe_float(self.state.get("max_drawdown")), drawdown), 6
        )
        self.state["last_run"] = self.run_date.strftime("%Y-%m-%d")

    def _actual_weights(self) -> Dict[str, float]:
        holdings = self._holdings_by_code()
        assets = self._total_assets(holdings)
        if assets is None or assets <= 0:
            return {}
        return {
            code: round(int(item.get("shares", 0)) * self._price(code, item) / assets, 6)
            for code, item in sorted(holdings.items())
        }

    def _capacity_summary(self) -> Dict[str, Any]:
        liquidity = self.rotation.get("execution_liquidity") or {}
        target_codes = self._target_weights()
        published_capacity = sum(
            safe_float((liquidity.get(code) or {}).get("max_new_risk_amount"))
            for code in target_codes
        )
        requested = sum(safe_float(item.get("requested_gross")) for item in self.buy_orders)
        executed = sum(safe_float(item.get("gross")) for item in self.buy_orders)
        unfilled = sum(safe_float(item.get("unfilled_gross")) for item in self.buy_orders)
        return {
            "published_new_risk_capacity_amount": round(published_capacity, 4),
            "requested_new_risk_amount": round(requested, 4),
            "executed_new_risk_amount": round(executed, 4),
            "unfilled_new_risk_amount": round(unfilled, 4),
            "remaining_capacity_headroom_amount": round(
                max(published_capacity - executed, 0.0), 4
            ),
            "capacity_utilization_rate": round(
                executed / published_capacity if published_capacity > 0.0 else 0.0,
                8,
            ),
            "buy_fill_ratio": round(
                executed / requested if requested > 0.0 else 1.0,
                8,
            ),
            "capacity_truncation_count": sum(
                bool(item.get("liquidity_capped")) for item in self.buy_orders
            ),
        }

    def run(self) -> Dict[str, Any]:
        self._rebalance()
        self.decision_diagnostics["execution_status"] = self._derive_execution_status()
        self._refresh_metrics()
        total_assets = self._total_assets()
        return {
            "schema_version": 1,
            "strategy": "approved_rotation_only",
            "run_date": self.run_date.strftime("%Y-%m-%d"),
            "model_version": self.rotation.get("model_version", ""),
            "execution_policy_version": self.rotation.get("execution_policy_version", ""),
            "acceptance_policy_version": self.rotation.get("acceptance_policy_version", ""),
            "strategy_specification_fingerprint": self.rotation.get("strategy_specification_fingerprint", ""),
            "data_date": self.rotation.get("data_date", ""),
            "execution_date": self.rotation.get("execution_date", ""),
            "approved": self.rotation.get("approved"),
            "risk_control_only": not self.allow_rebalance,
            "target_weights": self._target_weights(),
            "max_exposure_ratio": safe_float(self.rotation.get("max_exposure_ratio")),
            "cash_weight": safe_float(self.rotation.get("cash_weight")),
            "market_policy": dict(self.rotation.get("market_policy") or {}),
            "actual_weights": self._actual_weights(),
            "rotation_source": self.source_diagnostics,
            "buy_orders": self.buy_orders,
            "sell_orders": self.sell_orders,
            "decision_diagnostics": self.decision_diagnostics,
            "execution_cost_model": {
                **self.cost_model.to_dict(),
                "transfer_fee_rate": 0.0,
                "stamp_duty_sell_rate": 0.0,
            },
            "execution_cost_this_run": round(
                sum(item["total_cost"] for item in self.buy_orders + self.sell_orders), 4
            ),
            "capacity_summary": self._capacity_summary(),
            "total_assets": round(total_assets, 4) if total_assets is not None else None,
        }


__all__ = ["TradingEngine"]
