"""
Mirror spot orders from a leader wallet with fixed $10 USDC sizing.
Monitors leader's spot orders and places corresponding orders for follower.
Handles order placement, cancellation, and fills with real-time WebSocket monitoring.
"""

import asyncio
import json
import os
import signal
from typing import Dict, Optional
from dotenv import load_dotenv
import websockets
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.signing import OrderType as HLOrderType

load_dotenv()

# Configuration
WS_URL = os.getenv("HYPERLIQUID_TESTNET_PUBLIC_WS_URL")
BASE_URL = os.getenv("HYPERLIQUID_TESTNET_PUBLIC_BASE_URL")
LEADER_ADDRESS = "0x..."  # Replace with leader's wallet address
FIXED_ORDER_VALUE_USDC = 20.0  # Fixed $20 USDC per order

running = False
order_mappings: Dict[int, int] = {}  # leader_order_id -> follower_order_id
_metadata_cache: Optional[Dict] = None  # Cache for asset metadata


def signal_handler(_signum, _frame):
    """Handle Ctrl+C gracefully"""
    global running
    print("\nShutting down...")
    running = False


def detect_market_type(coin_field):
    """Detect market type from coin field"""
    if coin_field.startswith("@"):
        return "SPOT"
    elif "/" in coin_field:
        return "SPOT"
    else:
        return "PERP"


def is_spot_order(coin_field):
    """Check if order is for spot trading - basic format validation only"""
    if not coin_field or coin_field == "N/A":
        return False

    market_type = detect_market_type(coin_field)
    if market_type != "SPOT":
        return False

    # Basic format validation for @index
    if coin_field.startswith("@"):
        try:
            asset_index = int(coin_field[1:])
            # Only reject obviously invalid indices
            if asset_index < 0:
                return False
        except ValueError:
            return False

    return True




async def get_asset_metadata(info: Info) -> Dict:
    """Get and cache asset metadata for index-to-name mapping"""
    global _metadata_cache

    if _metadata_cache is None:
        try:
            meta = info.meta()
            universe = meta.get("universe", [])

            # Build index-to-asset mapping
            index_to_asset = {}
            for i, asset_info in enumerate(universe):
                asset_name = asset_info.get("name", "")
                if asset_name:
                    index_to_asset[i] = asset_name

            _metadata_cache = {
                "index_to_asset": index_to_asset,
                "universe": universe
            }
            print(f"📊 Loaded metadata for {len(universe)} assets")
        except Exception as e:
            print(f"⚠️ Error loading metadata: {e}")
            _metadata_cache = {"index_to_asset": {}, "universe": []}

    return _metadata_cache


async def get_spot_asset_info(info: Info, coin_field: str) -> Optional[dict]:
    """Get spot asset price and metadata for proper order sizing"""
    try:
        if coin_field.startswith("@"):
            # For @index format, use spot API
            spot_data = info.spot_meta_and_asset_ctxs()
            if len(spot_data) >= 2:
                spot_meta = spot_data[0]  # First element is metadata
                asset_ctxs = spot_data[1]  # Second element is asset contexts

                # Extract index number
                index = int(coin_field[1:])
                if index < len(asset_ctxs):
                    ctx = asset_ctxs[index]
                    # Try midPx first, fallback to markPx
                    price = float(ctx.get('midPx', ctx.get('markPx', 0)))

                    if price > 0:
                        # Get token metadata for size decimals
                        universe = spot_meta.get('universe', [])
                        tokens = spot_meta.get('tokens', [])

                        # Find the pair info
                        pair_info = None
                        for pair in universe:
                            if pair.get('index') == index:
                                pair_info = pair
                                break

                        # Get token info for size decimals
                        size_decimals = 6  # Default fallback
                        if pair_info and 'tokens' in pair_info:
                            token_indices = pair_info['tokens']
                            if len(token_indices) > 0:
                                base_token_index = token_indices[0]
                                if base_token_index < len(tokens):
                                    token_info = tokens[base_token_index]
                                    size_decimals = token_info.get('szDecimals', 6)

                        print(f"💰 Spot price for {coin_field}: ${price} (szDecimals: {size_decimals})")
                        return {
                            'price': price,
                            'szDecimals': size_decimals,
                            'coin': coin_field
                        }
                    else:
                        print(f"⚠️ No spot price for {coin_field} (midPx={ctx.get('midPx')}, markPx={ctx.get('markPx')})")
                        return None
                else:
                    print(f"⚠️ Spot index {coin_field} out of range (max: @{len(asset_ctxs)-1})")
                    return None

        elif "/" in coin_field:
            # For PAIR/USDC format, need to find the corresponding @index first
            spot_meta = info.spot_meta()
            universe = spot_meta.get('universe', [])

            # Find the matching pair in spot universe
            for pair_info in universe:
                if pair_info.get('name') == coin_field:
                    pair_index = pair_info.get('index')
                    # Get info using the index
                    return await get_spot_asset_info(info, f"@{pair_index}")

            print(f"⚠️ Spot pair {coin_field} not found in universe")
            return None

        else:
            print(f"⚠️ Unsupported coin format for spot: {coin_field}")
            return None

    except Exception as e:
        print(f"⚠️ Error getting spot info for {coin_field}: {e}")
        return None


