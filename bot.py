"""
Jupiter Predict — BTC Up or Down 5m Bot
═══════════════════════════════════════════════════════════════════════════════

Strategy:
  • Monitor live "Bitcoin Up/Down (5m)" markets on Jupiter Forecast (provider=bisonfi)
  • Capture BTC spot price at market open (beginAt)  →  that's the "price to beat"
  • Poll every 3 seconds and log: spot, price-to-beat, diff, time remaining
  • In the LAST 1 SECOND of the round, poll faster (250ms):
      - If spot >= price_to_beat + $5  →  BUY UP   (100% of USDC balance)
      - If spot <= price_to_beat - $5  →  BUY DOWN (100% of USDC balance)
      - Otherwise  →  skip this round
  • One trade per round, never double-enters

BTC spot price: Jupiter Price API v3 (wBTC on Solana)
Resolution:     Chainlink BTC/USD data stream (per market rules)

═══════════════════════════════════════════════════════════════════════════════
"""

import os
import time
import base64
import asyncio

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ─── CONFIG ────────────────────────────────────────────────────────────────────
JUPITER_API_KEY   = os.environ["JUPITER_API_KEY"]
WALLET_PRIVKEY    = os.environ["WALLET_PRIVKEY"]
SOLANA_RPC        = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
THRESHOLD_USD     = 5.0       # $5 deviation triggers entry
POLL_INTERVAL     = 0.25      # seconds between polls in the final entry window
REFRESH_INTERVAL  = 3.0       # seconds between status logs during the round
ENTRY_WINDOW_SEC  = 1.0       # enter only inside last N seconds of round

# wBTC mint on Solana — Jupiter Price v3 returns real BTC USD price for this
WBTC_MINT = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

JUP_PREDICT_BASE = "https://api.jup.ag/prediction/v1"
JUP_PRICE_V3      = f"https://api.jup.ag/price/v3?ids={WBTC_MINT}"

HEADERS = {
    "x-api-key": JUPITER_API_KEY,
    "Content-Type": "application/json",
}

# ─── WALLET ────────────────────────────────────────────────────────────────────
keypair     = Keypair.from_base58_string(WALLET_PRIVKEY)
OWNER_PUBKEY = str(keypair.pubkey())
print(f"[WALLET] {OWNER_PUBKEY}")

# ─── STATE ─────────────────────────────────────────────────────────────────────
traded_markets: set[str] = set()
price_to_beat: float | None = None


# ─── API CALLS ─────────────────────────────────────────────────────────────────
async def get_btc_5m_market(client: httpx.AsyncClient) -> dict | None:
    """
    Fetch the current Bitcoin Up/Down 5m market from Jupiter Forecast.
    Uses tags array for exact "5m" match — avoids matching "15m".
    """
    r = await client.get(
        f"{JUP_PREDICT_BASE}/events",
        params={
            "filter": "live",
            "provider": "bisonfi",
            "includeMarkets": "true",
        },
        headers=HEADERS,
    )
    r.raise_for_status()
    data = r.json()
    events = data.get("data", [])

    for event in events:
        tags = event.get("tags", [])
        series = event.get("metadata", {}).get("series", "")
        # Exact match: tags must contain "5m" (not "15m")
        if series == "btc" and "5m" in tags:
            return event
    return None


async def get_btc_spot(client: httpx.AsyncClient) -> float:
    """Get BTC spot price from Jupiter Price API v3 (wBTC on Solana)."""
    r = await client.get(JUP_PRICE_V3)
    r.raise_for_status()
    data = r.json()
    price = data[WBTC_MINT]["usdPrice"]
    return float(price)


async def get_market_prices(client: httpx.AsyncClient, market_id: str) -> dict | None:
    """Get current orderbook prices for a market."""
    r = await client.get(
        f"{JUP_PREDICT_BASE}/orderbook/{market_id}",
        headers=HEADERS,
    )
    if r.status_code != 200:
        return None
    return r.json()


