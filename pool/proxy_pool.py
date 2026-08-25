import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import quote

import httpx
from sqlmodel import select, update
from config import settings
from core.database import get_db_session
from core.models import Proxy

logger = logging.getLogger("orchis.proxy_pool")


def parse_proxy_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single proxy line in formats:
    - IP:PORT:USER:PASS
    - IP:PORT
    - http://USER:PASS@IP:PORT
    - socks5://USER:PASS@IP:PORT
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.startswith("http://") or line.startswith("https://") or line.startswith("socks5://"):
        return {
            "url": line,
            "ip": line.split("@")[-1].split(":")[0] if "@" in line else line.split("//")[-1].split(":")[0],
            "port": int(line.split(":")[-1].split("/")[0]),
            "username": line.split("//")[1].split(":")[0] if "@" in line else "",
            "password": line.split("//")[1].split(":")[1].split("@")[0] if "@" in line else "",
        }

    parts = line.split(":")
    if len(parts) == 4:
        ip, port_s, user, pwd = parts
        try:
            port = int(port_s)
        except ValueError:
            return None
        encoded_user = quote(user, safe="")
        encoded_pwd = quote(pwd, safe="")
        url = f"http://{encoded_user}:{encoded_pwd}@{ip}:{port}"
        return {
            "url": url,
            "ip": ip,
            "port": port,
            "username": user,
            "password": pwd,
        }
    elif len(parts) == 2:
        ip, port_s = parts
        try:
            port = int(port_s)
        except ValueError:
            return None
        url = f"http://{ip}:{port}"
        return {
            "url": url,
            "ip": ip,
            "port": port,
            "username": "",
            "password": "",
        }
    return None


class ProxyPool:
    def __init__(self, proxy_file_path: Optional[str] = None):
        self.proxy_file_path = proxy_file_path or settings.PROXY_FILE_PATH
        self._current_index = 0
        self._lock = asyncio.Lock()

    async def sync_from_file(self, file_path: Optional[str] = None) -> int:
        """
        Reads proxies from file and synchronizes with SQLite DB.
        Returns count of proxies imported / updated.
        """
        path = file_path or self.proxy_file_path
        if not os.path.exists(path):
            logger.warning(f"Proxy file not found at {path}")
            return 0

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        added_or_updated = 0
        async with get_db_session() as session:
            for line in lines:
                parsed = parse_proxy_line(line)
                if not parsed:
                    continue

                # Check if exists
                stmt = select(Proxy).where(Proxy.url == parsed["url"])
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if not existing:
                    proxy = Proxy(
                        url=parsed["url"],
                        ip=parsed["ip"],
                        port=parsed["port"],
                        username=parsed["username"],
                        password=parsed["password"],
                        status="active",
                    )
                    session.add(proxy)
                    added_or_updated += 1
                else:
                    existing.ip = parsed["ip"]
                    existing.port = parsed["port"]
                    existing.username = parsed["username"]
                    existing.password = parsed["password"]
                    session.add(existing)

            await session.commit()
        logger.info(f"Synchronized {added_or_updated} new proxies from {path}")
        return added_or_updated

    async def get_all_proxies(self) -> List[Proxy]:
        """Fetch all proxies from database."""
        async with get_db_session() as session:
            stmt = select(Proxy).order_by(Proxy.id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_active_proxies(self) -> List[Proxy]:
        """Fetch all active proxies from database."""
        async with get_db_session() as session:
            stmt = select(Proxy).where(Proxy.status == "active").order_by(Proxy.error_count.asc(), Proxy.id.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_next_proxy(self) -> Optional[Proxy]:
        """
        Round-robin rotation over active proxies.
        If no proxies are configured or available, returns None.
        """
        async with self._lock:
            active_proxies = await self.get_active_proxies()
            if not active_proxies:
                # If DB is empty, try to auto-sync from file
                await self.sync_from_file()
                active_proxies = await self.get_active_proxies()
                if not active_proxies:
                    return None

            self._current_index = (self._current_index + 1) % len(active_proxies)
            proxy = active_proxies[self._current_index]

            # Update last used timestamp
            async with get_db_session() as session:
                stmt = select(Proxy).where(Proxy.id == proxy.id)
                res = await session.execute(stmt)
                p = res.scalar_one_or_none()
                if p:
                    p.last_used_at = datetime.now(timezone.utc)
                    session.add(p)
                    await session.commit()

            return proxy

    async def mark_proxy_success(self, proxy_url: str, latency_ms: Optional[int] = None) -> None:
        """Record successful request through proxy."""
        async with get_db_session() as session:
            stmt = select(Proxy).where(Proxy.url == proxy_url)
            res = await session.execute(stmt)
            proxy = res.scalar_one_or_none()
            if proxy:
                proxy.success_count += 1
                proxy.error_count = max(0, proxy.error_count - 1)
                if latency_ms is not None:
                    proxy.latency_ms = latency_ms
                proxy.status = "active"
                session.add(proxy)
                await session.commit()

    async def mark_proxy_failed(self, proxy_url: str, error_msg: Optional[str] = None) -> None:
        """Record failed request through proxy, mark failing if consecutive errors."""
        async with get_db_session() as session:
            stmt = select(Proxy).where(Proxy.url == proxy_url)
            res = await session.execute(stmt)
            proxy = res.scalar_one_or_none()
            if proxy:
                proxy.error_count += 1
                if proxy.error_count >= 5:
                    proxy.status = "failing"
                session.add(proxy)
                await session.commit()
                logger.warning(f"Proxy {proxy.ip}:{proxy.port} error count: {proxy.error_count} ({error_msg or 'unknown'})")

    async def check_proxy_health(self, proxy: Proxy, target_url: str = "https://httpbin.org/ip") -> Dict[str, Any]:
        """
        Probe proxy connectivity, latency, and returned IP.
        """
        start = asyncio.get_event_loop().time()
        try:
            async with httpx.AsyncClient(proxy=proxy.url, timeout=10.0, verify=False) as client:
                resp = await client.get(target_url)
                latency = int((asyncio.get_event_loop().time() - start) * 1000)
                if resp.status_code == 200:
                    await self.mark_proxy_success(proxy.url, latency_ms=latency)
                    return {
                        "url": proxy.url,
                        "ip": proxy.ip,
                        "port": proxy.port,
                        "status": "active",
                        "latency_ms": latency,
                        "status_code": resp.status_code,
                        "success": True,
                    }
                else:
                    await self.mark_proxy_failed(proxy.url, f"Status {resp.status_code}")
                    return {
                        "url": proxy.url,
                        "ip": proxy.ip,
                        "port": proxy.port,
                        "status": "failing",
                        "latency_ms": latency,
                        "status_code": resp.status_code,
                        "success": False,
                    }
        except Exception as e:
            latency = int((asyncio.get_event_loop().time() - start) * 1000)
            await self.mark_proxy_failed(proxy.url, str(e))
            return {
                "url": proxy.url,
                "ip": proxy.ip,
                "port": proxy.port,
                "status": "failing",
                "latency_ms": latency,
                "error": str(e),
                "success": False,
            }

    async def check_all_proxies(self, target_url: str = "https://httpbin.org/ip") -> List[Dict[str, Any]]:
        """Run health check across all proxies concurrently."""
        proxies = await self.get_all_proxies()
        if not proxies:
            await self.sync_from_file()
            proxies = await self.get_all_proxies()

        tasks = [self.check_proxy_health(p, target_url) for p in proxies]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return results


proxy_pool = ProxyPool()
