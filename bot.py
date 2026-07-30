"""
Jupiter Predict — BTC Up or Down 5m Bot
═══════════════════════════════════════════════════════════════════════════════

Strategy:
  • Monitor live "Bitcoin Up/Down (5m)" markets on Jupiter Forecast (provider=bisonfi)
  • Record BTC spot price at market open  →  that's the "price to beat"
  • In the LAST 1 SECOND of the round, poll BTC spot price every 250ms:
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
from datetime import datetime, timezone

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ─── CONFIG ────────────────────────────────────────────────────────────────────
JUPITER_API_KEY   = os.environ["JUPITER_API_KEY"]
WALLET_PRIVKEY    = os.environ["WALLET_PRIVKEY"]
SOLANA_RPC        = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
THRESHOLD_USD     = 5.0       # $5 deviation triggers entry
POLL_INTERVAL     = 0.25      # seconds between spot-price polls in entry window
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
# Track which market we've already traded and the opening price
traded_markets: set[str] = set()
price_to_beat: float | None = None


# ─── API CALLS ─────────────────────────────────────────────────────────────────
async def get_btc_5m_market(client: httpx.AsyncClient) -> dict | None:
    """
    Fetch the current Bitcoin Up/Down 5m market from Jupiter Forecast.
    Returns the event dict or None.
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
        meta = event.get("metadata", {})
        title = meta.get("title", "")
        series = meta.get("series", "")
        if series == "btc" and "5m" in title.lower():
            return event
    return None


async def get_btc_spot(client: httpx.AsyncClient) -> float:
    """Get BTC spot price from Jupiter Price API v3 (wBTC on Solana)."""
    r = await client.get(JUP_PRICE_V3)
    r.raise_for_status()
    data = r.json()
    price = data[WBTC_MINT]["usdPrice"]
    return float(price)


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
    """
    Place a market buy order via Jupiter Predict API.
    Returns order pubkey on success, None on failure.
    """
    body = {
        "marketId": market_id,
        "outcome": "YES",          # buying YES shares for the chosen side
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
                markets   = event.get("markets", [])

                # Find UP and DOWN market objects
                up_market   = next((m for m in markets if m.get("outcomeSide") == "up"),   None)
                down_market = next((m for m in markets if m.get("outcomeSide") == "down"), None)

                if not up_market or not down_market:
                    print(f"[ERR] Could not find UP/DOWN markets in {title}")
                    await asyncio.sleep(5)
                    continue

                # Use UP market's timing (both share the same window)
                close_time = up_market.get("closeTime")  # unix seconds
                open_time  = up_market.get("openTime")   # unix seconds

                # Already traded this market?
                if event_id in traded_markets:
                    secs_left = close_time - time.time()
                    wait = max(secs_left + 3, 2)
                    print(f"[SKIP] Already traded {event_id}. Waiting {wait:.0f}s for next round.")
                    await asyncio.sleep(min(wait, 10))
                    continue

                # ── Capture price-to-beat when market first appears ───────────
                now_ts = time.time()
                if price_to_beat is None:
                    price_to_beat = await get_btc_spot(client)
                    print(f"[NEW ROUND] {title}")
                    print(f"  event={event_id}")
                    print(f"  price_to_beat=${price_to_beat:,.2f}")
                    print(f"  window: {open_time} → {close_time}  ({close_time - open_time}s)")

                secs_left = close_time - time.time()

                # ── Wait until entry window (last 1 second) ───────────────────
                wait_until_entry = secs_left - ENTRY_WINDOW_SEC
                if wait_until_entry > 0:
                    # Sleep until just before entry window, checking every 0.5s
                    print(f"  {secs_left:.1f}s left — waiting for entry window…")
                    while time.time() < close_time - ENTRY_WINDOW_SEC:
                        await asyncio.sleep(min(0.5, max(close_time - ENTRY_WINDOW_SEC - time.time(), 0)))

                # ── ENTRY WINDOW: poll every 250ms ────────────────────────────
                entered = False
                while time.time() < close_time:
                    spot = await get_btc_spot(client)
                    diff = spot - price_to_beat
                    print(f"  spot=${spot:,.2f}  ptb=${price_to_beat:,.2f}  diff={diff:+.2f}")

                    if diff >= THRESHOLD_USD:
                        side      = "UP"
                        market    = up_market
                        is_up     = True
                    elif diff <= -THRESHOLD_USD:
                        side      = "DOWN"
                        market    = down_market
                        is_up     = False
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
                        print("[RETRY] Order failed — will retry next round")
                        traded_markets.add(event_id)  # don't retry same round
                    break

                if not entered:
                    print("[PASS] Threshold not met — skipping round.")
                    traded_markets.add(event_id)

                # Reset price_to_beat for next round
                price_to_beat = None
                await asyncio.sleep(3)

            except Exception as e:
                print(f"[ERROR] {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