async def get_usdc_balance(client: httpx.AsyncClient) -> int:
    """Return USDC balance in micro-USDC (1 USDC = 1_000_000)."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            OWNER_PUBKEY,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"},
        ],
    }
    r = await client.post(SOLANA_RPC, json=payload)
    r.raise_for_status()
    accounts = r.json().get("result", {}).get("value", [])
    if not accounts:
        return 0
    amount = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
    return int(amount)


async def place_order(
    client: httpx.AsyncClient,
    market_id: str,
    is_up: bool,
    amount_micro: int,
) -> str | None:
    """Place a market buy order via Jupiter Predict API."""
    body = {
        "marketId": market_id,
        "outcome": "YES",
        "isBuy": True,
        "depositAmount": str(amount_micro),
        "ownerPubkey": OWNER_PUBKEY,
    }
    r = await client.post(
        f"{JUP_PREDICT_BASE}/orders", json=body, headers=HEADERS
    )
    if r.status_code != 200:
        print(f"[ORDER ERROR] {r.status_code} {r.text[:300]}")
        return None

    data     = r.json()
    tx_b64   = data.get("transaction")
    order_pk = data.get("orderPubkey")

    if not tx_b64 or not order_pk:
        print(f"[ORDER] Unexpected response: {data}")
        return None

    # ── Sign & send the VersionedTransaction ────────────────────────────────
    tx_bytes  = base64.b64decode(tx_b64)
    tx        = VersionedTransaction.from_bytes(tx_bytes)
    signed    = keypair.sign_message(bytes(tx.message))
    signed_tx = VersionedTransaction([signed], tx.message)

    send_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [
            base64.b64encode(bytes(signed_tx)).decode(),
            {"encoding": "base64", "skipPreflight": False, "maxRetries": 3},
        ],
    }
    rpc_r = await client.post(SOLANA_RPC, json=send_payload)
    rpc_r.raise_for_status()
    result = rpc_r.json()

    if "error" in result:
        print(f"[RPC ERROR] {result['error']}")
        return None

    sig = result["result"]
    side = "UP" if is_up else "DOWN"
    print(f"[TX SENT] sig={sig}  order={order_pk}  side={side}  amount={amount_micro/1e6:.4f} USDC")
    return order_pk


# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────
async def main():
    global price_to_beat

    async with httpx.AsyncClient(timeout=5.0) as client:

        while True:
            try:
                event = await get_btc_5m_market(client)
                if not event:
                    print("[WAIT] No active BTC 5m market. Retrying in 5s…")
                    await asyncio.sleep(5)
                    continue

                meta      = event.get("metadata", {})
                event_id  = event.get("eventId", "")
                title     = meta.get("title", "")
                begin_at  = int(event.get("beginAt", 0))    # unix seconds — window start
                markets   = event.get("markets", [])

                # Find UP and DOWN market objects
                up_market   = next((m for m in markets if m.get("outcomeSide") == "up"),   None)
                down_market = next((m for m in markets if m.get("outcomeSide") == "down"), None)

                if not up_market or not down_market:
                    print(f"[ERR] Could not find UP/DOWN markets in {title}")
                    await asyncio.sleep(5)
                    continue

                close_time = up_market.get("closeTime")   # unix seconds
                open_time  = up_market.get("openTime")     # unix seconds

                # Already traded this market?
                if event_id in traded_markets:
                    secs_left = close_time - time.time()
                    if secs_left > 0:
                        await asyncio.sleep(min(secs_left + 3, 10))
                    continue

                # ── Capture price-to-beat at market open ───────────────────────
                if price_to_beat is None:
                    now_ts = time.time()

                    # Wait for market to actually open if it hasn't yet
                    if begin_at > now_ts:
                        wait_sec = begin_at - now_ts
                        print(f"[NEW ROUND] {title}  event={event_id}")
                        print(f"  market opens in {wait_sec:.1f}s — waiting…")
                        await asyncio.sleep(max(wait_sec, 0.1))

                    # Capture BTC spot price as price-to-beat at window start
                    price_to_beat = await get_btc_spot(client)
                    print(f"[NEW ROUND] {title}")
                    print(f"  event={event_id}")
                    print(f"  price_to_beat=${price_to_beat:,.2f}")
                    print(f"  window: {open_time} → {close_time}  ({close_time - open_time}s)")

                # ── 3-SECOND REFRESH POLLING ──────────────────────────────────
                # Poll every 3 seconds, log status, until we hit entry window
                while True:
                    now_ts = time.time()
                    secs_left = close_time - now_ts

                    if secs_left <= ENTRY_WINDOW_SEC:
                        break  # enter fast-poll entry window

                    # Fetch spot + log every 3 seconds
                    spot = await get_btc_spot(client)
                    diff = spot - price_to_beat
                    up_price   = up_market.get("pricing", {}).get("buyYesPriceUsd", 0) / 1_000_000
                    down_price = down_market.get("pricing", {}).get("buyYesPriceUsd", 0) / 1_000_000

                    print(
                        f"[LIVE] {secs_left:.0f}s left | "
                        f"spot=${spot:,.2f} | "
                        f"target=${price_to_beat:,.2f} | "
                        f"diff={diff:+.2f} | "
                        f"UP=${up_price:.3f} DOWN=${down_price:.3f}"
                    )

                    # Sleep 3 seconds (or less if close to entry window)
                    sleep_time = min(REFRESH_INTERVAL, max(secs_left - ENTRY_WINDOW_SEC, 0.1))
                    await asyncio.sleep(sleep_time)

                # ── ENTRY WINDOW: poll every 250ms ────────────────────────────
                print(f"[ENTRY WINDOW] Last 1 second — fast polling…")
                entered = False
                while time.time() < close_time:
                    spot = await get_btc_spot(client)
                    diff = spot - price_to_beat
                    print(f"  spot=${spot:,.2f}  target=${price_to_beat:,.2f}  diff={diff:+.2f}")

                    if diff >= THRESHOLD_USD:
                        side, market, is_up = "UP", up_market, True
                    elif diff <= -THRESHOLD_USD:
                        side, market, is_up = "DOWN", down_market, False
                    else:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # ── Place the order ──────────────────────────────────────
                    balance = await get_usdc_balance(client)
                    if balance < 10_000:  # less than $0.01
                        print("[SKIP] USDC balance too low.")
                        break

                    market_id = market.get("marketId")
                    print(f"[ENTRY] side={side}  market={market_id}  balance={balance/1e6:.4f} USDC")
                    order_pk = await place_order(client, market_id, is_up, balance)

                    if order_pk:
                        traded_markets.add(event_id)
                        entered = True
                    else:
                        print("[FAIL] Order failed — skipping this round")
                        traded_markets.add(event_id)
                    break

                if not entered:
                    print("[PASS] Threshold not met — skipping round.")
                    traded_markets.add(event_id)

                # Reset for next round
                price_to_beat = None
                await asyncio.sleep(3)

            except Exception as e:
                print(f"[ERROR] {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
