"""Retro terminal-styled web dashboard for Polymarket bot."""

import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Any, List, Optional, TYPE_CHECKING

import structlog
from aiohttp import web

from .events import trade_events, EventTypes

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
    <title>TURN 1 SOL RING // POLYMARKET BOT</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=VT323&family=Share+Tech+Mono&display=swap');

        :root {
            --green: #ffffff;
            --dim-green: #888888;
            --dark-green: #333333;
            --amber: #cccccc;
            --red: #ff0040;
            --cyan: #aaaaaa;
            --bg: #000000;
            --panel-bg: #0a0a0a;
            --border: #333333;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            background: var(--bg);
            color: var(--green);
            font-family: 'Share Tech Mono', 'Courier New', monospace;
            min-height: 100vh;
            overflow-x: hidden;
        }

        /* Dithered noise texture overlay - visible grain pattern */
        body::before {
            content: "";
            position: fixed;
            top: 0; left: 0;
            width: 100%; height: 100%;
            pointer-events: none;
            background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 64 64' xmlns='http://www.w3.org/2000/svg'%3E%3Crect x='2' y='3' width='1' height='1' fill='%23555'/%3E%3Crect x='7' y='1' width='1' height='1' fill='%23444'/%3E%3Crect x='13' y='5' width='1' height='1' fill='%23666'/%3E%3Crect x='19' y='2' width='1' height='1' fill='%23555'/%3E%3Crect x='26' y='4' width='1' height='1' fill='%23444'/%3E%3Crect x='31' y='1' width='1' height='1' fill='%23666'/%3E%3Crect x='38' y='6' width='1' height='1' fill='%23555'/%3E%3Crect x='44' y='3' width='1' height='1' fill='%23444'/%3E%3Crect x='51' y='5' width='1' height='1' fill='%23666'/%3E%3Crect x='58' y='2' width='1' height='1' fill='%23555'/%3E%3Crect x='4' y='9' width='1' height='1' fill='%23666'/%3E%3Crect x='10' y='11' width='1' height='1' fill='%23555'/%3E%3Crect x='16' y='8' width='1' height='1' fill='%23444'/%3E%3Crect x='22' y='12' width='1' height='1' fill='%23666'/%3E%3Crect x='29' y='10' width='1' height='1' fill='%23555'/%3E%3Crect x='35' y='7' width='1' height='1' fill='%23444'/%3E%3Crect x='41' y='11' width='1' height='1' fill='%23666'/%3E%3Crect x='48' y='9' width='1' height='1' fill='%23555'/%3E%3Crect x='54' y='12' width='1' height='1' fill='%23444'/%3E%3Crect x='61' y='8' width='1' height='1' fill='%23666'/%3E%3Crect x='1' y='15' width='1' height='1' fill='%23555'/%3E%3Crect x='8' y='18' width='1' height='1' fill='%23444'/%3E%3Crect x='14' y='14' width='1' height='1' fill='%23666'/%3E%3Crect x='20' y='17' width='1' height='1' fill='%23555'/%3E%3Crect x='27' y='16' width='1' height='1' fill='%23444'/%3E%3Crect x='33' y='19' width='1' height='1' fill='%23666'/%3E%3Crect x='39' y='14' width='1' height='1' fill='%23555'/%3E%3Crect x='46' y='18' width='1' height='1' fill='%23444'/%3E%3Crect x='52' y='15' width='1' height='1' fill='%23666'/%3E%3Crect x='59' y='17' width='1' height='1' fill='%23555'/%3E%3Crect x='5' y='22' width='1' height='1' fill='%23444'/%3E%3Crect x='11' y='24' width='1' height='1' fill='%23666'/%3E%3Crect x='17' y='21' width='1' height='1' fill='%23555'/%3E%3Crect x='24' y='25' width='1' height='1' fill='%23444'/%3E%3Crect x='30' y='23' width='1' height='1' fill='%23666'/%3E%3Crect x='36' y='20' width='1' height='1' fill='%23555'/%3E%3Crect x='43' y='24' width='1' height='1' fill='%23444'/%3E%3Crect x='49' y='22' width='1' height='1' fill='%23666'/%3E%3Crect x='56' y='25' width='1' height='1' fill='%23555'/%3E%3Crect x='62' y='21' width='1' height='1' fill='%23444'/%3E%3Crect x='3' y='28' width='1' height='1' fill='%23666'/%3E%3Crect x='9' y='31' width='1' height='1' fill='%23555'/%3E%3Crect x='15' y='27' width='1' height='1' fill='%23444'/%3E%3Crect x='21' y='30' width='1' height='1' fill='%23666'/%3E%3Crect x='28' y='29' width='1' height='1' fill='%23555'/%3E%3Crect x='34' y='32' width='1' height='1' fill='%23444'/%3E%3Crect x='40' y='27' width='1' height='1' fill='%23666'/%3E%3Crect x='47' y='31' width='1' height='1' fill='%23555'/%3E%3Crect x='53' y='28' width='1' height='1' fill='%23444'/%3E%3Crect x='60' y='30' width='1' height='1' fill='%23666'/%3E%3Crect x='6' y='35' width='1' height='1' fill='%23555'/%3E%3Crect x='12' y='37' width='1' height='1' fill='%23444'/%3E%3Crect x='18' y='34' width='1' height='1' fill='%23666'/%3E%3Crect x='25' y='38' width='1' height='1' fill='%23555'/%3E%3Crect x='31' y='36' width='1' height='1' fill='%23444'/%3E%3Crect x='37' y='33' width='1' height='1' fill='%23666'/%3E%3Crect x='44' y='37' width='1' height='1' fill='%23555'/%3E%3Crect x='50' y='35' width='1' height='1' fill='%23444'/%3E%3Crect x='57' y='38' width='1' height='1' fill='%23666'/%3E%3Crect x='63' y='34' width='1' height='1' fill='%23555'/%3E%3Crect x='2' y='41' width='1' height='1' fill='%23444'/%3E%3Crect x='8' y='44' width='1' height='1' fill='%23666'/%3E%3Crect x='14' y='40' width='1' height='1' fill='%23555'/%3E%3Crect x='20' y='43' width='1' height='1' fill='%23444'/%3E%3Crect x='27' y='42' width='1' height='1' fill='%23666'/%3E%3Crect x='33' y='45' width='1' height='1' fill='%23555'/%3E%3Crect x='39' y='40' width='1' height='1' fill='%23444'/%3E%3Crect x='46' y='44' width='1' height='1' fill='%23666'/%3E%3Crect x='52' y='41' width='1' height='1' fill='%23555'/%3E%3Crect x='59' y='43' width='1' height='1' fill='%23444'/%3E%3Crect x='4' y='48' width='1' height='1' fill='%23666'/%3E%3Crect x='10' y='50' width='1' height='1' fill='%23555'/%3E%3Crect x='16' y='47' width='1' height='1' fill='%23444'/%3E%3Crect x='23' y='51' width='1' height='1' fill='%23666'/%3E%3Crect x='29' y='49' width='1' height='1' fill='%23555'/%3E%3Crect x='35' y='46' width='1' height='1' fill='%23444'/%3E%3Crect x='42' y='50' width='1' height='1' fill='%23666'/%3E%3Crect x='48' y='48' width='1' height='1' fill='%23555'/%3E%3Crect x='55' y='51' width='1' height='1' fill='%23444'/%3E%3Crect x='61' y='47' width='1' height='1' fill='%23666'/%3E%3Crect x='1' y='54' width='1' height='1' fill='%23555'/%3E%3Crect x='7' y='57' width='1' height='1' fill='%23444'/%3E%3Crect x='13' y='53' width='1' height='1' fill='%23666'/%3E%3Crect x='19' y='56' width='1' height='1' fill='%23555'/%3E%3Crect x='26' y='55' width='1' height='1' fill='%23444'/%3E%3Crect x='32' y='58' width='1' height='1' fill='%23666'/%3E%3Crect x='38' y='53' width='1' height='1' fill='%23555'/%3E%3Crect x='45' y='57' width='1' height='1' fill='%23444'/%3E%3Crect x='51' y='54' width='1' height='1' fill='%23666'/%3E%3Crect x='58' y='56' width='1' height='1' fill='%23555'/%3E%3Crect x='5' y='61' width='1' height='1' fill='%23444'/%3E%3Crect x='11' y='63' width='1' height='1' fill='%23666'/%3E%3Crect x='17' y='60' width='1' height='1' fill='%23555'/%3E%3Crect x='24' y='62' width='1' height='1' fill='%23444'/%3E%3Crect x='30' y='59' width='1' height='1' fill='%23666'/%3E%3Crect x='36' y='62' width='1' height='1' fill='%23555'/%3E%3Crect x='43' y='60' width='1' height='1' fill='%23444'/%3E%3Crect x='49' y='63' width='1' height='1' fill='%23666'/%3E%3Crect x='56' y='61' width='1' height='1' fill='%23555'/%3E%3Crect x='62' y='59' width='1' height='1' fill='%23444'/%3E%3C/svg%3E");
            background-size: 64px 64px;
            opacity: 0.6;
            z-index: 1000;
        }

        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 20px;
        }

        .header {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 30px;
            padding: 20px 0;
            border-bottom: 1px solid var(--border);
            margin-bottom: 20px;
        }

        .ring-art {
            font-family: 'Courier New', monospace;
            font-size: 11px;
            line-height: 1.15;
            white-space: pre;
            color: var(--green);
            text-shadow: 0 0 4px var(--green);
        }

        .header-text {
            text-align: center;
        }

        .header h1 {
            font-family: 'VT323', monospace;
            font-size: 3rem;
            letter-spacing: 8px;
            text-shadow: 0 0 5px var(--green);
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

        /* Log terminal - newest messages at top, no scrolling needed */
        .log-terminal {
            height: 300px;
            overflow-y: auto;
            font-size: 0.8rem;
            line-height: 1.6;
            padding: 10px;
            background: #000;
            border: 1px solid var(--border);
            display: flex;
            flex-direction: column;
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

        .circuit-breaker-banner {
            background: linear-gradient(90deg, #dc3545, transparent);
            color: #fff;
            text-align: center;
            padding: 8px;
            font-weight: bold;
            letter-spacing: 2px;
            animation: blink 1s infinite;
        }

        @media (max-width: 1200px) {
            .grid { grid-template-columns: repeat(2, 1fr); }
            .grid-2col { grid-template-columns: 1fr; }
        }

        /* Mobile responsiveness */
        @media (max-width: 768px) {
            .container { padding: 10px; }
            .header h1 { font-size: 2rem; letter-spacing: 4px; }
            .header .subtitle { font-size: 0.75rem; letter-spacing: 2px; }
            .status-bar {
                flex-wrap: wrap;
                gap: 10px 20px;
                font-size: 0.75rem;
            }
            .status-item {
                white-space: nowrap;
            }
            .grid { grid-template-columns: 1fr 1fr; gap: 10px; }
            .panel-title { font-size: 0.8rem; letter-spacing: 1px; }
            .stat-value { font-size: 1.5rem !important; }
            .winloss { gap: 15px; }
            .winloss-value { font-size: 1.2rem; }
            /* Hide less important status items on mobile */
            .status-item:nth-child(n+5) { display: none; }
            /* Make markets table scroll horizontally if needed */
            #markets-list { overflow-x: auto; }
            #markets-list table { min-width: 500px; }
        }

        @media (max-width: 480px) {
            .header h1 { font-size: 1.5rem; }
            .grid { grid-template-columns: 1fr; }
            .trade-item {
                grid-template-columns: 1fr;
                gap: 8px;
            }
            .trade-time { font-size: 0.7rem; }
            .trade-profit { text-align: left; }
        }
    </style>
</head>
<body>
    <div id="dry-run-banner" class="dry-run-banner" style="display: none;">
        [ DRY RUN MODE - NO REAL TRADES ]
    </div>
    <div id="circuit-breaker-banner" class="circuit-breaker-banner" style="display: none;">
        [ ⚠️ CIRCUIT BREAKER ACTIVE - DAILY LOSS LIMIT REACHED ]
    </div>

    <div class="container">
        <header class="header">
            <div class="ring-art">     ▄▄████▄▄
   ▄██▀▀▀▀▀▀██▄
  ██▀  ▄██▄  ▀██
 ██   ██▀▀██   ██
 ██   ██  ██   ██
 ██   ██▄▄██   ██
  ██▄  ▀██▀  ▄██
   ▀██▄▄▄▄▄▄██▀
     ▀▀████▀▀</div>
            <div class="header-text">
                <h1>TURN 1 SOL RING</h1>
                <div class="subtitle">POLYMARKET ARBITRAGE BOT v0.1.0</div>
            </div>
            <div class="ring-art">     ▄▄████▄▄
   ▄██▀▀▀▀▀▀██▄
  ██▀  ▄██▄  ▀██
 ██   ██▀▀██   ██
 ██   ██  ██   ██
 ██   ██▄▄██   ██
  ██▄  ▀██▀  ▄██
   ▀██▄▄▄▄▄▄██▀
     ▀▀████▀▀</div>
        </header>
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
                <div id="markets-list" style="max-height: 430px; overflow-y: auto;">
                    <div style="padding: 20px; text-align: center; color: var(--dim-green);">
                        Searching for markets...
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
            TURN 1 SOL RING // POLYMARKET CLOB //
            <span id="current-time"></span>
        </footer>
    </div>

    <script>
        // SSE connection for real-time updates
        const evtSource = new EventSource('/dashboard/events');

        // Debounce market updates to at most once per 500ms (prices only, time is client-side)
        let pendingMarketData = null;
        let lastMarketUpdate = 0;
        let marketUpdateTimer = null;

        evtSource.onmessage = function(event) {
            const data = JSON.parse(event.data);

            // Debounce market updates - only update prices every 500ms
            // Time remaining is handled by client-side interval, so no need for frequent server updates
            if (data.markets) {
                pendingMarketData = data.markets;
                const now = Date.now();

                // Only process update if 500ms has passed since last update
                if (now - lastMarketUpdate >= 500) {
                    lastMarketUpdate = now;
                    updateMarketsOptimized(pendingMarketData);
                } else if (!marketUpdateTimer) {
                    // Schedule update for when 500ms has passed
                    const delay = 500 - (now - lastMarketUpdate);
                    marketUpdateTimer = setTimeout(() => {
                        lastMarketUpdate = Date.now();
                        updateMarketsOptimized(pendingMarketData);
                        marketUpdateTimer = null;
                    }, delay);
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
        // Store market end times for client-side countdown (ms timestamp)
        const marketEndTimes = new Map();

        // Parse "HH:MM UTC" format to milliseconds timestamp (today's date)
        function parseEndTimeToMs(endTimeStr) {
            if (!endTimeStr || !/^\d{2}:\d{2} UTC$/.test(endTimeStr)) return null;
            const [timepart] = endTimeStr.split(' ');
            const [h, m] = timepart.split(':').map(Number);
            const now = new Date();
            // Create date in UTC
            const endDate = new Date(Date.UTC(
                now.getUTCFullYear(),
                now.getUTCMonth(),
                now.getUTCDate(),
                h, m, 0, 0
            ));
            // If the time has already passed today, it might be for the next slot
            // But for 15-min markets, they should be within a reasonable window
            return endDate.getTime();
        }

        // Calculate seconds remaining from end time
        function calculateSecondsRemaining(endTimeMs) {
            if (!endTimeMs) return 0;
            const now = Date.now();
            return Math.max(0, Math.floor((endTimeMs - now) / 1000));
        }

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

        function trimChildren(element, maxCount) {
            while (element.children.length > maxCount) {
                element.removeChild(element.lastChild);
            }
        }

        // Optimized market update - only updates prices/status from server
        // Time remaining is calculated client-side for smooth countdown
        function updateMarketsOptimized(markets) {
            const marketsList = document.getElementById('markets-list');
            const marketIds = new Set(Object.keys(markets));

            // Handle empty state
            if (marketIds.size === 0) {
                marketsList.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--dim-green);">No markets found. Waiting for next 15-minute window...</div>';
                marketRowCache.clear();
                marketEndTimes.clear();
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
                    <th style="padding: 8px; text-align: right;">Up</th>
                    <th style="padding: 8px; text-align: right;">Down</th>
                    <th style="padding: 8px; text-align: right;">Spread</th>
                    <th style="padding: 8px; text-align: center;">ARB</th>
                `;
                table.appendChild(header);
                marketsList.appendChild(table);
            }

            // Remove rows for markets no longer in the data
            for (const [id, row] of marketRowCache.entries()) {
                if (!marketIds.has(id)) {
                    row.remove();
                    marketRowCache.delete(id);
                    marketEndTimes.delete(id);
                }
            }

            // Store/update end times for each market
            for (const [id, m] of Object.entries(markets)) {
                if (m.end_time && !marketEndTimes.has(id)) {
                    const endMs = parseEndTimeToMs(m.end_time);
                    if (endMs) marketEndTimes.set(id, endMs);
                }
            }

            // Sort markets by minutes remaining (using client-side calculation)
            const sortedMarkets = Object.entries(markets).sort((a, b) => {
                const aEndMs = marketEndTimes.get(a[0]);
                const bEndMs = marketEndTimes.get(b[0]);
                const aSeconds = aEndMs ? calculateSecondsRemaining(aEndMs) : 0;
                const bSeconds = bEndMs ? calculateSecondsRemaining(bEndMs) : 0;
                const aMinutes = Math.floor(aSeconds / 60);
                const bMinutes = Math.floor(bSeconds / 60);
                if (aMinutes !== bMinutes) return aMinutes - bMinutes;
                return (a[1].asset || '').localeCompare(b[1].asset || '');
            });

            let foundCount = 0;
            let tradeableCount = 0;

            // Update or create rows for each market (but don't reorder existing rows)
            for (const [id, m] of sortedMarkets) {
                foundCount++;

                // Calculate time remaining client-side from stored end time
                const endMs = marketEndTimes.get(id);
                const secondsRemaining = endMs ? calculateSecondsRemaining(endMs) : 0;
                const isTradeable = secondsRemaining > 60;
                if (isTradeable) tradeableCount++;

                const mins = Math.floor(secondsRemaining / 60);
                const secs = Math.floor(secondsRemaining % 60);
                const timeLeft = secondsRemaining > 0 ? `${mins}m ${secs}s` : 'ENDED';

                // Calculate spread and ARB eligibility
                const spread = (m.up_price && m.down_price)
                    ? ((1 - m.up_price - m.down_price) * 100).toFixed(1)
                    : null;
                const spreadNum = spread ? parseFloat(spread) : 0;
                const meetsArbCriteria = isTradeable && spreadNum >= 2.0;
                const spreadColor = spreadNum >= 2.0 ? 'var(--green)' : spreadNum > 0 ? 'var(--amber)' : 'var(--red)';

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
                        <td style="padding: 8px; text-align: right;" class="cell-spread">${spread ? spread + '¢' : 'N/A'}</td>
                        <td style="padding: 8px; text-align: center;" class="cell-arb">${meetsArbCriteria ? '✓' : '—'}</td>
                    `;
                    // Append new rows to table (will be sorted on next full refresh)
                    table.appendChild(row);
                    marketRowCache.set(id, row);
                } else {
                    // Update prices, spread, and ARB status - only if changed
                    const upCell = row.querySelector('.cell-upprice');
                    const downCell = row.querySelector('.cell-downprice');
                    const spreadCell = row.querySelector('.cell-spread');
                    const arbCell = row.querySelector('.cell-arb');

                    const upText = m.up_price ? (m.up_price * 100).toFixed(1) + '¢' : 'N/A';
                    const downText = m.down_price ? (m.down_price * 100).toFixed(1) + '¢' : 'N/A';
                    const spreadText = spread ? spread + '¢' : 'N/A';
                    const arbText = meetsArbCriteria ? '✓' : '—';
                    const arbColor = meetsArbCriteria ? 'var(--green)' : 'var(--dim-green)';
                    const arbWeight = meetsArbCriteria ? 'bold' : 'normal';

                    if (upCell && upCell.textContent !== upText) upCell.textContent = upText;
                    if (downCell && downCell.textContent !== downText) downCell.textContent = downText;
                    if (spreadCell) {
                        if (spreadCell.textContent !== spreadText) spreadCell.textContent = spreadText;
                        if (spreadCell.style.color !== spreadColor) spreadCell.style.color = spreadColor;
                    }
                    if (arbCell) {
                        if (arbCell.textContent !== arbText) arbCell.textContent = arbText;
                        if (arbCell.style.color !== arbColor) arbCell.style.color = arbColor;
                        if (arbCell.style.fontWeight !== arbWeight) arbCell.style.fontWeight = arbWeight;
                    }
                }

                // Update row background only if changed
                const bgColor = isTradeable ? 'rgba(0, 255, 65, 0.05)' : 'rgba(255, 0, 64, 0.05)';
                if (row.style.background !== bgColor) row.style.background = bgColor;
            }

            document.getElementById('market-count').textContent = foundCount;
            document.getElementById('tradeable-count').textContent = tradeableCount;
        }

        // Client-side timer to update time remaining every second (independent of server updates)
        function updateMarketTimers() {
            let tradeableCount = 0;

            for (const [id, row] of marketRowCache.entries()) {
                const endMs = marketEndTimes.get(id);
                if (!endMs) continue;

                const secondsRemaining = calculateSecondsRemaining(endMs);
                const isTradeable = secondsRemaining > 60;
                if (isTradeable) tradeableCount++;

                const mins = Math.floor(secondsRemaining / 60);
                const secs = Math.floor(secondsRemaining % 60);
                const timeLeft = secondsRemaining > 0 ? `${mins}m ${secs}s` : 'ENDED';
                const timeColor = secondsRemaining > 60 ? 'var(--green)' : 'var(--red)';

                const timeCell = row.querySelector('.cell-timeleft');

                if (timeCell) {
                    if (timeCell.textContent !== timeLeft) timeCell.textContent = timeLeft;
                    if (timeCell.style.color !== timeColor) timeCell.style.color = timeColor;
                }

                // Update row background only if changed
                const bgColor = isTradeable ? 'rgba(0, 255, 65, 0.05)' : 'rgba(255, 0, 64, 0.05)';
                if (row.style.background !== bgColor) row.style.background = bgColor;
            }

            const countEl = document.getElementById('tradeable-count');
            if (countEl.textContent !== String(tradeableCount)) countEl.textContent = tradeableCount;
        }

        // Update market timers every second (client-side countdown)
        setInterval(updateMarketTimers, 1000);

        function updateDashboard(data) {
            if (data.stats) {
                const s = data.stats;

                // Helper to update text only if changed
                function updateText(id, value) {
                    const el = document.getElementById(id);
                    if (el && el.textContent !== value) el.textContent = value;
                }

                // Helper to update class only if changed
                function updateClass(el, newClass) {
                    if (el && el.className !== newClass) el.className = newClass;
                }

                const pnlEl = document.getElementById('daily-pnl');
                const pnlText = '$' + s.daily_pnl.toFixed(2);
                const pnlClass = 'stat-value ' + (s.daily_pnl >= 0 ? 'positive' : 'negative');
                if (pnlEl.textContent !== pnlText) pnlEl.textContent = pnlText;
                updateClass(pnlEl, pnlClass);

                updateText('daily-exposure', '$' + s.daily_exposure.toFixed(2));
                updateText('wins', String(s.wins || 0));
                updateText('losses', String(s.losses || 0));
                updateText('pending', String(s.pending || 0));

                const wsEl = document.getElementById('ws-status');
                updateClass(wsEl, 'status-dot ' + (s.websocket === 'CONNECTED' ? '' : 'error'));

                // Update wallet balance
                if (s.wallet_balance !== undefined) {
                    updateText('wallet-balance', s.wallet_balance.toFixed(2));
                }

                // Update strategy status indicators
                updateClass(document.getElementById('arb-status'), 'status-dot ' + (s.arbitrage_enabled ? '' : 'error'));
                updateClass(document.getElementById('dir-status'), 'status-dot ' + (s.directional_enabled ? '' : 'error'));
                updateClass(document.getElementById('nr-status'), 'status-dot ' + (s.near_resolution_enabled ? '' : 'error'));

                // Show appropriate banner based on trading mode
                const dryRunBanner = document.getElementById('dry-run-banner');
                const cbBanner = document.getElementById('circuit-breaker-banner');

                if (s.circuit_breaker_hit) {
                    // Circuit breaker takes priority
                    if (cbBanner.style.display !== 'block') cbBanner.style.display = 'block';
                    if (dryRunBanner.style.display !== 'none') dryRunBanner.style.display = 'none';
                } else if (s.dry_run) {
                    // Dry run mode
                    if (dryRunBanner.style.display !== 'block') dryRunBanner.style.display = 'block';
                    if (cbBanner.style.display !== 'none') cbBanner.style.display = 'none';
                } else {
                    // Live trading
                    if (dryRunBanner.style.display !== 'none') dryRunBanner.style.display = 'none';
                    if (cbBanner.style.display !== 'none') cbBanner.style.display = 'none';
                }

                // Update realized PnL if available
                if (s.realized_pnl !== undefined) {
                    const pnlEl = document.getElementById('daily-pnl');
                    if (pnlEl) {
                        pnlEl.textContent = (s.realized_pnl >= 0 ? '+' : '') + '$' + s.realized_pnl.toFixed(2);
                        pnlEl.className = s.realized_pnl >= 0 ? 'positive' : 'negative';
                    }
                }
            }

            if (data.logs) {
                const terminal = document.getElementById('log-terminal');
                // Prepend new logs at the top (newest first)
                data.logs.forEach(log => {
                    const line = document.createElement('div');
                    line.className = 'log-line';

                    const levelClass = (log.level || 'info').toLowerCase();
                    const extra = log.extra ? ' <span class="log-extra">' + JSON.stringify(log.extra) + '</span>' : '';

                    line.innerHTML =
                        '<span class="log-time">' + utcToCst(log.timestamp) + '</span>' +
                        '<span class="log-level ' + levelClass + '">[' + (log.level || 'INFO').toUpperCase() + ']</span>' +
                        '<span class="log-msg">' + log.message + '</span>' + extra;

                    // Append at bottom so newest is at bottom
                    terminal.appendChild(line);
                });

                // Trim old entries from top to prevent memory issues
                while (terminal.children.length > MAX_LOG_ENTRIES) {
                    terminal.removeChild(terminal.firstChild);
                }

                // Auto-scroll to bottom to show newest
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
            // Decisions removed - ARB status now shown in markets table
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
                // Handle markets separately with optimized function to prevent flicker
                if (data.markets) {
                    updateMarketsOptimized(data.markets);
                    delete data.markets;
                }
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
        """SSE endpoint for real-time updates.

        Sends periodic updates to ensure clients stay in sync even when
        WebSocket real-time data isn't flowing.
        """
        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"
        await response.prepare(request)

        self._clients.append(response)
        tick_count = 0
        try:
            while True:
                await asyncio.sleep(1)
                if response.task.done():
                    break

                tick_count += 1
                # Every 5 seconds, push current market data to this client
                # This ensures UI stays updated even if WebSocket isn't providing real-time data
                if tick_count >= 5 and active_markets:
                    tick_count = 0
                    try:
                        message = f"data: {json.dumps({'markets': active_markets, 'stats': stats})}\n\n"
                        await response.write(message.encode())
                    except Exception:
                        break
        finally:
            if response in self._clients:
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


async def _on_trade_event(event_type: str, data: Dict[str, Any]) -> None:
    """Handle trade events from strategy.

    Phase 6: Dashboard subscribes to events instead of being called directly.
    This keeps dashboard as a pure display layer.

    Args:
        event_type: Type of event (trade_created, trade_resolved, etc.)
        data: Event data payload
    """
    if event_type == EventTypes.TRADE_CREATED:
        # Update in-memory state for display
        trade = {
            "id": data.get("trade_id"),
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "market_time": data.get("market_end_time"),
            "asset": data.get("asset"),
            "yes_price": data.get("yes_price"),
            "no_price": data.get("no_price"),
            "yes_cost": data.get("yes_cost"),
            "no_cost": data.get("no_cost"),
            "spread": data.get("spread"),
            "expected_profit": data.get("expected_profit"),
            "actual_profit": None,
            "status": "pending",
            "dry_run": data.get("dry_run", False),
            # Phase 2 fields
            "hedge_ratio": data.get("hedge_ratio"),
            "execution_status": data.get("execution_status"),
        }
        trade_history.append(trade)

        # Update stats
        stats["daily_trades"] = stats.get("daily_trades", 0) + 1
        stats["pending"] = stats.get("pending", 0) + 1
        stats["daily_exposure"] = stats.get("daily_exposure", 0.0) + data.get("yes_cost", 0) + data.get("no_cost", 0)
        stats["last_trade"] = datetime.utcnow().isoformat()

        # Broadcast to SSE clients
        if dashboard:
            await dashboard.broadcast({
                "trades": [trade],
                "stats": stats,
            })

        log.debug(
            "Dashboard received trade event",
            trade_id=data.get("trade_id"),
            asset=data.get("asset"),
        )

    elif event_type == EventTypes.TRADE_RESOLVED:
        # Update trade status
        trade_id = data.get("trade_id")
        won = data.get("won", False)
        actual_profit = data.get("actual_profit", 0.0)

        for trade in trade_history:
            if trade["id"] == trade_id:
                trade["status"] = "win" if won else "loss"
                trade["actual_profit"] = actual_profit
                break

        # Update stats
        if won:
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1
        stats["pending"] = max(0, stats.get("pending", 0) - 1)
        stats["daily_pnl"] = stats.get("daily_pnl", 0.0) + actual_profit

        # Broadcast to SSE clients
        if dashboard:
            await dashboard.broadcast({
                "trade_update": {
                    "trade_id": trade_id,
                    "status": "win" if won else "loss",
                    "actual_profit": actual_profit,
                },
                "stats": stats,
            })

    elif event_type == EventTypes.STATS_UPDATED:
        # Direct stats update
        for key, value in data.items():
            if key in stats:
                stats[key] = value

        if dashboard:
            await dashboard.broadcast({"stats": stats})


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

    # Phase 6: Subscribe to trade events from strategy
    trade_events.subscribe(_on_trade_event)
    log.info("Dashboard subscribed to trade events")


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
    trading_mode: str = None,
) -> str:
    """Add a new trade to dashboard display.

    NOTE: Phase 2 Architecture Change (2025-12-14)
    This function now ONLY manages in-memory display state.
    Database persistence is handled by strategy via _record_trade().
    Dashboard is READ-ONLY for trade data.

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
        condition_id: Market condition ID (unused - kept for backward compat)
        dry_run: Whether this is a dry run trade
        trading_mode: Trading mode ('LIVE', 'DRY_RUN', or 'CIRCUIT_BREAKER')

    Returns:
        Trade ID for later updates
    """
    global _trade_id_counter
    _trade_id_counter += 1
    trade_id = f"trade-{_trade_id_counter}"

    # Determine trading mode if not provided
    if trading_mode is None:
        trading_mode = "DRY_RUN" if dry_run else "LIVE"

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
        "trading_mode": trading_mode,
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

    # NOTE: Database persistence is now handled by strategy via _record_trade()
    # Dashboard is READ-ONLY - do NOT save to DB here
    # The _db.save_trade() and _db.update_daily_stats() calls have been removed
    # as part of Phase 2 architecture (strategy owns persistence)

    # Log the trade (still done here for immediate display)
    add_log(
        "trade",
        f"Trade opened: {asset} spread={spread:.1f}¢ exp_profit=${expected_profit:.2f}",
        trade_id=trade_id,
        dry_run=dry_run,
    )

    return trade_id


def resolve_trade(trade_id: str, won: bool, actual_profit: float) -> None:
    """Update a trade's display state with resolution result.

    NOTE: Phase 2 Architecture Change (2025-12-14)
    This function now ONLY manages in-memory display state.
    Database persistence should be handled by strategy.
    Dashboard is READ-ONLY for trade data.

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

    # Update win/loss/pending counts (in-memory display state only)
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

    # Phase 6 (2025-12-14): Dashboard is READ-ONLY
    # Database persistence is handled by strategy via events.
    # The backward-compat _db.resolve_trade() call has been removed.
    # Strategy should emit EventTypes.TRADE_RESOLVED for resolution updates.

    # Log resolution
    status_str = "WIN" if won else "LOSS"
    add_log(
        "resolution",
        f"Market resolved: {status_str} ${abs(actual_profit):.2f}",
        trade_id=trade_id,
    )