async def get_spot_asset_price(info: Info, coin_field: str) -> Optional[float]:
    """Get spot asset price using proper spot API endpoints"""
    asset_info = await get_spot_asset_info(info, coin_field)
    return asset_info['price'] if asset_info else None


async def get_asset_price(info: Info, coin_field: str) -> Optional[float]:
    """Get current asset price for order sizing"""
    # For spot orders, use spot-specific API
    return await get_spot_asset_price(info, coin_field)


async def place_follower_order(
    exchange: Exchange,
    info: Info,
    leader_order_data: dict
) -> Optional[int]:
    """Place corresponding follower order for spot trades"""
    try:
        coin_field = leader_order_data.get("coin", "")
        side = leader_order_data.get("side")  # "B" or "A"
        price = float(leader_order_data.get("limitPx", 0))

        if not is_spot_order(coin_field):
            return None

        # Get current asset info with price and size decimals
        asset_info = await get_spot_asset_info(info, coin_field)
        if not asset_info:
            print(f"❌ Could not get asset info for {coin_field}")
            return None

        asset_price = asset_info['price']
        size_decimals = asset_info['szDecimals']

        # Calculate order size for $10 USDC value
        raw_order_size = FIXED_ORDER_VALUE_USDC / asset_price

        # Round to proper precision based on asset's size decimals
        order_size = round(raw_order_size, size_decimals)

        if order_size <= 0:
            print(f"❌ Invalid order size calculated for {coin_field}")
            return None

        is_buy = side == "B"

        print(f"🔄 Placing follower order: {'BUY' if is_buy else 'SELL'} {order_size} {coin_field} @ ${price}")

        # Place the order
        result = exchange.order(
            name=coin_field,
            is_buy=is_buy,
            sz=order_size,
            limit_px=price,
            order_type=HLOrderType({"limit": {"tif": "Gtc"}}),
            reduce_only=False,
        )

        if result and result.get("status") == "ok":
            response_data = result.get("response", {}).get("data", {})
            statuses = response_data.get("statuses", [])

            if statuses:
                status_info = statuses[0]
                if "resting" in status_info:
                    follower_order_id = status_info["resting"]["oid"]
                    print(f"✅ Follower order placed! ID: {follower_order_id}")
                    return follower_order_id
                elif "filled" in status_info:
                    print("✅ Follower order filled immediately!")
                    return -1  # Special value for immediate fill

        print(f"❌ Failed to place follower order: {result}")
        return None

    except Exception as e:
        print(f"❌ Error placing follower order: {e}")
        return None


async def cancel_follower_order(
    exchange: Exchange,
    follower_order_id: int,
    coin_field: str
) -> bool:
    """Cancel follower order"""
    try:
        print("🔄 Cancelling follower order ID:", follower_order_id)

        result = exchange.cancel_order(
            oid=follower_order_id,
            coin=coin_field
        )

        if result and result.get("status") == "ok":
            print("✅ Follower order cancelled successfully")
            return True
        else:
            print(f"❌ Failed to cancel follower order: {result}")
            return False

    except Exception as e:
        print(f"❌ Error cancelling follower order: {e}")
        return False


