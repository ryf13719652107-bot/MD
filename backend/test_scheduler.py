import asyncio
import sys
sys.path.insert(0, 'app')

from app.services.scheduler import strategy_scheduler
from app.database import async_session
from app.models.strategy import Strategy
from sqlalchemy import select

async def test():
    # Check if scheduler is running
    print(f"Scheduler running: {strategy_scheduler.scheduler.running}")
    print(f"Jobs: {strategy_scheduler.scheduler.get_jobs()}")
    
    # Try to start scheduler
    strategy_scheduler.start()
    print(f"Scheduler running after start: {strategy_scheduler.scheduler.running}")
    
    # Check strategies
    async with async_session() as session:
        result = await session.execute(select(Strategy))
        strategies = result.scalars().all()
        print(f"\nStrategies: {len(strategies)}")
        for s in strategies:
            print(f"  ID={s.id}, Status={s.status}")
            
        # Try to start strategy 1
        if strategies:
            print("\nTrying to start strategy 1...")
            await strategy_scheduler.add_strategy(1)
            
            # Check status after
            result = await session.execute(select(Strategy).where(Strategy.id == 1))
            strategy = result.scalar()
            print(f"Strategy 1 status after start: {strategy.status}")
            print(f"Jobs after start: {strategy_scheduler.scheduler.get_jobs()}")

asyncio.run(test())
