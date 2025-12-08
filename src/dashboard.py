"""Retro terminal-styled web dashboard for Polymarket bot."""

import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Any

from aiohttp import web
import aiohttp_sse

# Store recent logs and stats
log_buffer: Deque[Dict[str, Any]] = deque(maxlen=100)
stats: Dict[str, Any] = {
    "daily_pnl": 0.0,
    "daily_trades": 0,
    "daily_exposure": 0.0,
    "active_markets": 0,
    "circuit_breaker": "NORMAL",
    "websocket": "DISCONNECTED",
    "opportunities_detected": 0,
    "opportunities_executed": 0,
    "last_trade": None,
    "uptime_start": datetime.utcnow().isoformat(),
}

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GABAGOOL // POLYMARKET BOT</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=VT323&family=Share+Tech+Mono&display=swap');

        :root {
            --green: #00ff41;
            --dim-green: #00aa2a;
            --dark-green: #003b00;
            --amber: #ffb000;
            --red: #ff0040;
            --cyan: #00ffff;
            --bg: #0a0a0a;
            --panel-bg: #0d1117;
            --border: #1a3a1a;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            background: var(--bg);
            color: var(--green);
            font-family: 'Share Tech Mono', 'Courier New', monospace;
            min-height: 100vh;
            overflow-x: hidden;
        }

        /* CRT scan line effect */
        body::before {
            content: "";
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            background: repeating-linear-gradient(
                0deg,
                rgba(0, 0, 0, 0.15),
                rgba(0, 0, 0, 0.15) 1px,
                transparent 1px,
                transparent 2px
            );
            z-index: 1000;
        }

        /* Subtle flicker */
        @keyframes flicker {
            0%, 100% { opacity: 1; }
            92% { opacity: 1; }
            93% { opacity: 0.8; }
            94% { opacity: 1; }
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            animation: flicker 4s infinite;
        }

        /* Header */
        .header {
            text-align: center;
            padding: 20px 0;
            border-bottom: 1px solid var(--border);
            margin-bottom: 20px;
        }

        .header h1 {
            font-family: 'VT323', monospace;
            font-size: 3rem;
            letter-spacing: 8px;
            text-shadow: 0 0 10px var(--green), 0 0 20px var(--green);
            margin-bottom: 5px;
        }

        .header .subtitle {
            color: var(--dim-green);
            font-size: 0.9rem;
            letter-spacing: 4px;
        }

        .status-bar {
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-top: 15px;
            font-size: 0.85rem;
        }

        .status-item {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--green);
            box-shadow: 0 0 6px var(--green);
            animation: pulse 2s infinite;
        }

        .status-dot.warning { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
        .status-dot.error { background: var(--red); box-shadow: 0 0 6px var(--red); }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        /* Grid layout */
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }

        /* Panels */
        .panel {
            background: var(--panel-bg);
            border: 1px solid var(--border);
            border-radius: 4px;
            overflow: hidden;
        }

        .panel-header {
            background: linear-gradient(90deg, var(--dark-green), transparent);
            padding: 10px 15px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .panel-title {
            font-size: 0.9rem;
            letter-spacing: 2px;
            color: var(--green);
        }

        .panel-body {
            padding: 15px;
        }

        /* Stats */
        .stat-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
        }

        .stat {
            text-align: center;
            padding: 10px;
            background: rgba(0, 255, 65, 0.03);
            border: 1px solid var(--border);
            border-radius: 4px;
        }

        .stat-value {
            font-family: 'VT323', monospace;
            font-size: 2rem;
            color: var(--green);
            text-shadow: 0 0 5px var(--green);
        }

        .stat-value.positive { color: var(--green); }
        .stat-value.negative { color: var(--red); text-shadow: 0 0 5px var(--red); }
        .stat-value.warning { color: var(--amber); text-shadow: 0 0 5px var(--amber); }

        .stat-label {
            font-size: 0.75rem;
            color: var(--dim-green);
            letter-spacing: 1px;
            margin-top: 5px;
        }

        /* Log terminal */
        .log-terminal {
            height: 400px;
            overflow-y: auto;
            font-size: 0.8rem;
            line-height: 1.6;
            padding: 10px;
            background: #000;
            border: 1px solid var(--border);
        }

        .log-terminal::-webkit-scrollbar {
            width: 8px;
        }

        .log-terminal::-webkit-scrollbar-track {
            background: var(--bg);
        }

        .log-terminal::-webkit-scrollbar-thumb {
            background: var(--dark-green);
            border-radius: 4px;
        }

        .log-line {
            padding: 2px 0;
            border-bottom: 1px solid rgba(0, 255, 65, 0.05);
        }

        .log-time {
            color: var(--dim-green);
            margin-right: 10px;
        }

        .log-level {
            display: inline-block;
            width: 60px;
            text-align: center;
            margin-right: 10px;
            padding: 1px 4px;
            border-radius: 2px;
        }

        .log-level.info { color: var(--cyan); }
        .log-level.warning { color: var(--amber); }
        .log-level.error { color: var(--red); }
        .log-level.debug { color: var(--dim-green); }

        .log-msg { color: #ccc; }
        .log-extra { color: var(--dim-green); font-size: 0.75rem; }

        /* Trade feed */
        .trade-list {
            max-height: 300px;
            overflow-y: auto;
        }

        .trade-item {
            padding: 10px;
            border-bottom: 1px solid var(--border);
            display: grid;
            grid-template-columns: auto 1fr auto;
            gap: 10px;
            align-items: center;
        }

        .trade-item:last-child {
            border-bottom: none;
        }

        .trade-asset {
            font-weight: bold;
            color: var(--cyan);
        }

        .trade-details {
            font-size: 0.8rem;
            color: var(--dim-green);
        }

        .trade-profit {
            font-family: 'VT323', monospace;
            font-size: 1.2rem;
        }

        .trade-profit.positive { color: var(--green); }
        .trade-profit.negative { color: var(--red); }

        /* Circuit breaker */
        .circuit-status {
            text-align: center;
            padding: 20px;
        }

        .circuit-level {
            font-family: 'VT323', monospace;
            font-size: 2.5rem;
            margin-bottom: 10px;
        }

        .circuit-level.normal { color: var(--green); }
        .circuit-level.warning { color: var(--amber); }
        .circuit-level.caution { color: var(--amber); }
        .circuit-level.halt { color: var(--red); animation: blink 0.5s infinite; }

        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }

        .circuit-bar {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-top: 15px;
        }

        .circuit-segment {
            width: 60px;
            height: 8px;
            background: var(--dark-green);
            border-radius: 4px;
        }

        .circuit-segment.active { background: var(--green); box-shadow: 0 0 10px var(--green); }
        .circuit-segment.warning { background: var(--amber); box-shadow: 0 0 10px var(--amber); }
        .circuit-segment.halt { background: var(--red); box-shadow: 0 0 10px var(--red); }

        /* Market prices */
        .market-row {
            display: grid;
            grid-template-columns: 80px 1fr 1fr 80px;
            gap: 10px;
            padding: 10px 0;
            border-bottom: 1px solid var(--border);
            align-items: center;
        }

        .market-asset { color: var(--cyan); font-weight: bold; }
        .market-price { text-align: center; }
        .market-spread { text-align: right; }
        .market-spread.good { color: var(--green); }
        .market-spread.low { color: var(--amber); }

        /* Footer */
        .footer {
            text-align: center;
            padding: 20px;
            color: var(--dim-green);
            font-size: 0.8rem;
            border-top: 1px solid var(--border);
        }

        /* Dry run banner */
        .dry-run-banner {
            background: linear-gradient(90deg, var(--amber), transparent);
            color: #000;
            text-align: center;
            padding: 8px;
            font-weight: bold;
            letter-spacing: 2px;
            animation: blink 2s infinite;
        }
    </style>