async def handle_leader_order_events(
    data: dict,
    exchange: Exchange,
    info: Info
):
    """Process leader's order-related WebSocket events"""
    channel = data.get("channel")

    if channel == "orderUpdates":
        orders = data.get("data", [])
        for order_update in orders:
            order = order_update.get("order", {})
            status = order_update.get("status", "unknown")
            coin_field = order.get("coin", "")

            # Only process valid spot orders
            if not is_spot_order(coin_field):
                continue

            leader_order_id = order.get("oid")
            side = "BUY" if order.get("side") == "B" else "SELL"
            size = order.get("sz", "N/A")
            price = order.get("limitPx", "N/A")

            print(f"📋 LEADER ORDER: {status.upper()} - {side} {size} {coin_field} @ {price} [SPOT] (ID: {leader_order_id})")

            if status == "open" and leader_order_id:
                # New order placed - attempt to mirror it
                try:
                    follower_order_id = await place_follower_order(exchange, info, order)
                    if follower_order_id:
                        order_mappings[leader_order_id] = follower_order_id
                        print(f"🔗 Mapped leader order {leader_order_id} -> follower order {follower_order_id}")
                    else:
                        print(f"⚠️ Could not mirror leader order {leader_order_id} for {coin_field}")
                except Exception as e:
                    print(f"❌ Error mirroring order {leader_order_id}: {e}")

            elif status == "canceled" and leader_order_id:
                # Order cancelled - cancel corresponding follower order
                if leader_order_id in order_mappings:
                    follower_order_id = order_mappings[leader_order_id]
                    if follower_order_id > 0:  # Don't try to cancel immediate fills
                        await cancel_follower_order(exchange, follower_order_id, coin_field)
                    del order_mappings[leader_order_id]
                    print(f"🔗 Removed mapping for cancelled order {leader_order_id}")

    elif channel == "userEvents":
        events = data.get("data", [])
        for event in events:
            if event.get("fills"):
                for fill in event["fills"]:
                    coin_field = fill.get("coin", "N/A")
                    if is_spot_order(coin_field):
                        side = "BUY" if fill.get("side") == "B" else "SELL"
                        print(f"💰 LEADER FILL: {side} {fill.get('sz', 'N/A')} {coin_field} @ {fill.get('px', 'N/A')} [SPOT] (Fee: {fill.get('fee', 'N/A')})")

    elif channel == "subscriptionResponse":
        print("✅ WebSocket subscription confirmed")


async def monitor_and_mirror_spot_orders():
    """Connect to WebSocket and monitor leader's spot order activity"""
    global running

    private_key = os.getenv("HYPERLIQUID_TESTNET_PRIVATE_KEY")
    if not private_key:
        print("❌ Missing HYPERLIQUID_TESTNET_PRIVATE_KEY in .env file")
        return

    if not LEADER_ADDRESS or LEADER_ADDRESS == "0x...":
        print("❌ Please set LEADER_ADDRESS in the script")
        return

    # Initialize follower trading components
    try:
        wallet = Account.from_key(private_key)
        exchange = Exchange(wallet, BASE_URL)
        info = Info(BASE_URL, skip_ws=True)
        print(f"✅ Follower wallet initialized: {wallet.address}")
    except Exception as e:
        print(f"❌ Failed to initialize follower wallet: {e}")
        return

    print(f"🔗 Connecting to {WS_URL}")
    signal.signal(signal.SIGINT, signal_handler)

    try:
        async with websockets.connect(WS_URL) as websocket:
            print("✅ WebSocket connected!")

            # Subscribe to leader's order updates
            order_subscription = {
                "method": "subscribe",
                "subscription": {
                    "type": "orderUpdates",
                    "user": LEADER_ADDRESS
                }
            }

            # Subscribe to leader's user events (fills)
            events_subscription = {
                "method": "subscribe",
                "subscription": {
                    "type": "userEvents",
                    "user": LEADER_ADDRESS
                }
            }

            await websocket.send(json.dumps(order_subscription))
            await websocket.send(json.dumps(events_subscription))

            print(f"📊 Monitoring SPOT orders for leader: {LEADER_ADDRESS}")
            print(f"💰 Fixed order value: ${FIXED_ORDER_VALUE_USDC} USDC per order")
            print(f"👤 Follower wallet: {wallet.address}")
            print("=" * 80)

            running = True

            async for message in websocket:
                if not running:
                    break

                try:
                    data = json.loads(message)
                    await handle_leader_order_events(data, exchange, info)
                except json.JSONDecodeError:
                    print("⚠️ Received invalid JSON")
                except Exception as e:
                    print(f"❌ Error processing message: {e}")

    except websockets.exceptions.ConnectionClosed:
        print("🔌 WebSocket connection closed")
    except Exception as e:
        print(f"❌ WebSocket error: {e}")
    finally:
        print("👋 Disconnected")
        print(f"📊 Final order mappings: {len(order_mappings)} active")


async def main():
    print("Hyperliquid Spot Order Mirror")
    print("=" * 40)

    if not WS_URL or not BASE_URL:
        print("❌ Missing required environment variables:")
        print("   HYPERLIQUID_TESTNET_PUBLIC_WS_URL")
        print("   HYPERLIQUID_TESTNET_PUBLIC_BASE_URL")
        return

    await monitor_and_mirror_spot_orders()


if __name__ == "__main__":
    asyncio.run(main())