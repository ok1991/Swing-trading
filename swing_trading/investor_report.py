# -*- coding: utf-8 -*-
"""Investor-facing performance panel helpers for the Swing dashboard."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .config import live_performance_path
from .performance import build_investor_performance_view


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct_text(value: Any, *, digits: int = 2, empty: str = "—") -> str:
    if value is None:
        return empty
    try:
        number = float(value)
    except (TypeError, ValueError):
        return empty
    return f"{number * 100.0:+.{digits}f}%"


def ratio_text(value: Any, *, digits: int = 2, empty: str = "—") -> str:
    if value is None:
        return empty
    try:
        number = float(value)
    except (TypeError, ValueError):
        return empty
    return f"{number:.{digits}f}"


def load_live_performance(state: Mapping[str, Any], *, fallback_to_file: bool = False) -> Dict[str, Any]:
    live = dict(state.get("live_performance") or {})
    if live.get("history") or int(live.get("observation_count") or 0) > 0:
        return live
    if not fallback_to_file:
        return live
    path = live_performance_path()
    if not path.is_file():
        return live
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return live
    return payload if isinstance(payload, dict) else live


def build_investor_context(state: Mapping[str, Any], *, fallback_to_file: bool = False) -> Dict[str, Any]:
    live = load_live_performance(state, fallback_to_file=fallback_to_file)
    return build_investor_performance_view(
        live,
        trade_history=list(state.get("trade_history") or []),
    )


def _polyline(points: List[Dict[str, Any]], key: str, *, width: float, height: float, pad: float, min_y: float, max_y: float) -> str:
    usable = [(index, item) for index, item in enumerate(points) if item.get(key) is not None]
    if len(usable) < 2:
        return ""
    span = max(max_y - min_y, 1e-9)
    count = max(len(points) - 1, 1)
    coords = []
    for index, item in usable:
        x = pad + (width - 2 * pad) * (index / count)
        y = height - pad - ((float(item[key]) - min_y) / span) * (height - 2 * pad)
        coords.append(f"{x:.2f},{y:.2f}")
    return " ".join(coords)


def render_performance_section(investor: Mapping[str, Any]) -> str:
    available = bool(investor.get("available"))
    chart = list(investor.get("chart") or [])
    trade = dict(investor.get("trade_stats") or {})
    chips = [f'<span class="chip"><i data-lucide="line-chart"></i>{_e(investor.get("benchmark_label") or "沪深300ETF(510300)")}</span>']
    if investor.get("data_date"):
        chips.append(f'<span class="chip"><i data-lucide="calendar-range"></i>{_e("绩效截至 " + str(investor.get("data_date")))}</span>')
    if investor.get("estimate_only"):
        chips.append('<span class="chip warn"><i data-lucide="shield-alert"></i>估值估计中</span>')
    if investor.get("sparse_series"):
        chips.append('<span class="chip warn"><i data-lucide="info"></i>样本过少</span>')

    strategy_return = pct_text(investor.get("strategy_return"))
    max_return = pct_text(investor.get("max_return"))
    max_drawdown = pct_text(investor.get("strategy_max_drawdown"))
    excess_return = pct_text(investor.get("excess_return"))
    if trade.get("available"):
        profit_factor = ratio_text(trade.get("profit_factor"))
        win_rate = pct_text(trade.get("win_rate"))
        trade_note = (
            f"已实现回合 {int(trade.get('closed_rounds') or 0)}"
            f" · 平均盈利 {ratio_text(trade.get('average_win'), digits=2)}"
            f" / 平均亏损 {ratio_text(trade.get('average_loss'), digits=2)}"
        )
    else:
        profit_factor = "—"
        win_rate = "—"
        trade_note = str(trade.get("empty_reason") or "暂无足够交易样本")

    values: List[float] = []
    for item in chart:
        for key in ("strategy_return_pct", "benchmark_return_pct"):
            if item.get(key) is not None:
                values.append(float(item[key]))
    width, height, pad = 920.0, 280.0, 28.0
    if available and len(chart) >= 2 and values:
        min_y = min(values)
        max_y = max(values)
        if abs(max_y - min_y) < 1e-6:
            min_y -= 1.0
            max_y += 1.0
        strategy_line = _polyline(chart, "strategy_return_pct", width=width, height=height, pad=pad, min_y=min_y, max_y=max_y)
        benchmark_line = _polyline(chart, "benchmark_return_pct", width=width, height=height, pad=pad, min_y=min_y, max_y=max_y)
        zero_y = height - pad - ((0.0 - min_y) / max(max_y - min_y, 1e-9)) * (height - 2 * pad)
        first_date = _e(chart[0].get("date") or "")
        last_date = _e(chart[-1].get("date") or "")
        chart_html = (
            f'<div class="chart-wrap"><svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="策略与沪深300累计收益对比">'
            f'<line class="chart-grid" x1="{pad}" y1="{pad}" x2="{pad}" y2="{height-pad}" />'
            f'<line class="chart-grid" x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" />'
            f'<line class="chart-zero" x1="{pad}" y1="{zero_y:.2f}" x2="{width-pad}" y2="{zero_y:.2f}" />'
            f'<polyline class="chart-line benchmark" fill="none" points="{benchmark_line}" />'
            f'<polyline class="chart-line strategy" fill="none" points="{strategy_line}" />'
            f'</svg><div class="chart-legend">'
            f'<span class="legend strategy">策略累计收益%</span>'
            f'<span class="legend benchmark">沪深300ETF累计收益%</span>'
            f'<span class="legend muted">{first_date} → {last_date}</span></div></div>'
        )
    elif available and len(chart) == 1:
        point = chart[0]
        s_pct = point.get("strategy_return_pct")
        b_pct = point.get("benchmark_return_pct")
        chart_html = (
            '<div class="chart-empty"><strong>样本过少，暂不绘完整曲线</strong>'
            f"<p>当前仅有 {_e(point.get('date') or '—')} 一个观测点：策略 "
            f"{_e(pct_text(None if s_pct is None else s_pct/100.0))}，沪深300ETF "
            f"{_e(pct_text(None if b_pct is None else b_pct/100.0))}。</p></div>"
        )
    else:
        chart_html = (
            '<div class="chart-empty"><strong>等待实盘绩效记录</strong>'
            '<p>还没有可展示的累计收益曲线。系统不会用回测或示意数据填补。</p></div>'
        )

    return (
        '<section class="panel performance-panel"><div class="panel-head"><div>'
        '<p class="eyebrow">收益总览</p><h2>投资者绩效</h2></div>'
        f'<div class="chip-row">{"".join(chips)}</div></div>'
        '<div class="metric-grid performance-grid">'
        f'<article><span>累计收益</span><strong>{_e(strategy_return)}</strong><small>相对账户起点</small></article>'
        f'<article><span>最大收益</span><strong>{_e(max_return)}</strong><small>历史峰值累计收益</small></article>'
        f'<article><span>最大回撤</span><strong>{_e(max_drawdown)}</strong><small>策略净值回撤</small></article>'
        f'<article><span>超额收益</span><strong>{_e(excess_return)}</strong><small>相对沪深300ETF</small></article>'
        f'<article><span>盈亏比</span><strong>{_e(profit_factor)}</strong><small>总盈利 / 总亏损</small></article>'
        f'<article><span>胜率</span><strong>{_e(win_rate)}</strong><small>{_e(trade_note)}</small></article>'
        f'</div>{chart_html}</section>'
    )


def fold_section(eyebrow: str, title: str, body: str, *, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    content = body.strip()
    if content.startswith("<section") and "</section>" in content:
        inner_start = content.find(">") + 1
        content = content[inner_start : content.rfind("</section>")].strip()
        head_marker = '<div class="panel-head"'
        head_start = content.find(head_marker)
        if head_start != -1:
            title_div_start = content.find('<div>', head_start)
            if title_div_start != -1:
                title_div_end = content.find('</div>', title_div_start)
                if title_div_end != -1:
                    title_div_end += len('</div>')
                    content = (
                        content[:title_div_start]
                        + content[title_div_end:]
                    ).strip()
    return (
        f'<section class="panel fold-panel"><details class="fold"{open_attr}>'
        f'<summary class="fold-summary"><div class="panel-head compact"><div>'
        f'<p class="eyebrow">{_e(eyebrow)} · 默认折叠</p><h2>{_e(title)}</h2></div>'
        f'<span class="chip fold-hint"><i data-lucide="chevrons-up-down"></i>展开 / 收起</span>'
        f'</div></summary>{content}</details></section>'
    )


INVESTOR_CSS = """
  .performance-grid{grid-template-columns:repeat(3,1fr)}
  .fold{border:0}
  .fold > summary{list-style:none;cursor:pointer}
  .fold > summary::-webkit-details-marker{display:none}
  .fold-hint{color:var(--muted)}
  .fold[open] .fold-hint{color:var(--accent);border-color:rgba(103,240,212,0.28)}
  .chart-wrap{margin-top:16px;padding:12px;border:1px solid var(--line);border-radius:var(--radius);background:rgba(7,12,22,.45)}
  .chart-wrap svg{width:100%;height:auto;display:block}
  .chart-grid{stroke:rgba(140,170,210,.18);stroke-width:1}
  .chart-zero{stroke:rgba(140,170,210,.28);stroke-dasharray:4 4;stroke-width:1}
  .chart-line{stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
  .chart-line.strategy{stroke:var(--accent)}
  .chart-line.benchmark{stroke:var(--accent-2);stroke-dasharray:6 4}
  .chart-legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;color:var(--muted);font-size:12px}
  .legend.strategy{color:var(--accent)}
  .legend.benchmark{color:var(--accent-2)}
  .chart-empty{margin-top:16px;padding:18px;color:#d7e4f6;border:1px solid var(--line);border-radius:var(--radius);background:rgba(7,12,22,.45)}
  .chart-empty strong{display:block;margin-bottom:8px;color:var(--warn)}
  .chart-empty p{margin:0;color:var(--muted);line-height:1.7}
  @media(max-width:1100px){.performance-grid{grid-template-columns:1fr 1fr}}
  @media(max-width:760px){.performance-grid{grid-template-columns:1fr}}
"""