</head>
<body>
    <div id="dry-run-banner" class="dry-run-banner" style="display: none;">
        [ DRY RUN MODE - NO REAL TRADES ]
    </div>

    <div class="container">
        <header class="header">
            <h1>GABAGOOL</h1>
            <div class="subtitle">POLYMARKET ARBITRAGE BOT v0.1.0</div>
            <div class="status-bar">
                <div class="status-item">
                    <div id="ws-status" class="status-dot"></div>
                    <span>WEBSOCKET</span>
                </div>
                <div class="status-item">
                    <div id="api-status" class="status-dot"></div>
                    <span>CLOB API</span>
                </div>
                <div class="status-item">
                    <span id="uptime">00:00:00</span>
                    <span>UPTIME</span>
                </div>
            </div>
        </header>

        <div class="grid">
            <!-- Daily Stats -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-title">[ DAILY STATS ]</span>
                </div>
                <div class="panel-body">
                    <div class="stat-grid">
                        <div class="stat">
                            <div id="daily-pnl" class="stat-value">$0.00</div>
                            <div class="stat-label">P&L</div>
                        </div>
                        <div class="stat">
                            <div id="daily-trades" class="stat-value">0</div>
                            <div class="stat-label">TRADES</div>
                        </div>
                        <div class="stat">
                            <div id="daily-exposure" class="stat-value">$0.00</div>
                            <div class="stat-label">EXPOSURE</div>
                        </div>
                        <div class="stat">
                            <div id="active-markets" class="stat-value">0</div>
                            <div class="stat-label">MARKETS</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Circuit Breaker -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-title">[ CIRCUIT BREAKER ]</span>
                </div>
                <div class="panel-body">
                    <div class="circuit-status">
                        <div id="circuit-level" class="circuit-level normal">NORMAL</div>
                        <div class="circuit-bar">
                            <div id="cb-0" class="circuit-segment active"></div>
                            <div id="cb-1" class="circuit-segment"></div>
                            <div id="cb-2" class="circuit-segment"></div>
                            <div id="cb-3" class="circuit-segment"></div>
                        </div>
                        <div style="margin-top: 15px; font-size: 0.8rem; color: var(--dim-green);">
                            NORMAL → WARNING → CAUTION → HALT
                        </div>
                    </div>
                </div>
            </div>

            <!-- Opportunities -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-title">[ OPPORTUNITIES ]</span>
                </div>
                <div class="panel-body">
                    <div class="stat-grid">
                        <div class="stat">
                            <div id="opps-detected" class="stat-value">0</div>
                            <div class="stat-label">DETECTED</div>
                        </div>
                        <div class="stat">
                            <div id="opps-executed" class="stat-value">0</div>
                            <div class="stat-label">EXECUTED</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Log Terminal -->
        <div class="panel" style="grid-column: 1 / -1;">
            <div class="panel-header">
                <span class="panel-title">[ SYSTEM LOG ]</span>
                <span style="color: var(--dim-green); font-size: 0.8rem;">
                    <span id="log-count">0</span> entries
                </span>
            </div>
            <div class="panel-body" style="padding: 0;">
                <div id="log-terminal" class="log-terminal"></div>
            </div>
        </div>

        <footer class="footer">
            GABAGOOL ARBITRAGE BOT // POLYMARKET CLOB //
            <span id="current-time"></span>
        </footer>
    </div>

    <script>
        // SSE connection for real-time updates
        const evtSource = new EventSource('/dashboard/events');

        evtSource.onmessage = function(event) {
            const data = JSON.parse(event.data);
            updateDashboard(data);
        };

        evtSource.onerror = function(err) {
            document.getElementById('ws-status').classList.add('error');
        };

        function updateDashboard(data) {
            // Update stats
            if (data.stats) {
                const s = data.stats;

                // P&L with color
                const pnlEl = document.getElementById('daily-pnl');
                pnlEl.textContent = '$' + s.daily_pnl.toFixed(2);
                pnlEl.className = 'stat-value ' + (s.daily_pnl >= 0 ? 'positive' : 'negative');

                document.getElementById('daily-trades').textContent = s.daily_trades;
                document.getElementById('daily-exposure').textContent = '$' + s.daily_exposure.toFixed(2);
                document.getElementById('active-markets').textContent = s.active_markets;
                document.getElementById('opps-detected').textContent = s.opportunities_detected;
                document.getElementById('opps-executed').textContent = s.opportunities_executed;

                // Circuit breaker
                const cbLevel = s.circuit_breaker.toUpperCase();
                const cbEl = document.getElementById('circuit-level');
                cbEl.textContent = cbLevel;
                cbEl.className = 'circuit-level ' + cbLevel.toLowerCase();

                // Update circuit bar
                const levels = ['NORMAL', 'WARNING', 'CAUTION', 'HALT'];
                const currentIdx = levels.indexOf(cbLevel);
                for (let i = 0; i < 4; i++) {
                    const seg = document.getElementById('cb-' + i);
                    seg.className = 'circuit-segment';
                    if (i <= currentIdx) {
                        if (cbLevel === 'HALT') seg.classList.add('halt');
                        else if (cbLevel === 'WARNING' || cbLevel === 'CAUTION') seg.classList.add('warning');
                        else seg.classList.add('active');
                    }
                }

                // WebSocket status
                const wsEl = document.getElementById('ws-status');
                wsEl.className = 'status-dot ' + (s.websocket === 'CONNECTED' ? '' : 'error');

                // Dry run banner
                if (s.dry_run) {
                    document.getElementById('dry-run-banner').style.display = 'block';
                }
            }

            // Update logs
            if (data.logs) {
                const terminal = document.getElementById('log-terminal');
                data.logs.forEach(log => {
                    const line = document.createElement('div');
                    line.className = 'log-line';

                    const levelClass = (log.level || 'info').toLowerCase();
                    const extra = log.extra ? ' <span class="log-extra">' + JSON.stringify(log.extra) + '</span>' : '';

                    line.innerHTML =
                        '<span class="log-time">' + log.timestamp + '</span>' +
                        '<span class="log-level ' + levelClass + '">[' + (log.level || 'INFO').toUpperCase() + ']</span>' +
                        '<span class="log-msg">' + log.message + '</span>' + extra;

                    terminal.appendChild(line);
                });

                // Auto-scroll to bottom
                terminal.scrollTop = terminal.scrollHeight;
                document.getElementById('log-count').textContent = terminal.children.length;
            }
        }

        // Update time
        function updateTime() {
            document.getElementById('current-time').textContent = new Date().toISOString();
        }
        setInterval(updateTime, 1000);
        updateTime();

        // Calculate uptime
        let uptimeStart = null;
        function updateUptime() {
            if (!uptimeStart) return;
            const diff = Math.floor((Date.now() - uptimeStart) / 1000);
            const h = Math.floor(diff / 3600).toString().padStart(2, '0');
            const m = Math.floor((diff % 3600) / 60).toString().padStart(2, '0');
            const s = (diff % 60).toString().padStart(2, '0');
            document.getElementById('uptime').textContent = h + ':' + m + ':' + s;
        }
        setInterval(updateUptime, 1000);

        // Initial load
        fetch('/dashboard/state')
            .then(r => r.json())
            .then(data => {
                uptimeStart = new Date(data.stats.uptime_start).getTime();
                updateDashboard(data);
            });
    </script>
