# Jupiter Predict — BTC Up or Down 5m Bot

Fast, lightweight bot for Jupiter Forecast's BTC Up/Down 5-minute prediction markets on Solana.

## Strategy

Monitors the live **Bitcoin Up/Down (5m)** market on [Jupiter Predict](https://jup.ag/predict) (provider=bisonfi).

**How it works:**
1. When a new 5m round opens, the bot records the current BTC spot price → that's the **price to beat**
2. The bot sleeps until the **last 1 second** of the round
3. It polls BTC spot price every 250ms:
   - If spot **≥ price-to-beat + $5** → **BUY UP** (100% of USDC balance)
   - If spot **≤ price-to-beat − $5** → **BUY DOWN** (100% of USDC balance)
   - Otherwise → **skip** this round
4. One trade per round — never double-enters the same market

**Price sources:**
- BTC spot price: Jupiter Price API v3 (wBTC on Solana)
- Market resolution: Chainlink BTC/USD data stream (per Jupiter market rules)

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
| `WALLET_PRIVKEY` | Base58 Solana wallet private key (needs USDC + SOL for gas) |
| `SOLANA_RPC` | RPC endpoint — use Helius/QuickNode for best latency |

### 3. Fund your wallet

- Send **USDC** (Solana) to your bot wallet address — this is your trading balance
- Send a small amount of **SOL** for transaction gas (~0.05 SOL is plenty)

### 4. Run

```bash
python bot.py
```

## API Reference (verified)

### Market Discovery
```
GET /prediction/v1/events?filter=live&provider=bisonfi&includeMarkets=true
```
Response: `{ "data": [...] }` — each event has `metadata.title`, `eventId`, and `markets[]`

BTC 5m markets have `metadata.series = "btc"` and title containing `"5m"`.

### Markets
Each event has two markets:
- `marketId` ending in `-UP` (outcomeSide: "up")
- `marketId` ending in `-DOWN` (outcomeSide: "down")
- `openTime` / `closeTime`: unix timestamps in seconds

### Orderbook
```
GET /prediction/v1/orderbook/{marketId}
```
Returns `yes` / `no` bid arrays with prices in cents and dollar format.

### Place Order
```
POST /prediction/v1/orders
Body: { "marketId": "...", "outcome": "YES", "isBuy": true, "depositAmount": "micro_usdc", "ownerPubkey": "..." }
```
Returns Base64-encoded `VersionedTransaction` + `orderPubkey`. Sign with your keypair and send via Solana RPC.

### Check Positions
```
GET /prediction/v1/positions?ownerPubkey={pubkey}
```

### BTC Spot Price
```
GET /price/v3?ids=3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh
```
Returns `{ "...": { "usdPrice": 64821.45 } }` — real-time BTC USD price via Jupiter Price v3.

## ⚠️ Region Restriction

Jupiter Predict blocks trading from certain regions. The order endpoint may return:
```json
{"code": "unsupported_region", "message": "Trading is not available in your region"}
```

If you encounter this, run the bot from a VPS in a supported region (e.g., US).

## RPC Latency

For reliable last-second transaction delivery, use a private RPC:
- [Helius](https://helius.dev) — recommended for Solana (free tier available)
- [QuickNode](https://quicknode.com)
- [Triton](https://triton.one)

## Risk Warning

This bot places real on-chain trades with real USDC. Use at your own risk. Prediction markets are inherently risky — past performance does not guarantee future results.
