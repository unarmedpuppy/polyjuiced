"""Retro terminal-styled web dashboard for Polymarket bot."""

import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Any, List, Optional, TYPE_CHECKING

import structlog
from aiohttp import web

log = structlog.get_logger()

if TYPE_CHECKING:
    from .persistence import Database

# Store recent logs, stats, trades, markets, and decisions (in-memory cache, backed by SQLite)
log_buffer: Deque[Dict[str, Any]] = deque(maxlen=100)
trade_history: Deque[Dict[str, Any]] = deque(maxlen=50)
decisions_buffer: Deque[Dict[str, Any]] = deque(maxlen=20)  # Recent strategy decisions
active_markets: Dict[str, Dict[str, Any]] = {}  # condition_id -> market info
stats: Dict[str, Any] = {
    "daily_pnl": 0.0,
    "daily_trades": 0,
    "daily_exposure": 0.0,
    "active_markets": 0,
    "circuit_breaker": "NORMAL",
    "websocket": "DISCONNECTED",
    "clob_status": "DISCONNECTED",
    "opportunities_detected": 0,
    "opportunities_executed": 0,
    "wins": 0,
    "losses": 0,
    "pending": 0,
    "last_trade": None,
    "uptime_start": datetime.utcnow().isoformat(),
    # All-time stats (loaded from DB)
    "all_time_pnl": 0.0,
    "all_time_trades": 0,
    "all_time_wins": 0,
    "all_time_losses": 0,
    # Wallet balance
    "wallet_balance": 0.0,
    # Strategy status (for UI indicators)
    "arbitrage_enabled": True,
    "directional_enabled": False,
    "near_resolution_enabled": True,
}

