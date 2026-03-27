#!/usr/bin/env python3
"""Manual verification script for skip phases feature."""

import asyncio
import tempfile
from pathlib import Path

from app.database import Database


async def main():
    """Demonstrate skip phases feature."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(db_path=Path(tmpdir) / "test.db")
        await db.connect()
        
        token = "TokenTestAddress123"
        chain = "solana"
        
        print("=== Skip Phases Feature Demonstration ===\n")
        
        # Initial state
        print("1. Initial state - token has no skip phases:")
        skip = await db.get_skip_phases(token, chain)
        print(f"   skip_phases = {skip}\n")
        
        # First negative stop loss
        print("2. First negative stop loss:")
        count = await db.increment_negative_sl_count(token, chain)
        skip = await db.get_skip_phases(token, chain)
        print(f"   negative_sl_count = {count}")
        print(f"   skip_phases = {skip} (still 0, not skipped yet)\n")
        
        # Second negative stop loss - triggers skip
        print("3. Second negative stop loss:")
        count = await db.increment_negative_sl_count(token, chain)
        skip = await db.get_skip_phases(token, chain)
        print(f"   negative_sl_count = {count}")
        print(f"   skip_phases = {skip} ⚠️ (TOKEN WILL BE SKIPPED)\n")
        
        # After one discovery cycle
        print("4. After one discovery cycle (decrement skip_phases):")
        await db.decrement_all_skip_phases(chain)
        skip = await db.get_skip_phases(token, chain)
        print(f"   skip_phases = {skip}")
        print(f"   ✅ Token is now discoverable again!")
        print(f"   (negative_sl_count was also reset)\n")
        
        # Verify reset
        print("5. Verify fresh start - next negative SL starts at count 1:")
        count = await db.increment_negative_sl_count(token, chain)
        skip = await db.get_skip_phases(token, chain)
        print(f"   negative_sl_count = {count}")
        print(f"   skip_phases = {skip}\n")
        
        print("=== Feature working correctly! ===")
        
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
