"""Test Binance MAINNET connection and top movers data"""
import asyncio
import sys
import traceback

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, r"C:\Users\86137\Desktop\智能对冲马丁\backend")

from app.services.binance_service import BinanceService

async def test_mainnet():
    print("=" * 60)
    print("Testing Binance MAINNET - Top Movers Data")
    print("=" * 60)
    
    # Create mainnet connection (testnet=False)
    binance = BinanceService(api_key="", secret="", testnet=False)
    
    try:
        print("\n[1] Connecting to Binance Mainnet...")
        print("    URL: https://fapi.binance.com")
        
        print("\n[2] Fetching tickers...")
        tickers = await binance.exchange.fetch_tickers()
        print(f"    Total tickers: {len(tickers)}")
        
        # Check USDT pairs
        usdt_pairs = [s for s in tickers.keys() if ":USDT" in s]
        print(f"    USDT futures pairs: {len(usdt_pairs)}")
        
        if usdt_pairs:
            print(f"    Sample: {usdt_pairs[:3]}")
        
        print("\n[3] Fetching top movers (gainers + losers)...")
        movers = await binance.fetch_top_movers(source="both", limit=10)
        print(f"    Total movers fetched: {len(movers)}")
        
        # Display gainers
        gainers = [m for m in movers if m['source'] == 'gainers']
        print(f"\n[4] TOP GAINERS ({len(gainers)}):")
        print("-" * 60)
        for i, m in enumerate(gainers[:10], 1):
            symbol = m['symbol'].replace('USDT', '')
            change = m['price_change_pct']
            volume = m['volume_24h']
            print(f"    {i:2}. {symbol:8} | {change:>+8.2f}% | Vol: {volume:,.0f}")
        
        # Display losers
        losers = [m for m in movers if m['source'] == 'losers']
        print(f"\n[5] TOP LOSERS ({len(losers)}):")
        print("-" * 60)
        for i, m in enumerate(losers[:10], 1):
            symbol = m['symbol'].replace('USDT', '')
            change = m['price_change_pct']
            volume = m['volume_24h']
            print(f"    {i:2}. {symbol:8} | {change:>+8.2f}% | Vol: {volume:,.0f}")
        
        print("\n" + "=" * 60)
        print("SUCCESS: Mainnet data fetched successfully!")
        print("=" * 60)
        
    except Exception as e:
        print("\n" + "=" * 60)
        print("ERROR: Failed to fetch mainnet data")
        print("=" * 60)
        print(f"\nError type: {type(e).__name__}")
        print(f"Error message: {e}")
        print("\nFull traceback:")
        traceback.print_exc()
        print("\n" + "=" * 60)
        print("TROUBLESHOOTING:")
        print("- Check your internet connection")
        print("- If in China, configure HTTP_PROXY in .env file")
        print("- Example: HTTP_PROXY=http://127.0.0.1:7890")
        print("=" * 60)
    finally:
        await binance.close()

if __name__ == "__main__":
    asyncio.run(test_mainnet())
