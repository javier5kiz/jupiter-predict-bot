"""
Jupiter Predict — BTC Up or Down 5m Bot
Strategy: In the last 1 second of a round,
  if BTC spot price >= (price_to_beat + 5) → BUY UP  (100% of balance)
  if BTC spot price <= (price_to_beat - 5) → BUY DOWN (100% of balance)
  else → skip
"""

import os
import time
import base64
import asyncio
import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ─── CONFIG ────────────────────────────────────────────────────────────────────
JUPITER_API_KEY  = os.environ["JUPITER_API_KEY"]
WALLET_PRIVKEY   = os.environ["WALLET_PRIVKEY"]          # base58 private key
SOLANA_RPC       = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
THRESHOLD_USD    = 5.0        # $5 deviation from price-to-beat triggers entry
POLL_INTERVAL    = 0.25       # seconds between spot-price polls in final window
ENTRY_WINDOW_SEC = 1.0        # enter only inside last N seconds of round

JUP_BASE   = "https://api.jup.ag/prediction/v1"
PRICE_URL  = "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"  # SOL; BTC below
BTC_PRICE_URL = "https://api.jup.ag/price/v2?ids=9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"  # BTC via Jupiter

HEADERS = {
    "x-api-key": JUPITER_API_KEY,
    "Content-Type": "application/json",
}

# ─── WALLET ────────────────────────────────────────────────────────────────────
keypair = Keypair.from_base58_string(WALLET_PRIVKEY)
OWNER_PUBKEY = str(keypair.pubkey())
print(f"[WALLET] {OWNER_PUBKEY}")

# ─── HELPERS ───────────────────────────────────────────────────────────────────
async def get_active_btc_market(client: httpx.AsyncClient) -> dict | None:
    """Fetch the current live BTC Up/Down 5m market from Jupiter Forecast."""
    r = await client.get(
        f"{JUP_BASE}/events",
        params={
            "provider": "bisonfi",
            "category": "crypto",
            "filter": "active",
            "includeMarkets": "true",
            "query": "BTC",
        },
        headers=HEADERS,
    )
    r.raise_for_status()
    events = r.json().get("events", [])
    for event in events:
        title = event.get("title", "").lower()
        if "btc" in title and ("up or down" in title or "5m" in title):
            markets = event.get("markets", [])
            if markets:
                return event
    return None


async def get_btc_spot(client: httpx.AsyncClient) -> float:
    """Get BTC spot price from Jupiter Price API v2."""
    # BTC mint on Solana: 9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E (wrapped BTC)
    r = await client.get(BTC_PRICE_URL)
    r.raise_for_status()
    data = r.json()
    price_str = (
        data.get("data", {})
        .get("9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E", {})
        .get("price", "0")
    )
    return float(price_str)


async def get_usdc_balance(client: httpx.AsyncClient) -> int:
    """Return USDC balance in micro-USDC (1 USDC = 1_000_000)."""
    # Use Solana RPC getTokenAccountsByOwner for USDC
    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
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


async def place_order(client: httpx.AsyncClient, market_id: str, outcome: str, amount_micro: int) -> str | None:
    """Place a market buy order. Returns order pubkey or None on failure."""
    body = {
        "marketId": market_id,
        "outcome": outcome,          # "YES" = UP, "NO" = DOWN
        "depositAmount": str(amount_micro),
        "ownerPubkey": OWNER_PUBKEY,
    }
    r = await client.post(f"{JUP_BASE}/orders", json=body, headers=HEADERS)
    if r.status_code != 200:
        print(f"[ORDER ERROR] {r.status_code} {r.text}")
        return None

    data       = r.json()
    tx_b64     = data["transaction"]
    order_pk   = data["orderPubkey"]

    # Sign & send
    tx_bytes   = base64.b64decode(tx_b64)
    tx         = VersionedTransaction.from_bytes(tx_bytes)
    signed_tx  = keypair.sign_message(bytes(tx.message))   # sign message
    # Reconstruct signed VersionedTransaction
    signed_vtx = VersionedTransaction([signed_tx], tx.message)

    send_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [
            base64.b64encode(bytes(signed_vtx)).decode(),
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
    print(f"[TX SENT] sig={sig}  order={order_pk}  outcome={outcome}  amount={amount_micro/1e6:.4f} USDC")
    return order_pk


# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────
async def main():
    async with httpx.AsyncClient(timeout=5.0) as client:
        last_market_id = None

        while True:
            try:
                event = await get_active_btc_market(client)
                if not event:
                    print("[WAIT] No active BTC 5m market found. Retrying in 5s…")
                    await asyncio.sleep(5)
                    continue

                market     = event["markets"][0]
                market_id  = market["id"]
                end_time   = market.get("endTime") or event.get("endTime")  # ISO or unix ms
                price_to_beat = float(event.get("priceToBeat") or market.get("priceToBeat") or 0)

                # Parse end_time to unix seconds
                if isinstance(end_time, str):
                    from datetime import datetime, timezone
                    end_ts = datetime.fromisoformat(end_time.replace("Z", "+00:00")).timestamp()
                else:
                    end_ts = int(end_time) / 1000 if end_time > 1e12 else int(end_time)

                now = time.time()
                secs_left = end_ts - now

                if market_id == last_market_id:
                    # Already traded this round — wait for next
                    wait = max(secs_left + 2, 2)
                    print(f"[SKIP] Already traded market {market_id}. Waiting {wait:.1f}s for next round.")
                    await asyncio.sleep(min(wait, 10))
                    continue

                print(f"[MARKET] id={market_id}  price_to_beat=${price_to_beat:.2f}  ends_in={secs_left:.1f}s")

                # Wait until we're in the entry window (last 1 second)
                wait_until_entry = secs_left - ENTRY_WINDOW_SEC
                if wait_until_entry > 0:
                    await asyncio.sleep(wait_until_entry - 0.05)  # wake up just before

                # ── ENTRY WINDOW ──────────────────────────────────────────────
                entered = False
                while time.time() < end_ts:
                    spot = await get_btc_spot(client)
                    diff = spot - price_to_beat
                    print(f"  spot=${spot:.2f}  ptb=${price_to_beat:.2f}  diff={diff:+.2f}")

                    if diff >= THRESHOLD_USD:
                        outcome = "YES"   # UP
                    elif diff <= -THRESHOLD_USD:
                        outcome = "NO"    # DOWN
                    else:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    balance = await get_usdc_balance(client)
                    if balance < 10_000:  # less than $0.01 USDC
                        print("[SKIP] Balance too low.")
                        break

                    print(f"[ENTRY] outcome={outcome}  balance={balance/1e6:.4f} USDC")
                    order_pk = await place_order(client, market_id, outcome, balance)
                    if order_pk:
                        last_market_id = market_id
                        entered = True
                    break  # one trade per round regardless

                if not entered:
                    print("[PASS] Threshold not met — skipping this round.")
                    last_market_id = market_id  # don't retry same round

                # Brief pause then loop to pick up next market
                await asyncio.sleep(3)

            except Exception as e:
                print(f"[ERROR] {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
