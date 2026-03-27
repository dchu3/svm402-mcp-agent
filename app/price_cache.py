"""TTL-based price cache to reduce redundant API calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple


@dataclass
class CachedPrice:
    """Cached price data with timestamp."""

    data: Any
    cached_at: datetime


class PriceCache:
    """In-memory TTL cache for token price data.
    
    Reduces redundant DexScreener/DexPaprika API calls by caching
    price responses for a configurable duration.
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        """Initialize the cache.
        
        Args:
            ttl_seconds: How long cached entries remain valid (default: 30s)
        """
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[Tuple[str, str], CachedPrice] = {}
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    def _make_key(self, chain: str, token_address: str) -> Tuple[str, str]:
        """Create a cache key from chain and address."""
        return (chain.lower(), token_address.lower())

    def _is_expired(self, cached: CachedPrice) -> bool:
        """Check if a cached entry has expired."""
        age = datetime.now(timezone.utc) - cached.cached_at
        return age > timedelta(seconds=self.ttl_seconds)

    async def get(self, chain: str, token_address: str) -> Optional[Any]:
        """Get cached price data if available and not expired.
        
        Args:
            chain: Blockchain network (e.g., 'solana')
            token_address: Token contract address
            
        Returns:
            Cached data if fresh, None if missing or expired
        """
        key = self._make_key(chain, token_address)
        
        async with self._lock:
            cached = self._cache.get(key)
            
            if cached is None:
                self._misses += 1
                return None
            
            if self._is_expired(cached):
                del self._cache[key]
                self._misses += 1
                return None
            
            self._hits += 1
            return cached.data

    async def set(self, chain: str, token_address: str, data: Any) -> None:
        """Store price data in the cache.
        
        Args:
            chain: Blockchain network
            token_address: Token contract address
            data: Price data to cache
        """
        key = self._make_key(chain, token_address)
        
        async with self._lock:
            self._cache[key] = CachedPrice(
                data=data,
                cached_at=datetime.now(timezone.utc),
            )

    async def clear(self) -> int:
        """Clear all cached entries.
        
        Returns:
            Number of entries cleared
        """
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    async def cleanup_expired(self) -> int:
        """Remove expired entries from the cache.
        
        Returns:
            Number of entries removed
        """
        async with self._lock:
            expired_keys = [
                key for key, cached in self._cache.items()
                if self._is_expired(cached)
            ]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)

    @property
    def stats(self) -> Dict[str, int]:
        """Get cache statistics."""
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / (self._hits + self._misses) * 100, 1)
            if (self._hits + self._misses) > 0
            else 0.0,
        }