# Database reference (set during initialization)
_db: Optional["Database"] = None

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

        * { margin: 0; padding: 0; box-sizing: border-box; }

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
            top: 0; left: 0;
            width: 100%; height: 100%;
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

        @keyframes flicker {
            0%, 100% { opacity: 1; }
            92% { opacity: 1; }
            93% { opacity: 0.8; }
            94% { opacity: 1; }
        }

        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 20px;
            animation: flicker 4s infinite;
        }

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

        .grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            margin-bottom: 20px;
        }

        .grid-2col {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }

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

        .panel-body { padding: 15px; }

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

        /* Trade history */
        .trade-list {
            max-height: 400px;
            overflow-y: auto;
        }

        .trade-item {
            padding: 12px;
            border-bottom: 1px solid var(--border);
            display: grid;
            grid-template-columns: 70px 1fr 100px 80px;
            gap: 15px;
            align-items: center;
        }

        .trade-item:last-child { border-bottom: none; }
        .trade-item.win { border-left: 3px solid var(--green); }
        .trade-item.loss { border-left: 3px solid var(--red); }
        .trade-item.pending { border-left: 3px solid var(--amber); }

        .trade-time {
            font-size: 0.75rem;
            color: var(--dim-green);
        }

        .trade-info { }

        .trade-asset {
            font-weight: bold;
            color: var(--cyan);
            font-size: 1rem;
        }

        .trade-details {
            font-size: 0.75rem;
            color: var(--dim-green);
            margin-top: 3px;
        }

        .trade-result {
            text-align: center;
        }

        .trade-status {
            font-family: 'VT323', monospace;
            font-size: 1rem;
            padding: 4px 8px;
            border-radius: 3px;
        }

        .trade-status.win {
            color: var(--green);
            background: rgba(0, 255, 65, 0.1);
            border: 1px solid var(--green);
        }

        .trade-status.loss {
            color: var(--red);
            background: rgba(255, 0, 64, 0.1);
            border: 1px solid var(--red);
        }

        .trade-status.pending {
            color: var(--amber);
            background: rgba(255, 176, 0, 0.1);
            border: 1px solid var(--amber);
            animation: pulse 2s infinite;
        }

        .trade-profit {
            font-family: 'VT323', monospace;
            font-size: 1.3rem;
            text-align: right;
        }

        .trade-profit.positive { color: var(--green); }
        .trade-profit.negative { color: var(--red); }

        /* Log terminal */
        .log-terminal {
            height: 300px;
            overflow-y: auto;
            font-size: 0.8rem;
            line-height: 1.6;
            padding: 10px;
            background: #000;
            border: 1px solid var(--border);
        }

        .log-terminal::-webkit-scrollbar { width: 8px; }
        .log-terminal::-webkit-scrollbar-track { background: var(--bg); }
        .log-terminal::-webkit-scrollbar-thumb { background: var(--dark-green); border-radius: 4px; }

        .log-line {
            padding: 2px 0;
            border-bottom: 1px solid rgba(0, 255, 65, 0.05);
        }

        .log-time { color: var(--dim-green); margin-right: 10px; }

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
        .log-level.trade { color: var(--green); }
        .log-level.resolution { color: #ff00ff; }

        .log-msg { color: #ccc; }
        .log-extra { color: var(--dim-green); font-size: 0.75rem; }

        /* P&L Chart */
        .chart-timeframe {
            display: flex;
            gap: 5px;
        }

        .timeframe-btn {
            background: var(--dark-green);
            border: 1px solid var(--border);
            color: var(--dim-green);
            padding: 2px 8px;
            font-family: inherit;
            font-size: 0.7rem;
            cursor: pointer;
            transition: all 0.2s;
        }

        .timeframe-btn.active {
            background: var(--green);
            color: var(--bg);
            border-color: var(--green);
        }

        .timeframe-btn:hover {
            border-color: var(--green);
        }

        #pnl-chart {
            width: 100%;
            height: 100px;
            display: block;
        }

        /* Win/Loss stats */
        .winloss {
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-top: 10px;
        }

        .winloss-item {
            text-align: center;
        }

        .winloss-value {
            font-family: 'VT323', monospace;
            font-size: 1.5rem;
        }

        .winloss-value.wins { color: var(--green); }
        .winloss-value.losses { color: var(--red); }
        .winloss-value.pending { color: var(--amber); }

        .winloss-label {
            font-size: 0.7rem;
            color: var(--dim-green);
        }

        .footer {
            text-align: center;
            padding: 20px;
            color: var(--dim-green);
            font-size: 0.8rem;
            border-top: 1px solid var(--border);
        }

        .dry-run-banner {
            background: linear-gradient(90deg, var(--amber), transparent);
            color: #000;
            text-align: center;
            padding: 8px;
            font-weight: bold;
            letter-spacing: 2px;
            animation: blink 2s infinite;
        }

        @media (max-width: 1200px) {
            .grid { grid-template-columns: repeat(2, 1fr); }
            .grid-2col { grid-template-columns: 1fr; }
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
                <div class="status-item" style="margin-left: 20px; padding-left: 20px; border-left: 1px solid var(--border);">
                    <span style="color: var(--cyan); font-family: 'VT323', monospace; font-size: 1.2rem;">$<span id="wallet-balance">0.00</span></span>
                    <span>WALLET</span>
                </div>
                <!-- Strategy Status Indicators -->
                <div class="status-item" style="margin-left: 20px; padding-left: 20px; border-left: 1px solid var(--border);">
                    <div id="arb-status" class="status-dot"></div>
                    <span>ARBITRAGE</span>
                </div>
                <div class="status-item">
                    <div id="dir-status" class="status-dot error"></div>
                    <span>DIRECTIONAL</span>
                </div>
                <div class="status-item">
                    <div id="nr-status" class="status-dot"></div>
                    <span>NEAR-RES</span>
                </div>
            </div>
        </header>

        <div class="grid">
            <!-- Total Stats -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-title">[ P&L ]</span>
                </div>
                <div class="panel-body">
                    <div style="text-align: center;">
                        <div id="daily-pnl" class="stat-value" style="font-size: 2.5rem;">$0.00</div>
                        <div class="stat-label">TOTAL P&L</div>
                    </div>
                </div>
            </div>

            <!-- Win/Loss Record -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-title">[ RECORD ]</span>
                </div>
                <div class="panel-body">
                    <div class="winloss">
                        <div class="winloss-item">
                            <div id="wins" class="winloss-value wins">0</div>
                            <div class="winloss-label">WINS</div>
                        </div>
                        <div class="winloss-item">
                            <div id="losses" class="winloss-value losses">0</div>
                            <div class="winloss-label">LOSSES</div>
                        </div>
                        <div class="winloss-item">
                            <div id="pending" class="winloss-value pending">0</div>
                            <div class="winloss-label">PENDING</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Exposure -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-title">[ EXPOSURE ]</span>
                </div>
                <div class="panel-body">
                    <div style="text-align: center;">
                        <div id="daily-exposure" class="stat-value" style="font-size: 2.5rem;">$0.00</div>
                        <div class="stat-label">DAILY EXPOSURE</div>
                    </div>
                </div>
            </div>

            <!-- P&L Chart -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-title">[ P&L CHART ]</span>
                    <div class="chart-timeframe">
                        <button class="timeframe-btn active" data-tf="24h">24H</button>
                        <button class="timeframe-btn" data-tf="7d">7D</button>
                        <button class="timeframe-btn" data-tf="all">ALL</button>
                    </div>
                </div>
                <div class="panel-body" style="padding: 10px;">
                    <canvas id="pnl-chart" width="280" height="100"></canvas>
                </div>
            </div>
        </div>

        <!-- Active Markets Panel -->
        <div class="panel" style="margin-bottom: 20px;">
            <div class="panel-header">
                <span class="panel-title">[ ACTIVE MARKETS ]</span>
                <span style="color: var(--dim-green); font-size: 0.8rem;">
                    <span id="market-count">0</span> found / <span id="tradeable-count">0</span> tradeable
                </span>
            </div>
            <div class="panel-body" style="padding: 0;">
                <div id="markets-list" style="max-height: 200px; overflow-y: auto;">
                    <div style="padding: 20px; text-align: center; color: var(--dim-green);">
                        Searching for markets...
                    </div>
                </div>
            </div>
        </div>

        <!-- Strategy Decisions Panel -->
        <div class="panel" style="margin-bottom: 20px;">
            <div class="panel-header">
                <span class="panel-title">[ STRATEGY DECISIONS ]</span>
                <span style="color: var(--dim-green); font-size: 0.8rem;">
                    Real-time evaluation
                </span>
            </div>
            <div class="panel-body" style="padding: 0;">
                <div id="decisions-list" style="max-height: 180px; overflow-y: auto; font-size: 0.85rem;">
                    <div style="padding: 15px; text-align: center; color: var(--dim-green);">
                        Waiting for market evaluation...
                    </div>
                </div>
            </div>
        </div>

        <div class="grid-2col">
            <!-- Trade History -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-title">[ TRADE HISTORY ]</span>
                    <span style="color: var(--dim-green); font-size: 0.8rem;">
                        <span id="trade-count">0</span> trades
                    </span>
                </div>
                <div class="panel-body" style="padding: 0;">
                    <div id="trade-list" class="trade-list">
                        <div style="padding: 20px; text-align: center; color: var(--dim-green);">
                            No trades yet...
                        </div>
                    </div>
                </div>
            </div>

            <!-- Log Terminal -->
            <div class="panel">
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
        </div>

        <footer class="footer">
            GABAGOOL ARBITRAGE BOT // POLYMARKET CLOB //
            <span id="current-time"></span>
        </footer>
    </div>

    <script>
        // SSE connection for real-time updates
        const evtSource = new EventSource('/dashboard/events');

        // Debounce rapid updates using requestAnimationFrame
        let pendingMarketUpdate = null;
        let pendingMarketData = null;

        evtSource.onmessage = function(event) {
            const data = JSON.parse(event.data);

            // Debounce market updates to prevent flickering
            if (data.markets) {
                pendingMarketData = data.markets;
                if (!pendingMarketUpdate) {
                    pendingMarketUpdate = requestAnimationFrame(() => {
                        updateMarketsOptimized(pendingMarketData);
                        pendingMarketUpdate = null;
                    });
                }
                // Remove markets from data so updateDashboard doesn't process it
                delete data.markets;
            }

            // Process other updates immediately
            if (Object.keys(data).length > 0) {
                updateDashboard(data);
            }
        };

        evtSource.onerror = function(err) {
            document.getElementById('ws-status').classList.add('error');
        };

        // Cache for market row elements to enable incremental updates
        const marketRowCache = new Map();

        // Convert UTC timestamp to CST (UTC-6) for display
        function utcToCst(utcTimeStr) {
            // If already formatted as HH:MM:SS, convert it
            if (!utcTimeStr) return 'N/A';

            // Handle HH:MM:SS format (from server)
            if (/^\\d{2}:\\d{2}:\\d{2}$/.test(utcTimeStr)) {
                const [h, m, s] = utcTimeStr.split(':').map(Number);
                const now = new Date();
                const utcDate = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), h, m, s));
                return utcDate.toLocaleTimeString('en-US', {
                    timeZone: 'America/Chicago',
                    hour12: false,
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit'
                });
            }

            // Handle HH:MM UTC format (market end times)
            if (/^\\d{2}:\\d{2} UTC$/.test(utcTimeStr)) {
                const [timepart] = utcTimeStr.split(' ');
                const [h, m] = timepart.split(':').map(Number);
                const now = new Date();
                const utcDate = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), h, m, 0));
                return utcDate.toLocaleTimeString('en-US', {
                    timeZone: 'America/Chicago',
                    hour12: false,
                    hour: '2-digit',
                    minute: '2-digit'
                }) + ' CST';
            }

            return utcTimeStr;
        }

        // Limit DOM children to prevent memory issues
        const MAX_LOG_ENTRIES = 100;
        const MAX_TRADE_ENTRIES = 50;
        const MAX_DECISION_ENTRIES = 20;

        function trimChildren(element, maxCount) {
            while (element.children.length > maxCount) {
                element.removeChild(element.lastChild);
            }
        }

        // Optimized market update - only updates changed cells, not entire table
        function updateMarketsOptimized(markets) {
            const marketsList = document.getElementById('markets-list');
            const marketIds = new Set(Object.keys(markets));

            let foundCount = 0;
            let tradeableCount = 0;

            // Handle empty state
            if (marketIds.size === 0) {
                marketsList.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--dim-green);">No markets found. Waiting for next 15-minute window...</div>';
                marketRowCache.clear();
                document.getElementById('market-count').textContent = '0';
                document.getElementById('tradeable-count').textContent = '0';
                return;
            }

            // Create table structure if not exists
            let table = marketsList.querySelector('table');
            if (!table) {
                marketsList.innerHTML = '';
                table = document.createElement('table');
                table.style.cssText = 'width: 100%; border-collapse: collapse; font-size: 0.85rem;';

                const header = document.createElement('tr');
                header.style.cssText = 'background: var(--dark-green); color: var(--green);';
                header.innerHTML = `
                    <th style="padding: 8px; text-align: left;">Asset</th>
                    <th style="padding: 8px; text-align: left;">End Time</th>
                    <th style="padding: 8px; text-align: right;">Time Left</th>
                    <th style="padding: 8px; text-align: right;">Up Price</th>
                    <th style="padding: 8px; text-align: right;">Down Price</th>
                    <th style="padding: 8px; text-align: center;">Status</th>
                `;
                table.appendChild(header);
                marketsList.appendChild(table);
            }

            // Remove rows for markets no longer in the data
            for (const [id, row] of marketRowCache.entries()) {
                if (!marketIds.has(id)) {
                    row.remove();
                    marketRowCache.delete(id);
                }
            }

            // Update or create rows for each market
            for (const [id, m] of Object.entries(markets)) {
                foundCount++;
                const isTradeable = m.seconds_remaining > 60;
                if (isTradeable) tradeableCount++;

                const mins = Math.floor(m.seconds_remaining / 60);
                const secs = Math.floor(m.seconds_remaining % 60);
                const timeLeft = m.seconds_remaining > 0 ? `${mins}m ${secs}s` : 'ENDED';

                let row = marketRowCache.get(id);
                if (!row) {
                    // Create new row
                    row = document.createElement('tr');
                    row.dataset.marketId = id;
                    row.style.cssText = 'border-bottom: 1px solid var(--border);';

                    const marketUrl = m.slug ? `https://polymarket.com/event/${m.slug}` : null;
                    const assetDisplay = marketUrl
                        ? `<a href="${marketUrl}" target="_blank" style="color: var(--cyan); text-decoration: none; font-weight: bold;">${m.asset} ↗</a>`
                        : m.asset;

                    row.innerHTML = `
                        <td style="padding: 8px; color: var(--cyan); font-weight: bold;" class="cell-asset">${assetDisplay}</td>
                        <td style="padding: 8px; color: var(--dim-green);" class="cell-endtime">${utcToCst(m.end_time) || 'N/A'}</td>
                        <td style="padding: 8px; text-align: right;" class="cell-timeleft">${timeLeft}</td>
                        <td style="padding: 8px; text-align: right;" class="cell-upprice">${m.up_price ? (m.up_price * 100).toFixed(1) + '¢' : 'N/A'}</td>
                        <td style="padding: 8px; text-align: right;" class="cell-downprice">${m.down_price ? (m.down_price * 100).toFixed(1) + '¢' : 'N/A'}</td>
                        <td style="padding: 8px; text-align: center;" class="cell-status">${isTradeable ? 'TRADEABLE' : 'EXPIRED'}</td>
                    `;
                    table.appendChild(row);
                    marketRowCache.set(id, row);
                } else {
                    // Update only changed cells (no innerHTML replacement = no flicker)
                    const timeCell = row.querySelector('.cell-timeleft');
                    const upCell = row.querySelector('.cell-upprice');
                    const downCell = row.querySelector('.cell-downprice');
                    const statusCell = row.querySelector('.cell-status');

                    // Update time left
                    if (timeCell.textContent !== timeLeft) {
                        timeCell.textContent = timeLeft;
                        timeCell.style.color = m.seconds_remaining > 60 ? 'var(--green)' : 'var(--red)';
                    }

                    // Update prices
                    const upText = m.up_price ? (m.up_price * 100).toFixed(1) + '¢' : 'N/A';
                    const downText = m.down_price ? (m.down_price * 100).toFixed(1) + '¢' : 'N/A';
                    if (upCell.textContent !== upText) upCell.textContent = upText;
                    if (downCell.textContent !== downText) downCell.textContent = downText;

                    // Update status
                    const statusText = isTradeable ? 'TRADEABLE' : 'EXPIRED';
                    if (statusCell.textContent !== statusText) {
                        statusCell.textContent = statusText;
                        statusCell.style.color = isTradeable ? 'var(--green)' : 'var(--red)';
                    }
                }

                // Update row background based on tradeable status
                row.style.background = isTradeable ? 'rgba(0, 255, 65, 0.05)' : 'rgba(255, 0, 64, 0.05)';
            }

            document.getElementById('market-count').textContent = foundCount;
            document.getElementById('tradeable-count').textContent = tradeableCount;
        }

        function updateDashboard(data) {
            if (data.stats) {
                const s = data.stats;

                const pnlEl = document.getElementById('daily-pnl');
                pnlEl.textContent = '$' + s.daily_pnl.toFixed(2);
                pnlEl.className = 'stat-value ' + (s.daily_pnl >= 0 ? 'positive' : 'negative');

                document.getElementById('daily-exposure').textContent = '$' + s.daily_exposure.toFixed(2);
                document.getElementById('wins').textContent = s.wins || 0;
                document.getElementById('losses').textContent = s.losses || 0;
                document.getElementById('pending').textContent = s.pending || 0;

                const wsEl = document.getElementById('ws-status');
                wsEl.className = 'status-dot ' + (s.websocket === 'CONNECTED' ? '' : 'error');

                // Update wallet balance
                if (s.wallet_balance !== undefined) {
                    document.getElementById('wallet-balance').textContent = s.wallet_balance.toFixed(2);
                }

                // Update strategy status indicators
                const arbEl = document.getElementById('arb-status');
                arbEl.className = 'status-dot ' + (s.arbitrage_enabled ? '' : 'error');

                const dirEl = document.getElementById('dir-status');
                dirEl.className = 'status-dot ' + (s.directional_enabled ? '' : 'error');

                const nrEl = document.getElementById('nr-status');
                nrEl.className = 'status-dot ' + (s.near_resolution_enabled ? '' : 'error');

                if (s.dry_run) {
                    document.getElementById('dry-run-banner').style.display = 'block';
                }
            }

            if (data.logs) {
                const terminal = document.getElementById('log-terminal');
                data.logs.forEach(log => {
                    const line = document.createElement('div');
                    line.className = 'log-line';

                    const levelClass = (log.level || 'info').toLowerCase();
                    const extra = log.extra ? ' <span class="log-extra">' + JSON.stringify(log.extra) + '</span>' : '';

                    line.innerHTML =
                        '<span class="log-time">' + utcToCst(log.timestamp) + '</span>' +
                        '<span class="log-level ' + levelClass + '">[' + (log.level || 'INFO').toUpperCase() + ']</span>' +
                        '<span class="log-msg">' + log.message + '</span>' + extra;

                    terminal.appendChild(line);
                });

                // Trim old entries to prevent memory issues
                trimChildren(terminal, MAX_LOG_ENTRIES);
                terminal.scrollTop = terminal.scrollHeight;
                document.getElementById('log-count').textContent = terminal.children.length;
            }

            if (data.trades) {
                const tradeList = document.getElementById('trade-list');

                // Clear placeholder if present
                if (tradeList.children.length === 1 && tradeList.children[0].style.padding) {
                    tradeList.innerHTML = '';
                }

                data.trades.forEach(trade => {
                    const item = document.createElement('div');
                    const status = trade.status || 'pending';
                    item.className = 'trade-item ' + status;
                    item.id = 'trade-' + trade.id;

                    const profitClass = (trade.actual_profit || trade.expected_profit) >= 0 ? 'positive' : 'negative';
                    const profitValue = trade.actual_profit !== null ? trade.actual_profit : trade.expected_profit;
                    const profitLabel = trade.actual_profit !== null ? '' : '(est)';

                    let statusLabel = 'PENDING';
                    if (status === 'win') statusLabel = 'WIN';
                    else if (status === 'loss') statusLabel = 'LOSS';

                    const tradeUrl = trade.market_slug ? `https://polymarket.com/event/${trade.market_slug}` : null;
                    const tradeAssetDisplay = tradeUrl
                        ? `<a href="${tradeUrl}" target="_blank" style="color: inherit; text-decoration: none;">${trade.asset} ↗</a>`
                        : trade.asset;

                    item.innerHTML = `
                        <div class="trade-time">${utcToCst(trade.time)}<br>${utcToCst(trade.market_time) || ''}</div>
                        <div class="trade-info">
                            <div class="trade-asset">${tradeAssetDisplay}</div>
                            <div class="trade-details">
                                YES: $${trade.yes_cost.toFixed(2)} @ ${(trade.yes_price * 100).toFixed(1)}¢ |
                                NO: $${trade.no_cost.toFixed(2)} @ ${(trade.no_price * 100).toFixed(1)}¢ |
                                Spread: ${trade.spread.toFixed(1)}¢
                            </div>
                        </div>
                        <div class="trade-result">
                            <span class="trade-status ${status}">${statusLabel}</span>
                        </div>
                        <div class="trade-profit ${profitClass}">
                            ${profitValue >= 0 ? '+' : ''}$${profitValue.toFixed(2)}${profitLabel}
                        </div>
                    `;

                    // Insert at top
                    tradeList.insertBefore(item, tradeList.firstChild);
                });

                // Trim old entries to prevent memory issues
                trimChildren(tradeList, MAX_TRADE_ENTRIES);
                document.getElementById('trade-count').textContent = tradeList.children.length;
            }

            // Update existing trade status
            if (data.trade_update) {
                const t = data.trade_update;
                const item = document.getElementById('trade-' + t.id);
                if (item) {
                    item.className = 'trade-item ' + t.status;
                    const statusEl = item.querySelector('.trade-status');
                    statusEl.className = 'trade-status ' + t.status;
                    statusEl.textContent = t.status.toUpperCase();

                    const profitEl = item.querySelector('.trade-profit');
                    profitEl.className = 'trade-profit ' + (t.actual_profit >= 0 ? 'positive' : 'negative');
                    profitEl.textContent = (t.actual_profit >= 0 ? '+' : '') + '$' + t.actual_profit.toFixed(2);
                }
            }

            // Update P&L chart with new data point
            if (data.pnl_update) {
                pnlData.push(data.pnl_update);
                drawPnlChart(pnlData);
            }

            // Markets are now handled by updateMarketsOptimized() with debouncing

            // Update decisions list
            if (data.decisions) {
                const decisionsList = document.getElementById('decisions-list');
                let html = '';

                if (data.decisions.length === 0) {
                    html = '<div style="padding: 15px; text-align: center; color: var(--dim-green);">Waiting for market evaluation...</div>';
                } else {
                    // Limit decisions shown to prevent memory issues
                    const decisionsToShow = data.decisions.slice(0, MAX_DECISION_ENTRIES);
                    for (const d of decisionsToShow) {
                        // Determine colors based on action
                        let actionColor, actionBg, decisionText;
                        if (d.action === 'YES' || d.action === 'TRADE') {
                            actionColor = 'var(--green)';
                            actionBg = 'rgba(0, 255, 65, 0.15)';
                            decisionText = 'ARB: YES';
                        } else if (d.action === 'NO' || d.action === 'SKIP') {
                            actionColor = 'var(--red)';
                            actionBg = 'rgba(255, 0, 64, 0.08)';
                            decisionText = 'ARB: NO';
                        } else if (d.action === 'REJECT') {
                            // FOK order didn't fill - normal, not critical
                            actionColor = 'var(--amber)';
                            actionBg = 'rgba(255, 176, 0, 0.08)';
                            decisionText = 'REJECTED';
                        } else if (d.action === 'PARTIAL') {
                            // CRITICAL: Partial fill - one leg filled, other didn't
                            actionColor = '#ff0000';
                            actionBg = 'rgba(255, 0, 0, 0.25)';
                            decisionText = '⚠ PARTIAL FILL';
                        } else if (d.action === 'DIR_YES') {
                            actionColor = 'var(--cyan)';
                            actionBg = 'rgba(0, 255, 255, 0.15)';
                            decisionText = 'DIR: YES';
                        } else if (d.action === 'DIR_NO') {
                            actionColor = 'var(--amber)';
                            actionBg = 'rgba(255, 176, 0, 0.08)';
                            decisionText = 'DIR: NO';
                        } else {
                            actionColor = 'var(--amber)';
                            actionBg = 'rgba(255, 176, 0, 0.08)';
                            decisionText = d.action;
                        }
                        const spreadColor = d.spread >= 2.0 ? 'var(--green)' :
                                           d.spread >= 0 ? 'var(--amber)' : 'var(--red)';

                        html += `<div style="padding: 8px 12px; border-bottom: 1px solid var(--border); background: ${actionBg}; display: flex; justify-content: space-between; align-items: center;">`;
                        html += `<div style="flex: 1;">`;
                        html += `<span style="color: var(--dim-green); font-size: 0.75rem;">${utcToCst(d.timestamp)}</span> `;
                        html += `<span style="color: var(--cyan); font-weight: bold;">${d.asset}</span> `;
                        html += `<span style="color: ${actionColor}; font-weight: bold; font-size: 1.1rem;">[${decisionText}]</span> `;
                        html += `<span style="color: var(--dim-green);">${d.reason}</span>`;
                        html += `</div>`;
                        html += `<div style="text-align: right; min-width: 200px;">`;
                        html += `<span style="color: var(--dim-green);">Up:</span> <span>${d.up_price ? (d.up_price * 100).toFixed(1) + '¢' : 'N/A'}</span> `;
                        html += `<span style="color: var(--dim-green);">Down:</span> <span>${d.down_price ? (d.down_price * 100).toFixed(1) + '¢' : 'N/A'}</span> `;
                        html += `<span style="color: var(--dim-green);">Spread:</span> <span style="color: ${spreadColor}; font-weight: bold;">${d.spread.toFixed(1)}¢</span>`;
                        html += `</div>`;
                        html += `</div>`;
                    }
                }

                decisionsList.innerHTML = html;
            }
        }

        function updateTime() {
            // Display current time in CST
            const now = new Date();
            const cstTime = now.toLocaleString('en-US', {
                timeZone: 'America/Chicago',
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false
            });
            document.getElementById('current-time').textContent = cstTime + ' CST';
        }
        setInterval(updateTime, 1000);
        updateTime();

        let uptimeStart = null;
        function updateUptime() {
            if (!uptimeStart) return;
            const diff = Math.floor((Date.now() - uptimeStart) / 1000);
            // Only show positive uptime
            if (diff < 0) {
                document.getElementById('uptime').textContent = '00:00:00';
                return;
            }
            const h = Math.floor(diff / 3600).toString().padStart(2, '0');
            const m = Math.floor((diff % 3600) / 60).toString().padStart(2, '0');
            const s = (diff % 60).toString().padStart(2, '0');
            document.getElementById('uptime').textContent = h + ':' + m + ':' + s;
        }
        setInterval(updateUptime, 1000);

        // ========== P&L Chart ==========
        let currentTimeframe = '24h';
        let pnlData = [];

        function drawPnlChart(data) {
            const canvas = document.getElementById('pnl-chart');
            if (!canvas) return;
            const ctx = canvas.getContext('2d');
            const width = canvas.width;
            const height = canvas.height;

            // Clear canvas
            ctx.fillStyle = '#0d1117';
            ctx.fillRect(0, 0, width, height);

            if (!data || !data.length) {
                ctx.fillStyle = '#00aa2a';
                ctx.font = '12px "Share Tech Mono", monospace';
                ctx.textAlign = 'center';
                ctx.fillText('No data', width/2, height/2);
                return;
            }

            // Calculate bounds with padding
            const values = data.map(d => d.cumulative_pnl);
            const minVal = Math.min(0, ...values);
            const maxVal = Math.max(0, ...values);
            const range = maxVal - minVal || 1;
            const padding = range * 0.15;

            // Draw horizontal grid lines
            ctx.strokeStyle = '#003b00';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {
                const y = (height / 4) * i;
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(width, y);
                ctx.stroke();
            }

            // Draw zero line if crosses zero
            if (minVal < 0 && maxVal > 0) {
                const zeroY = height - ((0 - minVal + padding) / (range + 2*padding)) * height;
                ctx.strokeStyle = '#1a3a1a';
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(0, zeroY);
                ctx.lineTo(width, zeroY);
                ctx.stroke();
            }

            // Draw P&L line with glow
            ctx.strokeStyle = '#00ff41';
            ctx.lineWidth = 2;
            ctx.shadowColor = '#00ff41';
            ctx.shadowBlur = 8;
            ctx.beginPath();

            data.forEach((point, i) => {
                const x = data.length === 1 ? width / 2 : (i / (data.length - 1)) * width;
                const y = height - ((point.cumulative_pnl - minVal + padding) / (range + 2*padding)) * height;
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.shadowBlur = 0;

            // Draw current value label
            const lastVal = data[data.length - 1].cumulative_pnl;
            ctx.fillStyle = lastVal >= 0 ? '#00ff41' : '#ff0040';
            ctx.font = 'bold 12px "Share Tech Mono", monospace';
            ctx.textAlign = 'right';
            ctx.fillText((lastVal >= 0 ? '+' : '') + '$' + lastVal.toFixed(2), width - 5, 14);

            // Draw min/max labels
            ctx.fillStyle = '#00aa2a';
            ctx.font = '10px "Share Tech Mono", monospace';
            ctx.textAlign = 'left';
            ctx.fillText('$' + maxVal.toFixed(2), 3, 12);
            ctx.fillText('$' + minVal.toFixed(2), 3, height - 3);
        }

        async function loadPnlData(timeframe) {
            try {
                const resp = await fetch('/dashboard/pnl-history?timeframe=' + timeframe);
                const data = await resp.json();
                pnlData = data.points || [];
                drawPnlChart(pnlData);
            } catch (e) {
                console.error('Failed to load P&L data:', e);
            }
        }

        // Timeframe button handlers
        document.querySelectorAll('.timeframe-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.timeframe-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentTimeframe = btn.dataset.tf;
                loadPnlData(currentTimeframe);
            });
        });

        fetch('/dashboard/state')
            .then(r => r.json())
            .then(data => {
                // Parse uptime_start as a UTC time
                const startStr = data.stats.uptime_start;
                if (startStr) {
                    // Ensure it's parsed as UTC
                    uptimeStart = new Date(startStr.endsWith('Z') ? startStr : startStr + 'Z').getTime();
                }
                updateUptime();
                // Load initial state - trades are included in data, no need to load separately
                updateDashboard(data);
                // Load P&L chart data
                loadPnlData(currentTimeframe);
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
        self._app.router.add_get("/dashboard/pnl-history", self._handle_pnl_history)

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
            "trades": list(trade_history),
            "markets": active_markets,
            "decisions": list(decisions_buffer),
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

    async def _handle_pnl_history(self, request: web.Request) -> web.Response:
        """Get P&L history for charting."""
        timeframe = request.query.get("timeframe", "all")
        if timeframe not in ("24h", "7d", "all"):
            timeframe = "all"

        if _db:
            points = await _db.get_pnl_history(timeframe)
        else:
            points = []

        return web.json_response({"points": points})

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

# Trade ID counter
_trade_id_counter = 0


async def init_persistence(db: "Database") -> None:
    """Initialize persistence and load historical data.

    Args:
        db: Database instance
    """
    global _db, _trade_id_counter
    _db = db

    # Load recent trades from database
    try:
        recent_trades = await db.get_recent_trades(limit=50)
        for trade in reversed(recent_trades):  # oldest first
            trade_dict = {
                "id": trade["id"],
                "time": trade["created_at"].split("T")[1][:8] if "T" in str(trade.get("created_at", "")) else "00:00:00",
                "market_time": trade.get("market_end_time"),
                "asset": trade["asset"],
                "yes_price": trade["yes_price"],
                "no_price": trade["no_price"],
                "yes_cost": trade["yes_cost"],
                "no_cost": trade["no_cost"],
                "spread": trade["spread"],
                "expected_profit": trade["expected_profit"],
                "actual_profit": trade.get("actual_profit"),
                "status": trade.get("status", "pending"),
                "dry_run": trade.get("dry_run", False),
            }
            trade_history.append(trade_dict)

        # Set trade ID counter to continue from last ID
        if recent_trades:
            last_id = recent_trades[0]["id"]  # Most recent
            if last_id.startswith("trade-"):
                _trade_id_counter = int(last_id.split("-")[1])

        # Load all-time stats (primary display)
        all_time = await db.get_all_time_stats()
        if all_time:
            stats["all_time_pnl"] = all_time.get("total_pnl") or 0.0
            stats["all_time_trades"] = all_time.get("total_trades") or 0
            stats["all_time_wins"] = all_time.get("wins") or 0
            stats["all_time_losses"] = all_time.get("losses") or 0
            # Use all-time stats for main display
            stats["daily_pnl"] = stats["all_time_pnl"]
            stats["daily_trades"] = stats["all_time_trades"]
            stats["wins"] = stats["all_time_wins"]
            stats["losses"] = stats["all_time_losses"]
            stats["pending"] = all_time.get("pending") or 0

        # Load today's stats for daily exposure tracking
        today_stats = await db.get_today_stats()
        if today_stats:
            stats["daily_exposure"] = today_stats.get("exposure") or 0.0

        # Load recent logs
        recent_logs = await db.get_recent_logs(limit=100)
        for log_entry in reversed(recent_logs):  # oldest first
            log_buffer.append({
                "timestamp": log_entry["created_at"].split("T")[1][:8] if "T" in str(log_entry.get("created_at", "")) else "00:00:00",
                "level": log_entry["level"],
                "message": log_entry["message"],
                "extra": log_entry.get("extra"),
            })

        import structlog
        log = structlog.get_logger()
        log.info(
            "Loaded historical data from database",
            trades=len(recent_trades),
            logs=len(recent_logs),
        )
    except Exception as e:
        import structlog
        log = structlog.get_logger()
        log.warning("Failed to load historical data", error=str(e))


def add_log(level: str, message: str, persist: bool = True, **extra) -> None:
    """Add a log entry to the buffer.

    Args:
        level: Log level (info, warning, error, trade, resolution)
        message: Log message
        persist: Whether to persist to database (default True)
        **extra: Additional data to include
    """
    entry = {
        "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
        "extra": extra if extra else None,
    }
    log_buffer.append(entry)

    if dashboard:
        asyncio.create_task(dashboard.broadcast({"logs": [entry]}))

    # Persist important logs to database
    if persist and _db and level in ("warning", "error", "trade", "resolution"):
        asyncio.create_task(_db.save_log(level, message, extra if extra else None))


def update_stats(**kwargs) -> None:
    """Update dashboard stats."""
    stats.update(kwargs)

    if dashboard:
        asyncio.create_task(dashboard.broadcast({"stats": stats}))


def update_markets(markets_data: Dict[str, Dict[str, Any]]) -> None:
    """Update active markets display.

    Args:
        markets_data: Dict of condition_id -> market info with:
            - asset: Asset symbol (BTC, ETH)
            - end_time: Market end time string (HH:MM UTC)
            - seconds_remaining: Seconds until market ends
            - up_price: Current UP/YES price
            - down_price: Current DOWN/NO price
            - is_tradeable: Whether market is tradeable
    """
    global active_markets
    active_markets = markets_data

    if dashboard:
        asyncio.create_task(dashboard.broadcast({"markets": active_markets}))


def add_decision(
    asset: str,
    action: str,
    reason: str,
    up_price: Optional[float] = None,
    down_price: Optional[float] = None,
    spread: float = 0.0,
) -> None:
    """Add a strategy decision to the dashboard.

    Args:
        asset: Asset symbol (BTC, ETH)
        action: Decision action (TRADE, SKIP, EVAL, etc.)
        reason: Human-readable reason for the decision
        up_price: Current UP/YES price (0-1)
        down_price: Current DOWN/NO price (0-1)
        spread: Current spread in cents
    """
    decision = {
        "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
        "asset": asset,
        "action": action,
        "reason": reason,
        "up_price": up_price,
        "down_price": down_price,
        "spread": spread,
    }

    decisions_buffer.appendleft(decision)  # Newest first

    if dashboard:
        asyncio.create_task(dashboard.broadcast({"decisions": list(decisions_buffer)}))


def add_trade(
    asset: str,
    yes_price: float,
    no_price: float,
    yes_cost: float,
    no_cost: float,
    spread: float,
    expected_profit: float,
    market_end_time: str = None,
    market_slug: str = None,
    condition_id: str = None,
    dry_run: bool = False,
) -> str:
    """Add a new trade to history.

    Args:
        asset: Asset symbol (BTC, ETH)
        yes_price: Price paid for YES
        no_price: Price paid for NO
        yes_cost: Cost of YES position
        no_cost: Cost of NO position
        spread: Spread in cents
        expected_profit: Expected profit
        market_end_time: Market resolution time
        market_slug: Market slug for reference
        condition_id: Market condition ID
        dry_run: Whether this is a dry run trade

    Returns:
        Trade ID for later updates
    """
    global _trade_id_counter
    _trade_id_counter += 1
    trade_id = f"trade-{_trade_id_counter}"

    trade = {
        "id": trade_id,
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "market_time": market_end_time,
        "asset": asset,
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_cost": yes_cost,
        "no_cost": no_cost,
        "spread": spread,
        "expected_profit": expected_profit,
        "actual_profit": None,
        "status": "pending",
        "dry_run": dry_run,
        "market_slug": market_slug,
    }

    trade_history.append(trade)
    stats["pending"] = stats.get("pending", 0) + 1
    stats["daily_trades"] = stats.get("daily_trades", 0) + 1
    stats["daily_exposure"] = stats.get("daily_exposure", 0.0) + yes_cost + no_cost

    if dashboard:
        asyncio.create_task(dashboard.broadcast({
            "trades": [trade],
            "stats": stats,
        }))

    # Persist to database
    if _db:
        asyncio.create_task(_db.save_trade(
            trade_id=trade_id,
            asset=asset,
            yes_price=yes_price,
            no_price=no_price,
            yes_cost=yes_cost,
            no_cost=no_cost,
            spread=spread,
            expected_profit=expected_profit,
            market_end_time=market_end_time,
            market_slug=market_slug,
            condition_id=condition_id,
            dry_run=dry_run,
        ))
        # Update daily stats
        asyncio.create_task(_db.update_daily_stats(
            trades_delta=1,
            exposure_delta=yes_cost + no_cost,
        ))

    # Log the trade
    add_log(
        "trade",
        f"Trade opened: {asset} spread={spread:.1f}¢ exp_profit=${expected_profit:.2f}",
        trade_id=trade_id,
        dry_run=dry_run,
    )

    return trade_id


def resolve_trade(trade_id: str, won: bool, actual_profit: float) -> None:
    """Update a trade with its resolution result.

    Args:
        trade_id: The trade ID returned from add_trade
        won: Whether the trade was profitable
        actual_profit: Actual profit/loss amount
    """
    for trade in trade_history:
        if trade["id"] == trade_id:
            trade["status"] = "win" if won else "loss"
            trade["actual_profit"] = actual_profit
            break

    # Update win/loss/pending counts
    if won:
        stats["wins"] = stats.get("wins", 0) + 1
    else:
        stats["losses"] = stats.get("losses", 0) + 1
    stats["pending"] = max(0, stats.get("pending", 0) - 1)
    stats["daily_pnl"] = stats.get("daily_pnl", 0.0) + actual_profit

    if dashboard:
        # Calculate cumulative P&L for chart update
        cumulative_pnl = stats.get("all_time_pnl", 0.0) + actual_profit
        stats["all_time_pnl"] = cumulative_pnl

        asyncio.create_task(dashboard.broadcast({
            "trade_update": {
                "id": trade_id,
                "status": "win" if won else "loss",
                "actual_profit": actual_profit,
            },
            "pnl_update": {
                "timestamp": datetime.utcnow().isoformat(),
                "cumulative_pnl": round(cumulative_pnl, 2),
            },
            "stats": stats,
        }))

    # Persist to database
    if _db:
        asyncio.create_task(_db.resolve_trade(trade_id, won, actual_profit))
        # Update daily stats
        asyncio.create_task(_db.update_daily_stats(
            pnl_delta=actual_profit,
            wins_delta=1 if won else 0,
            losses_delta=0 if won else 1,
        ))

    # Log resolution
    status_str = "WIN" if won else "LOSS"
    add_log(
        "resolution",
        f"Market resolved: {status_str} ${abs(actual_profit):.2f}",
        trade_id=trade_id,
    )