</body>
</html>
"""


class DashboardServer:
    """Retro terminal-styled web dashboard."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self._app: web.Application = None
        self._runner: web.AppRunner = None
        self._clients: list = []

    async def start(self) -> None:
        """Start the dashboard server."""
        self._app = web.Application()
        self._app.router.add_get("/dashboard", self._handle_dashboard)
        self._app.router.add_get("/dashboard/", self._handle_dashboard)
        self._app.router.add_get("/dashboard/state", self._handle_state)
        self._app.router.add_get("/dashboard/events", self._handle_events)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

        import structlog
        log = structlog.get_logger()
        log.info(
            "Dashboard started",
            url=f"http://{self.host}:{self.port}/dashboard",
        )

    async def stop(self) -> None:
        """Stop the dashboard server."""
        if self._runner:
            await self._runner.cleanup()

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        """Serve the dashboard HTML."""
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _handle_state(self, request: web.Request) -> web.Response:
        """Get current state."""
        return web.json_response({
            "stats": stats,
            "logs": list(log_buffer),
        })

    async def _handle_events(self, request: web.Request) -> web.StreamResponse:
        """SSE endpoint for real-time updates."""
        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"
        await response.prepare(request)

        self._clients.append(response)
        try:
            while True:
                await asyncio.sleep(1)
                if response.task.done():
                    break
        finally:
            self._clients.remove(response)

        return response

    async def broadcast(self, data: Dict[str, Any]) -> None:
        """Broadcast update to all connected clients."""
        message = f"data: {json.dumps(data)}\n\n"
        for client in self._clients[:]:
            try:
                await client.write(message.encode())
            except Exception:
                self._clients.remove(client)


# Global dashboard instance
dashboard: DashboardServer = None


def add_log(level: str, message: str, **extra) -> None:
    """Add a log entry to the buffer."""
    entry = {
        "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
        "extra": extra if extra else None,
    }
    log_buffer.append(entry)

    # Broadcast to clients
    if dashboard:
        asyncio.create_task(dashboard.broadcast({"logs": [entry]}))


def update_stats(**kwargs) -> None:
    """Update dashboard stats."""
    stats.update(kwargs)

    # Broadcast to clients
    if dashboard:
        asyncio.create_task(dashboard.broadcast({"stats": stats}))
