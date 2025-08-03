import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Optional
import orjson

try:
    import aiofiles
except ImportError:
    # Fallback to synchronous file operations if aiofiles not available
    aiofiles = None

from .config.settings import settings


class FileSystemCache:
    """High-performance filesystem cache for run data."""
    
    def __init__(self, cache_dir: str = "/tmp/ls_cache", max_size_mb: int = 100):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self._lock = asyncio.Lock()
    
    def _get_cache_path(self, key: str) -> Path:
        """Get cache file path for a given key."""
        # Use first 2 chars for directory sharding
        key_hash = hashlib.md5(key.encode()).hexdigest()
        subdir = self.cache_dir / key_hash[:2]
        subdir.mkdir(exist_ok=True)
        return subdir / f"{key_hash}.json"
    
    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get cached data if it exists and is valid."""
        cache_path = self._get_cache_path(key)
        
        if not cache_path.exists():
            return None
        
        try:
            if aiofiles:
                async with aiofiles.open(cache_path, 'rb') as f:
                    data = await f.read()
            else:
                # Fallback to sync operations
                with open(cache_path, 'rb') as f:
                    data = f.read()
            
            cached_item = orjson.loads(data)
            
            # Check if cache is still valid (1 hour TTL)
            if time.time() - cached_item.get('timestamp', 0) > 3600:
                # Cache expired, remove it
                cache_path.unlink(missing_ok=True)
                return None
            
            return cached_item.get('data')
            
        except Exception:
            # If any error, remove the corrupted cache file
            cache_path.unlink(missing_ok=True)
            return None
    
    async def set(self, key: str, data: Dict[str, Any]) -> None:
        """Cache data with timestamp."""
        cache_path = self._get_cache_path(key)
        
        cached_item = {
            'timestamp': time.time(),
            'data': data
        }
        
        try:
            if aiofiles:
                async with aiofiles.open(cache_path, 'wb') as f:
                    await f.write(orjson.dumps(cached_item))
            else:
                # Fallback to sync operations
                with open(cache_path, 'wb') as f:
                    f.write(orjson.dumps(cached_item))
            
            # Cleanup old cache files if needed
            await self._cleanup_if_needed()
            
        except Exception:
            # If caching fails, just continue without caching
            pass
    
    async def _cleanup_if_needed(self) -> None:
        """Remove old cache files if cache size exceeds limit."""
        async with self._lock:
            total_size = 0
            cache_files = []
            
            # Collect all cache files with their stats
            for subdir in self.cache_dir.iterdir():
                if subdir.is_dir():
                    for cache_file in subdir.glob("*.json"):
                        try:
                            stat = cache_file.stat()
                            total_size += stat.st_size
                            cache_files.append((cache_file, stat.st_mtime, stat.st_size))
                        except OSError:
                            continue
            
            # If over limit, remove oldest files
            if total_size > self.max_size_bytes:
                # Sort by modification time (oldest first)
                cache_files.sort(key=lambda x: x[1])
                
                for cache_file, _, size in cache_files:
                    try:
                        cache_file.unlink()
                        total_size -= size
                        if total_size <= self.max_size_bytes * 0.8:  # Remove 20% extra
                            break
                    except OSError:
                        continue
    
    async def delete(self, key: str) -> None:
        """Delete cached item."""
        cache_path = self._get_cache_path(key)
        cache_path.unlink(missing_ok=True)


# Global cache instance
_cache = None

def get_cache() -> FileSystemCache:
    """Get the global cache instance."""
    global _cache
    if _cache is None:
        cache_dir = getattr(settings, 'CACHE_DIR', '/tmp/ls_cache')
        cache_size = getattr(settings, 'CACHE_SIZE_MB', 100)
        _cache = FileSystemCache(cache_dir, cache_size)
    return _cache