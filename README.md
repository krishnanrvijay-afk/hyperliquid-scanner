# Hyperliquid Scanner

Crypto futures market scanner for Hyperliquid perpetuals.

This is a **separate project** from the MEXC scanner (`mexc-scanner-krishnan`).
It uses the [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
and targets the Hyperliquid testnet by default.

## Environment Variables

| Variable | Description |
|---|---|
| `HL_PRIVATE_KEY` | Ethereum private key for signing orders (hex, with or without 0x prefix) |
| `HL_ADDRESS` | Wallet address (derived from private key if not set) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for alerts |
| `CHAT_ID` | Telegram chat ID for alert delivery |
| `HL_DRY_RUN` | Set to `false` to enable live trading (default: `true`) |
| `HL_USE_MAINNET` | Set to `true` to switch from testnet to mainnet |

## Endpoints

- **Testnet**: `https://api.hyperliquid-testnet.xyz`
- **Mainnet**: `https://api.hyperliquid.xyz`

## Deployment

Deployed on Railway with nixpacks builder. Restarts automatically on failure.

## Architecture

- `scanner_server.py` — HTTP server, scan loop, alert logic, dashboard
- `hyperliquid_api.py` — Hyperliquid SDK wrapper: balance, positions, orders, monitoring
- `scanner.py` — Indicator calculations (MA, KDJ, EMA), scoring, trend classification
