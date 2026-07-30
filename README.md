# Jupiter Predict — BTC Up or Down 5m Bot

Fast, lightweight bot for Jupiter Forecast's BTC Up/Down 5-minute prediction markets on Solana.

## Strategy

Monitors the live **BTC Up or Down 5m** market on [Jupiter Predict](https://jup.ag/predict).

In the **last 1 second** of each round:
- If BTC spot price is **≥ $5 above** the *Price to Beat* → **BUY UP** (100% of USDC balance)
- If BTC spot price is **≤ $5 below** the *Price to Beat* → **BUY DOWN** (100% of USDC balance)
- Otherwise → **skip** this round

Spot price and Price to Beat both sourced directly from Jupiter's APIs.

## Setup

### 1. Clone & install

```bash
git clone https://github.com/javier5kiz/jupiter-predict-bot
cd jupiter-predict-bot
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in your values
```

| Variable | Description |
|---|---|
| `JUPITER_API_KEY` | From [Jupiter Developer Platform](https://dev.jup.ag) |
| `WALLET_PRIVKEY` | Base58 Solana wallet private key (needs USDC balance) |
| `SOLANA_RPC` | RPC endpoint — use Helius/QuickNode for best latency |

### 3. Run

```bash
python bot.py
```

## Architecture

- **Single file** — `bot.py` is the entire bot, no extra complexity
- **Async/fast** — `httpx` async client, polls every 250ms in entry window
- **Last-second entry** — wakes up at `T-1s` from round end, polls until threshold or expiry
- **One trade per round** — never double-enters the same market
- **100% USDC in** — uses full available USDC balance per trade

## RPC Latency Note

For reliable last-second transaction delivery, use a private RPC:
- [Helius](https://helius.dev) — recommended for Solana
- [QuickNode](https://quicknode.com)
- [Triton](https://triton.one)

## Risk Warning

This bot places real on-chain trades with real USDC. Use at your own risk. Past performance on prediction markets does not guarantee future results.
