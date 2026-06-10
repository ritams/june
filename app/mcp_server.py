"""DJG Dashboard MCP server — exposes the 10 Hermes tool endpoints as MCP tools.

Mounted onto the main FastAPI app at /mcp so Hermes Agent can connect over HTTP
with one URL. Each MCP tool is a thin wrapper that calls the same service
functions the /api/hermes/* endpoints use — no HTTP round-trip in-process.

Hermes config (in ~/.hermes/config.yaml on dan-mac):
    mcp_servers:
      djg_dashboard:
        url: "http://127.0.0.1:8000/mcp"
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP


def build_mcp_server(
    *,
    dashboard_service,
    backtest_service,
    correlation_service,
    phase_service,
    state_store,
    settings,
    telegram_client,
    hermes_state_module,
    risk_budget_module,
    whatif_module,
    framework_portfolio_module,
    cio_message_module,
    auto_fill_scenario,
    current_state_fn,
) -> FastMCP:
    """Build the MCP server. Service dependencies are injected so the FastAPI
    app's already-initialized singletons are reused (Hermes runs in-process)."""

    mcp = FastMCP("djg-dashboard")

    @mcp.tool()
    def get_current_dashboard_state() -> dict[str, Any]:
        """Get the full CIO View payload — current stance, risk budget score,
        deployment/cash split, macro season, liquidity state, cycle state,
        confidence, and the plain-English summary. Use this when the user asks
        about overall positioning or anything multi-faceted."""
        return current_state_fn().to_dict()

    @mcp.tool()
    def get_current_risk_budget() -> dict[str, Any]:
        """Get the current Risk Budget score (0-100), the stance band it maps
        to, deployment/cash split, and the weighted factor components. Use this
        when the user asks 'how risk-on are we?' or 'what's the score?'"""
        scenario = auto_fill_scenario({}, backtest_service)
        return risk_budget_module.compute(scenario).to_dict()

    @mcp.tool()
    def get_current_macro_season() -> dict[str, Any]:
        """Get Bittel's 4-season classification (Spring/Summer/Autumn/Winter)
        based on growth direction and inflation direction with hysteresis.
        Includes confirmation status and months in current season."""
        return phase_service.get(force=False).to_dict()

    @mcp.tool()
    def get_liquidity_state() -> dict[str, Any]:
        """Get the dashboard's liquidity reading: status (EXPANDING/NEUTRAL/
        CONTRACTING) plus the 6 underlying metrics (M2, RRP, TGA, Fed BS, DXY,
        Global M2 proxy) with their individual classifications."""
        snapshot = dashboard_service.get_snapshot(force=False)
        return {
            "status": snapshot["dashboards"]["liquidity"]["status"],
            "summary": snapshot["dashboards"]["liquidity"].get("summary"),
            "metrics": snapshot["dashboards"]["liquidity"]["metrics"],
        }

    @mcp.tool()
    def get_cycle_state() -> dict[str, Any]:
        """Get the dashboard's business-cycle reading: status (EXPANSION/LATE
        CYCLE/TRANSITION/CONTRACTION) plus the 5 underlying metrics (ISM PMI,
        Yield Curve, Credit Spreads, Jobless Claims, Korean Exports)."""
        snapshot = dashboard_service.get_snapshot(force=False)
        return {
            "status": snapshot["dashboards"]["business-cycle"]["status"],
            "summary": snapshot["dashboards"]["business-cycle"].get("summary"),
            "metrics": snapshot["dashboards"]["business-cycle"]["metrics"],
        }

    @mcp.tool()
    def get_current_allocation() -> dict[str, Any]:
        """Get the engine's current asset-by-asset allocation: top picks with
        weights, bottom picks to avoid, and the cash residual. Driven by the
        live correlation matrix against the auto-filled macro scenario."""
        scenario = auto_fill_scenario({}, backtest_service)
        ranking = correlation_service.rank_scenario(scenario)
        return {
            "available": ranking.get("available", False),
            "allocation": ranking.get("allocation"),
            "scenario": scenario,
        }

    @mcp.tool()
    def run_what_if_outcome(
        start_date: str,
        amount: float = 100000.0,
        asset_key: str | None = None,
        basket_key: str | None = None,
        end_date: str | None = None,
        mode: Literal["buy_and_hold"] = "buy_and_hold",
    ) -> dict[str, Any]:
        """Backtest a fixed amount in a single asset OR a preset basket using
        buy-and-hold. Returns ending value, total return, CAGR, max drawdown,
        best/worst month, and the equity curve.

        Exactly ONE of asset_key or basket_key must be provided.

        asset_key options: GOLD, QQQ, SMH, SPY, HYG, LQD, TLT, BIL, BTC, IEF
        basket_key options: 60_40, ALL_WEATHER, FRAMEWORK_PORTFOLIO

        FRAMEWORK_PORTFOLIO routes to run_framework_portfolio_outcome.

        Use this when the user asks 'what would £X in Y have done since Z?'
        Never invent return numbers — always call this tool.
        """
        if basket_key and basket_key.upper() == "FRAMEWORK_PORTFOLIO":
            return run_framework_portfolio_outcome(
                amount=amount, start_date=start_date, end_date=end_date
            )
        result = whatif_module.run(
            amount=amount,
            asset_key=asset_key,
            basket_key=basket_key,
            start_date=start_date,
            end_date=end_date,
            fetcher=backtest_service._download_close,
            mode=mode,
        )
        return result.to_dict()

    @mcp.tool()
    def run_framework_portfolio_outcome(
        start_date: str = "2010-01-01",
        amount: float = 100000.0,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Backtest the dashboard's own allocation framework: each month-end,
        compute the Risk Budget on point-in-time factor z-scores, map to band,
        rebalance into the band's basket. Returns ending value, CAGR, max
        drawdown, average cash level, best/worst 12-month windows, and the
        per-month stance distribution.

        This is THE answer to "would following the dashboard have protected
        capital?" Always call this rather than estimating.
        """
        result = framework_portfolio_module.run_backtest(
            initial_amount=amount,
            start_date=start_date,
            end_date=end_date,
            factor_series=backtest_service.factor_series(),
            price_fetcher=backtest_service._download_close,
        )
        return result.to_dict()

    @mcp.tool()
    def generate_weekly_cio_message() -> dict[str, Any]:
        """Render the weekly CIO message body without sending. Use this when
        the user wants a preview of what would go out on Monday morning, or
        wants the current 'what changed this week' summary in writing."""
        curr = current_state_fn()
        state = state_store.load()
        prev = state.get("hermes_weekly")
        return {
            "message": cio_message_module.render(curr, prev),
            "state": curr.to_dict(),
        }

    @mcp.tool()
    def send_weekly_telegram_message() -> dict[str, Any]:
        """Send the weekly CIO message to the configured Telegram chat AND
        persist this week's snapshot so next week can diff against it. Use
        this only when the user explicitly asks to send / publish the weekly
        update. Returns telegram_sent (bool) and the rendered message."""
        curr = current_state_fn()
        message = cio_message_module.generate_and_persist(curr, state_store)
        sent, err = (False, None)
        try:
            sent = telegram_client.send_message(message)
        except Exception as exc:
            err = str(exc)
        return {"telegram_sent": sent, "telegram_error": err, "message": message}

    return mcp
