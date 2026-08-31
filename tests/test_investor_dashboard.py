# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_trading.investor_report import build_investor_context, render_performance_section
from swing_trading.performance import record_live_performance, summarize_realized_trade_stats
from swing_trading.reporting import TradeHTMLReporter


class InvestorDashboardTests(unittest.TestCase):
    def test_empty_history_is_honest_empty_state(self):
        view = build_investor_context(
            {"cash": 10000.0, "initial_capital": 10000.0, "holdings": [], "trade_history": []}
        )
        self.assertFalse(view["available"])
        self.assertEqual("等待实盘绩效记录", view["empty_reason"])
        html = render_performance_section(view)
        self.assertIn("等待实盘绩效记录", html)
        self.assertNotIn("chart-line strategy", html)

    def test_history_builds_kpis_and_chart(self):
        state = {"cash": 10450.0, "initial_capital": 10000.0, "holdings": [], "trade_history": []}
        for index, (assets, bench) in enumerate(
            [(10000, 4.0), (10100, 4.01), (10300, 4.05), (10200, 4.08), (10450, 4.10)],
            start=1,
        ):
            record_live_performance(
                state,
                total_assets=assets,
                benchmark_price=bench,
                date=f"2026-01-{index:02d}",
                model_version="demo",
            )
        view = build_investor_context(state)
        self.assertTrue(view["available"])
        self.assertAlmostEqual(view["strategy_return"], 0.045)
        self.assertGreaterEqual(view["max_return"], view["strategy_return"])
        self.assertEqual(5, len(view["chart"]))
        html = render_performance_section(view)
        self.assertIn("chart-line strategy", html)
        self.assertIn("+4.50%", html)

    def test_trade_stats_require_enough_closed_rounds(self):
        sparse = summarize_realized_trade_stats(
            [
                {"side": "BUY", "code": "510300", "shares": 100, "price": 4.0, "total_cost": 1.0, "date": "2026-01-01"},
                {"side": "SELL", "code": "510300", "shares": 100, "price": 4.2, "total_cost": 1.0, "date": "2026-01-02"},
            ]
        )
        self.assertFalse(sparse["available"])
        self.assertEqual("暂无足够交易样本", sparse["empty_reason"])

        enough = summarize_realized_trade_stats(
            [
                {"side": "BUY", "code": "510300", "shares": 100, "price": 4.0, "total_cost": 1.0, "date": "2026-01-01"},
                {"side": "SELL", "code": "510300", "shares": 100, "price": 4.4, "total_cost": 1.0, "date": "2026-01-10"},
                {"side": "BUY", "code": "512800", "shares": 100, "price": 1.0, "total_cost": 0.5, "date": "2026-01-02"},
                {"side": "SELL", "code": "512800", "shares": 100, "price": 0.9, "total_cost": 0.5, "date": "2026-01-11"},
                {"side": "BUY", "code": "159930", "shares": 100, "price": 1.0, "total_cost": 0.2, "date": "2026-01-03"},
                {"side": "SELL", "code": "159930", "shares": 100, "price": 1.2, "total_cost": 0.2, "date": "2026-01-12"},
            ]
        )
        self.assertTrue(enough["available"])
        self.assertEqual(3, enough["closed_rounds"])
        self.assertAlmostEqual(enough["win_rate"], 2 / 3, places=5)

    def test_report_folds_ops_and_prioritizes_performance(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.html"
            state = {"cash": 10000.0, "initial_capital": 10000.0, "holdings": [], "trade_history": []}
            orders = {
                "total_assets": 10000.0,
                "target_weights": {},
                "actual_weights": {},
                "decision_diagnostics": {"mode": "APPROVED_ROTATION_ONLY", "reasons": []},
                "model_version": "risk-control-cash-v4",
                "data_date": "2026-07-22",
                "execution_date": "2026-07-23",
                "risk_control_only": True,
                "approved": True,
                "rotation_source": {
                    "allow_rebalance": False,
                    "source": "local_sibling",
                    "model_version": "risk-control-cash-v4",
                    "errors": [],
                    "quotes": {"tradeable": False, "errors": []},
                },
            }
            TradeHTMLReporter.generate(state, orders, {}, output_path=str(output))
            html = output.read_text(encoding="utf-8")
            self.assertIn("收益总览", html)
            self.assertIn("等待实盘绩效记录", html)
            self.assertIn("默认折叠", html)
            self.assertGreaterEqual(html.count('<details class="fold"'), 5)
            self.assertLess(html.find("投资者绩效"), html.find("本轮交易动作"))


if __name__ == "__main__":
    unittest.main()
