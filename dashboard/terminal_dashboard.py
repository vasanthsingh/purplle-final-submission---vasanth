import asyncio
import json
import websockets
from collections import deque
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich.bar import Bar

WS_ENDPOINT = "ws://localhost:8000/ws/stores"
OUTLET_ID = "ST1008"

def build_layout() -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="bottom", size=12),
        Layout(name="footer", size=3)
    )
    layout["main"].split_row(
        Layout(name="metrics_panel", ratio=1),
        Layout(name="sales_panel", ratio=1),
    )
    layout["bottom"].split_row(
        Layout(name="pipeline_panel", ratio=1),
        Layout(name="alerts_panel", ratio=1),
    )
    return layout

# Footfall history for mini trend (last 30 samples)
_footfall_trend: deque = deque(maxlen=30)

def _mini_chart(values) -> str:
    """Render a compact sparkline from numeric values using Unicode block chars."""
    if not values:
        return ""
    blocks = " ▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    rng = hi - lo or 1
    return "".join(blocks[min(8, int((v - lo) / rng * 8))] for v in values)

def display_metrics(metrics) -> Panel:
    if not metrics:
        return Panel("Waiting for stream...", title="Core Metrics")

    footfall = metrics.get("unique_visitors", 0)
    _footfall_trend.append(footfall)

    tbl = Table(show_header=False, expand=True, border_style="green")
    tbl.add_column("Metric", style="bold white")
    tbl.add_column("Value", justify="right", style="green")

    tbl.add_row("🚶 Footfall", str(footfall))
    tbl.add_row("📈 Conversion", f"{metrics.get('conversion_rate', 0)*100:.1f}%")
    tbl.add_row("💳 Sales", str(metrics.get("pos_transactions", 0)))
    tbl.add_row("⏱️  Queue Depth", str(metrics.get("current_queue_depth", 0)))
    tbl.add_row("🚫 Dropout Rate", f"{metrics.get('abandonment_rate', 0)*100:.1f}%")
    tbl.add_row("🏷️  Staff Found", str(metrics.get("staff_count", 0)))
    if len(_footfall_trend) > 1:
        chart = _mini_chart(list(_footfall_trend))
        tbl.add_row("📊 Trend", Text(chart, style="green"))

    return Panel(tbl, title="🔢 Core Metrics", border_style="green")

def display_sales(metrics) -> Panel:
    if not metrics:
        return Panel("Waiting for stream...", title="Brand Performance")

    tbl = Table(expand=True, border_style="yellow")
    tbl.add_column("Top Brands", style="bold white")
    tbl.add_column("Units", justify="right", style="yellow")

    brands = metrics.get("top_brands", {})
    for b, count in brands.items():
        tbl.add_row(str(b), str(count))

    return Panel(tbl, title="🏆 Brand Performance", border_style="yellow")

def display_pipeline(funnel) -> Panel:
    if not funnel or not funnel.get("stages"):
        return Panel("Waiting for funnel data...", title="Conversion Pipeline")

    stages = funnel["stages"]
    tbl = Table(expand=True, border_style="cyan")
    tbl.add_column("Stage", style="bold white")
    tbl.add_column("Count", justify="right", style="cyan")
    tbl.add_column("Drop-off", justify="right", style="red")
    tbl.add_column("Bar", style="green", min_width=20)

    peak = max((s["count"] for s in stages), default=1) or 1
    for s in stages:
        bar_len = int((s["count"] / peak) * 20) if peak else 0
        bar_str = "█" * bar_len + "░" * (20 - bar_len)
        drop = f"-{s['drop_off_from_prev_pct']:.0f}%" if s["drop_off_from_prev_pct"] > 0 else "—"
        tbl.add_row(s["stage"], str(s["count"]), drop, bar_str)

    conv = funnel.get("conversion_rate", 0)
    return Panel(tbl, title=f"🔄 Conversion Pipeline  (rate: {conv*100:.1f}%)", border_style="cyan")

# Severity styling: distinct icons + colours for visual triage
_ALERT_STYLE = {
    "CRITICAL": {"icon": "🔴", "style": "bold red"},
    "WARN":     {"icon": "🟠", "style": "bold yellow"},
    "INFO":     {"icon": "🟢", "style": "bold green"},
}

def display_alerts(anomalies) -> Panel:
    if not anomalies or not anomalies.get("anomalies"):
        return Panel(
            Text("✅ All clear — no active alerts", style="bold green"),
            title="⚠️  Alert Monitor", border_style="green"
        )

    alert_list = anomalies["anomalies"]
    has_critical = any(a.get("severity") == "CRITICAL" for a in alert_list)
    border = "bold red" if has_critical else "yellow"

    tbl = Table(expand=True, border_style=border)
    tbl.add_column("Sev", style="bold", width=6)
    tbl.add_column("Type", style="white")
    tbl.add_column("Action", style="dim white")

    for a in alert_list:
        sev = a.get("severity", "INFO")
        cfg = _ALERT_STYLE.get(sev, {"icon": "⚪", "style": "white"})
        tbl.add_row(
            Text(f"{cfg['icon']} {sev}", style=cfg["style"]),
            a.get("type", "UNKNOWN"),
            a.get("suggested_action", "—")[:50],
        )

    count = anomalies.get('count', 0)
    return Panel(tbl, title=f"⚠️  Alert Monitor ({count})", border_style=border)

async def main():
    console = Console()
    layout = build_layout()

    while True:
        try:
            async with websockets.connect(f"{WS_ENDPOINT}/{OUTLET_ID}") as ws:
                with Live(layout, refresh_per_second=4, screen=True) as live:
                    async for message in ws:
                        data = json.loads(message)
                        m = data.get("metrics")
                        f = data.get("funnel")
                        a = data.get("anomalies")
                        h = data.get("health")

                        # Header
                        status = h.get("status", "DOWN").upper() if h else "DOWN"
                        color = "green" if status == "OK" else "red" if status == "DOWN" else "yellow"
                        hdr = Text(f" 🔮  Vortex Analytics  |  Outlet: {OUTLET_ID}  |  Status: ", style="bold white")
                        hdr.append(status, style=f"bold {color}")
                        layout["header"].update(Panel(hdr, style="blue"))

                        # Main panels
                        layout["metrics_panel"].update(display_metrics(m))
                        layout["sales_panel"].update(display_sales(m))

                        # Bottom panels
                        layout["pipeline_panel"].update(display_pipeline(f))
                        layout["alerts_panel"].update(display_alerts(a))

                        # Footer
                        alert_count = a.get("count", 0) if a else 0
                        footfall = m.get("unique_visitors", 0) if m else 0
                        ftr = Text(
                            f" Footfall: {footfall}  |  Alerts: {alert_count}  |  Press Ctrl+C to exit",
                            style="bold white"
                        )
                        layout["footer"].update(Panel(ftr, style="dim"))
        except (websockets.ConnectionClosed, ConnectionRefusedError):
            print("Connection lost. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(3)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
