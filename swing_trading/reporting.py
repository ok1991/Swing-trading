"""HTML report for the approved ETF rotation executor."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .config import INITIAL_CAPITAL, report_path


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


MODE_LABELS = {
    "APPROVED_ROTATION_ONLY": "仅执行已验收轮动",
    "approved_rotation_only": "仅执行已验收轮动",
}

MODEL_VERSION_LABELS = {
    "risk-control-cash-v4": "风控现金保护 v4",
}

REASON_CODE_LABELS = {
    "SOURCE_BLOCKED": "目标来源已被阻断，先不要按新目标加仓",
    "MODEL_NOT_APPROVED": "模型还没通过验收",
    "INVALID_ROTATION_CONTRACT": "轮动合约无效",
    "EXECUTION_DATE_MISMATCH": "非指定执行日",
    "ENGINE_COST_MODEL_MISMATCH": "执行成本口径不一致",
    "PLAN_AWAITING_BROKER_CONFIRMATION": "等待券商成交确认",
    "PRIOR_PLAN_AWAITING_BROKER_CONFIRMATION": "上一计划等待券商确认",
    "PLAN_STATE_PROVENANCE_MISSING": "计划状态缺少确认证据",
    "PLAN_ALREADY_APPLIED": "本轮目标已经执行过了",
    "VALUATION_BLOCKED": "估值已阻断",
    "PORTFOLIO_ALREADY_AT_TARGET": "组合已经在目标仓位附近",
    "MISSING_PRICE": "缺少可用行情，无法可靠下单",
    "LIQUIDITY_CAP_REACHED": "触及流动性上限",
    "INSUFFICIENT_CASH": "可用资金不足",
    "ROTATION_INCREASE": "轮动加仓",
    "ROTATION_DECREASE": "轮动减仓",
    "ROTATION_EXIT": "轮动清仓",
    "RISK_CONTROL_EXIT": "风控减仓",
}

SOURCE_LABELS = {
    "local_sibling": "本地兄弟目录",
    "local": "本地文件",
    "remote": "远程发布",
    "cache": "本地缓存",
    "unknown": "未知",
    "SINA_REALTIME": "新浪实时行情",
}

QUOTE_ERROR_PREFIX_LABELS = {
    "MISSING_QUOTES": "缺少报价",
    "QUOTE_DATE_MISMATCH": "报价日期不一致",
    "QUOTE_OUTSIDE_TRADING_SESSION": "报价不在交易时段",
    "RUN_DATE_NOT_EXECUTION_DATE": "运行日不是执行日",
    "CLOCK_DATE_NOT_EXECUTION_DATE": "系统日期不是执行日",
    "CURRENT_TIME_OUTSIDE_TRADING_SESSION": "当前时间不在交易时段",
    "STALE_QUOTES": "报价过期",
    "FUTURE_QUOTES": "报价时间超前",
    "QUOTE_TIME_SKEW_EXCEEDED": "报价时差过大",
    "INVALID_EXECUTION_WINDOW": "执行窗口无效",
}


class TradeHTMLReporter:
    @staticmethod
    def _e(value: Any) -> str:
        return html.escape(str(value if value is not None else ""), quote=True)

    @classmethod
    def _label(cls, value: Any, labels: Dict[str, str]) -> str:
        raw = str(value if value is not None else "").strip()
        if not raw:
            return ""
        return labels.get(raw, raw)

    @classmethod
    def _model_label(cls, value: Any) -> str:
        raw = str(value if value is not None else "").strip()
        if not raw:
            return "未提供"
        return MODEL_VERSION_LABELS.get(raw, raw)

    @classmethod
    def _reason_code_label(cls, value: Any) -> str:
        raw = str(value if value is not None else "").strip()
        if not raw:
            return ""
        return REASON_CODE_LABELS.get(raw, raw)

    @classmethod
    def _quote_error_label(cls, value: Any) -> str:
        raw = str(value if value is not None else "").strip()
        if not raw:
            return ""
        prefix, sep, detail = raw.partition(":")
        label = QUOTE_ERROR_PREFIX_LABELS.get(prefix, prefix)
        return f"{label}：{detail}" if sep and detail else label

    @classmethod
    def _chip(cls, icon: str, text: str, tone: str = "") -> str:
        tone_class = f" {tone}" if tone else ""
        return (
            f'<span class="chip{tone_class}">'
            f'<i data-lucide="{cls._e(icon)}"></i>{cls._e(text)}</span>'
        )

    @classmethod
    def _diagnostics_section(cls, orders: Dict[str, Any]) -> str:
        diagnostics = orders.get("decision_diagnostics") or {}
        reasons = diagnostics.get("reasons") or []
        reason_html = "".join(
            (
                f"<li><strong>{cls._e(cls._reason_code_label(item.get('code')))}</strong>"
                f" {cls._e(item.get('message'))}</li>"
            )
            for item in reasons
        ) or "<li>无阻断或提示</li>"
        mode_label = cls._label(diagnostics.get("mode"), MODE_LABELS) or "未提供"
        return f"""
        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="eyebrow">执行诊断</p>
              <h2>为什么这样落地</h2>
            </div>
            <span class="chip"><i data-lucide="route"></i>{cls._e(mode_label)}</span>
          </div>
          <div class="metric-grid two">
            <article><span>需要调仓</span><strong>{'是' if diagnostics.get('rebalance_required') else '否'}</strong></article>
            <article><span>计划 ID</span><strong class="mono">{cls._e(diagnostics.get('plan_id') or '—')}</strong></article>
          </div>
          <ul class="reason-list">{reason_html}</ul>
        </section>
        """

    @classmethod
    def _source_section(cls, orders: Dict[str, Any]) -> str:
        source = orders.get("rotation_source") or {}
        errors = source.get("errors") or []
        error_html = "".join(
            f"<li>{cls._e(cls._quote_error_label(error) if isinstance(error, str) and ':' in str(error) else cls._reason_code_label(error) if str(error).isupper() else error)}</li>"
            for error in errors
        ) or "<li>无</li>"
        quotes = source.get("quotes") or {}
        quote_errors = quotes.get("errors") or []
        quote_error_html = "".join(
            f"<li>{cls._e(cls._quote_error_label(error))}</li>" for error in quote_errors
        ) or "<li>无</li>"
        quote_dates = quotes.get("quote_dates") or {}
        quote_times = quotes.get("quote_times") or {}
        quote_rows = "".join(
            "<tr>"
            f"<td>{cls._e(code)}</td>"
            f"<td>{cls._e(quote_dates.get(code, ''))}</td>"
            f"<td>{cls._e(quote_times.get(code, ''))}</td>"
            "</tr>"
            for code in sorted(set(quote_dates) | set(quote_times))
        ) or '<tr><td colspan="3">无实时报价</td></tr>'
        mode = "允许调仓" if source.get("allow_rebalance") else "保持持仓"
        source_label = cls._label(source.get("source", "unknown"), SOURCE_LABELS)
        model_label = cls._model_label(source.get("model_version", ""))
        quote_status = "可执行" if quotes.get("tradeable") else "已阻断"
        quote_tone = "ok" if quotes.get("tradeable") else "bad"
        return f"""
        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="eyebrow">目标来源</p>
              <h2>轮动目标从哪来</h2>
            </div>
            <span class="chip"><i data-lucide="folder-input"></i>{cls._e(source_label)}</span>
          </div>
          <div class="split-grid">
            <div class="stack">
              <p>地址：<code>{cls._e(source.get('source_url', ''))}</code></p>
              <p>数据日期：{cls._e(source.get('data_date', ''))}</p>
              <p>指定执行日：{cls._e(source.get('execution_date', ''))}</p>
              <p>模型：<strong>{cls._e(model_label)}</strong></p>
              <p>执行状态：<strong>{mode}</strong></p>
            </div>
            <div class="stack">
              <h3>获取诊断</h3>
              <ul class="reason-list">{error_html}</ul>
            </div>
          </div>
          <div class="subpanel">
            <div class="panel-head compact">
              <div>
                <p class="eyebrow">实时行情审计</p>
                <h3>新浪实时行情审计</h3>
              </div>
              <span class="chip {quote_tone}"><i data-lucide="radio-tower"></i>{quote_status}</span>
            </div>
            <p>验证时间：<code>{cls._e(quotes.get('validated_at', ''))}</code></p>
            <p>最大报价年龄：{int(_f(quotes.get('max_quote_age_seconds')))}秒 · 最大横截面时差：{int(_f(quotes.get('max_quote_time_skew_seconds')))}秒</p>
            <ul class="reason-list">{quote_error_html}</ul>
            <div class="table-wrap">
              <table>
                <thead><tr><th>代码</th><th>报价日期</th><th>报价时间</th></tr></thead>
                <tbody>{quote_rows}</tbody>
              </table>
            </div>
          </div>
        </section>
        """

    @classmethod
    def _orders(cls, orders: Dict[str, Any]) -> str:
        rows = []
        for side, items in (("买入", orders.get("buy_orders", [])), ("卖出", orders.get("sell_orders", []))):
            for item in items:
                if item.get("liquidity_capped"):
                    capacity_text = (
                        f"截断 {int(item.get('requested_shares', 0))}→{int(item.get('shares', 0))}"
                    )
                elif item.get("capacity_exceeded"):
                    capacity_text = "减仓超10% ADV"
                else:
                    capacity_text = "正常"
                reason = item.get("reason")
                reason_text = cls._reason_code_label(reason) if reason else ""
                rows.append(
                    "<tr>"
                    f"<td><span class='pill'>{side}</span></td>"
                    f"<td><div class='name-cell'><strong>{cls._e(item.get('name'))}</strong><small>{cls._e(item.get('code'))}</small></div></td>"
                    f"<td>{int(item.get('shares', 0))}</td>"
                    f"<td>{_f(item.get('price')):.3f}</td>"
                    f"<td>{_f(item.get('average_daily_amount')):,.0f}</td>"
                    f"<td>{_f(item.get('participation_rate')):.4%}</td>"
                    f"<td>{_f(item.get('impact_bps')):.3f}</td>"
                    f"<td>{capacity_text}</td>"
                    f"<td>{_f(item.get('capacity_headroom_amount')):,.0f}</td>"
                    f"<td>{_f(item.get('total_cost')):.2f}</td>"
                    f"<td>{cls._e(reason_text)}</td>"
                    "</tr>"
                )
        body = "".join(rows) or '<tr><td colspan="11" class="empty">本轮无交易</td></tr>'
        return f"""
        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="eyebrow">订单</p>
              <h2>本轮交易动作</h2>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>方向</th><th>名称</th><th>份额</th><th>价格</th>
                <th>20日ADV</th><th>参与率</th><th>冲击bps</th><th>容量</th><th>剩余容量金额</th>
                <th>全部执行成本</th><th>依据</th>
              </tr></thead>
              <tbody>{body}</tbody>
            </table>
          </div>
        </section>
        """

    @classmethod
    def _capacity_section(cls, orders: Dict[str, Any]) -> str:
        summary = orders.get("capacity_summary") or {}
        return f"""
        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="eyebrow">容量</p>
              <h2>实时容量审计</h2>
            </div>
          </div>
          <div class="metric-grid two">
            <article>
              <span>已发布新增风险容量</span>
              <strong>{_f(summary.get('published_new_risk_capacity_amount')):,.0f}</strong>
              <small>本次请求 / 执行 {_f(summary.get('requested_new_risk_amount')):,.0f} / {_f(summary.get('executed_new_risk_amount')):,.0f}</small>
            </article>
            <article>
              <span>剩余容量余量</span>
              <strong>{_f(summary.get('remaining_capacity_headroom_amount')):,.0f}</strong>
              <small>未成交 {_f(summary.get('unfilled_new_risk_amount')):,.0f} · 利用率 {_f(summary.get('capacity_utilization_rate')):.2%}</small>
            </article>
          </div>
        </section>
        """

    @classmethod
    def _holdings(cls, state: Dict[str, Any], orders: Dict[str, Any], prices: Dict[str, float]) -> str:
        targets = orders.get("target_weights") or {}
        actual = orders.get("actual_weights") or {}
        rows = []
        for holding in state.get("holdings", []):
            code = str(holding.get("code", ""))
            price = _f(prices.get(code))
            buy = _f(holding.get("buy_price"))
            price_text = f"{price:.3f}" if price > 0 else "缺失"
            pnl_text = f"{(price / buy - 1.0) * 100.0:+.2f}%" if price > 0 and buy > 0 else "不可计算"
            rows.append(
                "<tr>"
                f"<td><div class='name-cell'><strong>{cls._e(holding.get('name'))}</strong><small>{cls._e(code)}</small></div></td>"
                f"<td>{int(holding.get('shares', 0))}</td>"
                f"<td>{buy:.3f}</td>"
                f"<td>{price_text}</td>"
                f"<td>{_f(targets.get(code)):.2%}</td>"
                f"<td>{_f(actual.get(code)):.2%}</td>"
                f"<td>{pnl_text}</td>"
                "</tr>"
            )
        body = "".join(rows) or '<tr><td colspan="7" class="empty">当前空仓</td></tr>'
        return f"""
        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="eyebrow">组合</p>
              <h2>持仓与目标</h2>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>名称</th><th>份额</th><th>成本</th><th>现价</th>
                <th>目标权重</th><th>实际权重</th><th>收益</th>
              </tr></thead>
              <tbody>{body}</tbody>
            </table>
          </div>
        </section>
        """

    @classmethod
    def _run_meta_section(cls, orders: Dict[str, Any]) -> str:
        model_label = cls._model_label(orders.get("model_version"))
        approved = orders.get("approved")
        if approved is True:
            approved_text = "已获批"
            approved_tone = "ok"
        elif approved is False:
            approved_text = "未获批"
            approved_tone = "bad"
        else:
            approved_text = "未提供"
            approved_tone = "warn"
        risk_only = "是" if orders.get("risk_control_only") else "否"
        return f"""
        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="eyebrow">运行摘要</p>
              <h2>执行端同步了什么</h2>
            </div>
            <div class="chip-row">
              {cls._chip("badge-check", approved_text, approved_tone)}
              {cls._chip("wallet", f"仅风控现金：{risk_only}", "warn" if orders.get("risk_control_only") else "")}
            </div>
          </div>
          <div class="metric-grid two">
            <article>
              <span>模型</span>
              <strong>{cls._e(model_label)}</strong>
            </article>
            <article>
              <span>日期</span>
              <strong>{cls._e(orders.get('data_date') or '—')}</strong>
              <small>指定执行日 {cls._e(orders.get('execution_date') or '—')}</small>
            </article>
          </div>
        </section>
        """

    @classmethod
    def generate(
        cls,
        state: Dict[str, Any],
        orders: Dict[str, Any],
        prices: Dict[str, float],
        output_path: Optional[str] = None,
    ) -> None:
        from .investor_report import (
            build_investor_context,
            fold_section,
            pct_text,
            render_performance_section,
        )

        cash = _f(state.get("cash"))
        assets_value = orders.get("total_assets")
        assets = _f(assets_value) if assets_value is not None else None
        initial = _f(state.get("initial_capital"), INITIAL_CAPITAL)
        assets_text = f"{assets:,.2f}" if assets is not None else "不可估值"
        investor = build_investor_context(state, fallback_to_file=False)
        if investor.get("available") and investor.get("strategy_return") is not None:
            pnl_text = pct_text(investor.get("strategy_return"))
        else:
            pnl_text = (
                f"{(assets / initial - 1.0) * 100.0:+.2f}%"
                if assets is not None and initial > 0
                else "—"
            )
        risk_only = bool(orders.get("risk_control_only"))
        approved = orders.get("approved")
        if risk_only or approved is False:
            trust = "observe"
            trust_label = "仅观察"
            headline = "保持现金 / 审慎执行"
            summary = "执行端已读取生产目标。当前是保护性状态：优先保持现金/已有仓位。下方优先展示投资者收益，运维细节默认折叠。"
        elif assets is None:
            trust = "distrust"
            trust_label = "不可信"
            headline = "估值受阻"
            summary = "组合暂时不可估值，先核对行情与持仓状态。"
        else:
            trust = "follow"
            trust_label = "可跟随"
            headline = "执行链路就绪"
            summary = "生产目标与执行摘要已对齐。下方优先展示投资者收益，运维细节默认折叠。"

        production_href = "https://etf.imlam.com/"
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        performance_section = render_performance_section(investor)
        holdings_section = cls._holdings(state, orders, prices)
        ops_sections = "".join(
            [
                fold_section("运行摘要", "执行端同步了什么", cls._run_meta_section(orders)),
                fold_section("订单", "本轮交易动作", cls._orders(orders)),
                fold_section("容量", "实时容量审计", cls._capacity_section(orders)),
                fold_section("目标来源", "轮动目标从哪来", cls._source_section(orders)),
                fold_section("执行诊断", "为什么这样落地", cls._diagnostics_section(orders)),
            ]
        )
        document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>V4 执行端 · 投资者收益</title>
  <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js" defer></script>
  <style>
  :root {{
    color-scheme: dark;
    --bg:#05070d; --panel:rgba(16,24,40,.92); --line:rgba(140,170,210,.16);
    --ink:#edf4ff; --muted:#8fa6c2; --accent:#67f0d4; --accent-2:#7aa7ff;
    --up:#ff6b81; --down:#3dde97; --warn:#f0c36a; --bad:#ff7b72; --ok:#67f0d4;
    --radius:8px; --shadow:0 30px 80px rgba(0,0,0,.35);
    --font:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  }}
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;color:var(--ink);font-family:var(--font);
    background:radial-gradient(1200px 700px at 12% -10%,rgba(88,140,255,.18),transparent 55%),
               radial-gradient(900px 600px at 90% 0%,rgba(64,220,190,.12),transparent 50%),
               linear-gradient(180deg,#07101c 0%,var(--bg) 45%,#04060b 100%)}}
  body::before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.35;
    background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);
    background-size:72px 72px;mask-image:radial-gradient(circle at center,black 30%,transparent 85%)}}
  .shell{{width:min(1440px,calc(100% - 32px));margin:0 auto;padding:24px 0 72px;position:relative;z-index:1}}
  .bridge,.panel,.hero,.metric-grid article,.subpanel{{border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);box-shadow:var(--shadow)}}
  .bridge{{display:grid;grid-template-columns:1.2fr auto .8fr;gap:16px;align-items:center;padding:14px 18px;margin-bottom:18px;backdrop-filter:blur(18px)}}
  .bridge-brand{{display:flex;gap:12px;align-items:center}}
  .brand-mark,.chip i,.bridge-tab i,.bridge-meta i{{width:16px;height:16px;stroke-width:1.8}}
  .brand-mark{{width:40px;height:40px;display:grid;place-items:center;border-radius:8px;color:var(--accent);border:1px solid rgba(103,240,212,.25);background:linear-gradient(135deg,rgba(103,240,212,.16),rgba(122,167,255,.12))}}
  .eyebrow{{margin:0;color:var(--accent);letter-spacing:.16em;text-transform:uppercase;font-size:11px;font-weight:700}}
  .bridge-brand strong{{display:block;margin-top:2px;font-size:15px}}
  .bridge-tabs{{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}}
  .bridge-tab{{display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border-radius:8px;color:var(--muted);text-decoration:none;border:1px solid transparent}}
  .bridge-tab.is-active,.bridge-tab:hover{{color:var(--ink);border-color:rgba(103,240,212,.28);background:linear-gradient(180deg,rgba(103,240,212,.12),rgba(122,167,255,.08))}}
  .bridge-meta{{display:flex;justify-content:flex-end;align-items:center;gap:8px;color:var(--muted);font-size:13px}}
  .hero{{position:relative;overflow:hidden;display:grid;grid-template-columns:1.3fr .7fr;gap:18px;padding:28px;margin-bottom:18px;
    background:linear-gradient(135deg,rgba(20,40,68,.92),rgba(10,16,28,.94)),radial-gradient(circle at 80% 20%,rgba(103,240,212,.12),transparent 30%)}}
  .decision-follow{{--hero-accent:var(--ok)}} .decision-observe{{--hero-accent:var(--warn)}} .decision-distrust{{--hero-accent:var(--bad)}}
  .hero-kicker{{display:inline-flex;padding:6px 10px;border-radius:999px;border:1px solid color-mix(in srgb,var(--hero-accent) 45%,transparent);color:var(--hero-accent);background:color-mix(in srgb,var(--hero-accent) 12%,transparent);font-size:12px;letter-spacing:.12em;text-transform:uppercase}}
  .hero h1{{margin:14px 0 0;font-size:clamp(36px,6vw,68px);line-height:.95;letter-spacing:-.03em}}
  .hero-summary{{max-width:42rem;margin:16px 0 0;color:#d5e4f7;line-height:1.7}}
  .chip-row,.hero-reasons{{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}}
  .chip{{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--muted);font-size:12px}}
  .chip.ok{{color:var(--ok);border-color:rgba(103,240,212,.28)}} .chip.warn{{color:var(--warn);border-color:rgba(240,195,106,.28)}} .chip.bad{{color:var(--bad);border-color:rgba(255,123,114,.28)}}
  .orb{{min-height:220px;display:grid;place-items:center;position:relative}}
  .orb-core{{width:160px;height:160px;border-radius:50%;display:grid;place-content:center;text-align:center;border:1px solid rgba(103,240,212,.25);background:radial-gradient(circle at 30% 30%,rgba(103,240,212,.18),rgba(8,14,24,.95))}}
  .orb-core span,.orb-core small,.metric-grid small,.name-cell small{{color:var(--muted);font-size:12px}}
  .orb-core strong{{display:block;margin:6px 0;font-size:34px;color:var(--hero-accent);letter-spacing:-.04em}}
  .metric-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}}
  .metric-grid.two{{grid-template-columns:1fr 1fr}}
  .metric-grid article{{padding:16px;background:rgba(7,12,22,.55)}}
  .metric-grid span{{display:block;color:var(--muted);font-size:12px}}
  .metric-grid strong{{display:block;margin-top:10px;font-size:24px;letter-spacing:-.02em}}
  .panel{{padding:22px;margin-bottom:18px}}
  .panel-head{{display:flex;justify-content:space-between;gap:16px;align-items:start}}
  .panel-head.compact{{align-items:center}}
  .panel-head h2,.panel-head h3{{margin:6px 0 0}}
  .panel-head h2{{font-size:clamp(24px,3vw,34px);letter-spacing:-.03em}}
  .split-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}}
  .stack p,.stack h3,.subpanel p{{margin:0 0 10px;color:#d7e4f6;line-height:1.6}}
  .subpanel{{margin-top:18px;padding:16px}}
  .reason-list{{margin:14px 0 0;padding-left:18px;color:var(--muted);line-height:1.8}}
  .table-wrap{{overflow:auto;margin-top:16px;border:1px solid var(--line);border-radius:var(--radius)}}
  table{{width:100%;border-collapse:collapse;min-width:760px}}
  th,td{{padding:13px 12px;border-bottom:1px solid rgba(140,170,210,.1);text-align:left;vertical-align:top}}
  th{{color:var(--muted);font-size:12px;letter-spacing:.04em;text-transform:uppercase;background:rgba(8,13,22,.96)}}
  .name-cell strong{{display:block}}
  .pill{{display:inline-flex;padding:6px 10px;border-radius:999px;border:1px solid var(--line);font-size:12px}}
  .empty{{color:var(--muted);line-height:1.7}}
  code,.mono{{color:#8fd8ff;font-family:Consolas,monospace}}
  @media(max-width:1100px){{.bridge,.hero,.metric-grid,.split-grid{{grid-template-columns:1fr 1fr}}.bridge{{grid-template-columns:1fr}}.bridge-meta{{justify-content:flex-start}}}}
  .performance-grid{{grid-template-columns:repeat(3,1fr)}}
  .fold{{border:0}}
  .fold > summary{{list-style:none;cursor:pointer}}
  .fold > summary::-webkit-details-marker{{display:none}}
  .fold-hint{{color:var(--muted)}}
  .fold[open] .fold-hint{{color:var(--accent);border-color:rgba(103,240,212,0.28)}}
  .chart-wrap{{margin-top:16px;padding:12px;border:1px solid var(--line);border-radius:var(--radius);background:rgba(7,12,22,.45)}}
  .chart-wrap svg{{width:100%;height:auto;display:block}}
  .chart-grid{{stroke:rgba(140,170,210,.18);stroke-width:1}}
  .chart-zero{{stroke:rgba(140,170,210,.28);stroke-dasharray:4 4;stroke-width:1}}
  .chart-line{{stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}}
  .chart-line.strategy{{stroke:var(--accent)}}
  .chart-line.benchmark{{stroke:var(--accent-2);stroke-dasharray:6 4}}
  .chart-legend{{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;color:var(--muted);font-size:12px}}
  .legend.strategy{{color:var(--accent)}}
  .legend.benchmark{{color:var(--accent-2)}}
  .chart-empty{{margin-top:16px;padding:18px;color:#d7e4f6;border:1px solid var(--line);border-radius:var(--radius);background:rgba(7,12,22,.45)}}
  .chart-empty strong{{display:block;margin-bottom:8px;color:var(--warn)}}
  .chart-empty p{{margin:0;color:var(--muted);line-height:1.7}}
  @media(max-width:1100px){{.performance-grid{{grid-template-columns:1fr 1fr}}}}
  @media(max-width:760px){{.performance-grid{{grid-template-columns:1fr}}}}
  @media(max-width:760px){{.shell{{width:min(100% - 16px,1440px);padding:12px 0 48px}}.bridge{{position:sticky;top:8px;z-index:20;grid-template-columns:1fr;gap:10px}}.bridge-tabs{{display:grid;grid-template-columns:1fr 1fr}}.bridge-tab{{justify-content:center}}.hero,.metric-grid,.split-grid{{grid-template-columns:1fr}}.hero{{padding:16px}}.orb{{display:none}}.panel{{padding:16px}}.panel-head{{display:block}}}} /* priority mobile scan */
  </style>
</head>
<body data-trust="{trust}">
  <div class="shell">
    <nav class="bridge" aria-label="双端导航">
      <div class="bridge-brand">
        <span class="brand-mark" aria-hidden="true"><i data-lucide="zap"></i></span>
        <div>
          <p class="eyebrow">SCHEMA V4 · DUAL BOARD</p>
          <strong>ETF 生产 / 执行联动看板</strong>
        </div>
      </div>
      <div class="bridge-tabs">
        <a class="bridge-tab" href="{cls._e(production_href)}">
          <i data-lucide="cpu"></i><span>生产端 · 权威结论</span>
        </a>
        <a class="bridge-tab is-active" href="https://swing.imlam.com/">
          <i data-lucide="zap"></i><span>执行端 · 投资者收益</span>
        </a>
      </div>
      <div class="bridge-meta">
        <i data-lucide="clock-3"></i><span>{generated_at}</span>
      </div>
    </nav>

    <header class="hero decision-{trust}">
      <div>
        <p class="eyebrow">执行落地结论</p>
        <div class="hero-kicker">{trust_label}</div>
        <h1>{cls._e(headline)}</h1>
        <p class="hero-summary">{cls._e(summary)}</p>
        <div class="hero-reasons">
          {cls._chip("box", cls._model_label(orders.get("model_version")))}
          {cls._chip("calendar-range", f"数据 {orders.get('data_date') or '—'} · 执行 {orders.get('execution_date') or '—'}")}
          {cls._chip("badge-check", "已获批" if approved is True else "未获批" if approved is False else "验收未提供", "ok" if approved is True else "bad" if approved is False else "warn")}
        </div>
      </div>
      <div class="orb">
        <div class="orb-core">
          <span>总资产</span>
          <strong>{assets_text}</strong>
          <small>现金 {cash:,.2f} · 累计 {pnl_text}</small>
        </div>
      </div>
      <div class="metric-grid" style="grid-column:1/-1">
        <article><span>总资产</span><strong>{assets_text}</strong></article>
        <article><span>现金</span><strong>{cash:,.2f}</strong></article>
        <article><span>累计收益</span><strong>{pnl_text}</strong></article>
        <article><span>说明</span><strong>见下方收益总览</strong></article>
      </div>
    </header>

    {performance_section}
    {holdings_section}
    {ops_sections}
  </div>
  <script>
    document.addEventListener("DOMContentLoaded", () => {{
      if (window.lucide && typeof window.lucide.createIcons === "function") {{
        window.lucide.createIcons();
      }}
      if (false) document.querySelectorAll(".bridge, .hero, .panel").forEach((item, index) => {{
        item.style.opacity = "0";
        item.style.transform = "translateY(18px)";
        item.style.transition = `opacity 520ms ease ${{index * 60}}ms, transform 520ms ease ${{index * 60}}ms`;
        requestAnimationFrame(() => {{
          item.style.opacity = "1";
          item.style.transform = "translateY(0)";
        }});
      }});
    }});
  </script>
</body>
</html>"""
        path = Path(output_path) if output_path else report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")


__all__ = ["TradeHTMLReporter"]
