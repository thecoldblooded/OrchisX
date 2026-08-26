import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import quote

import httpx
from sqlmodel import select, update, delete
from config import settings
from core.database import get_db_session
from core.models import Proxy

logger = logging.getLogger("orchis.proxy_pool")


def parse_proxy_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single proxy line in formats:
    - protocol://user:pass@ip:port
    - ip:port:user:pass
    - ip:port
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.startswith("http://") or line.startswith("https://") or line.startswith("socks5://"):
        return {
            "url": line,
            "protocol": line.split("://")[0],
            "ip": line.split("@")[-1].split(":")[0] if "@" in line else line.split("://")[1].split(":")[0],
            "port": int(line.split(":")[-1].split("/")[0]),
            "username": line.split("://")[1].split("@")[0].split(":")[0] if "@" in line else "",
            "password": line.split("://")[1].split("@")[0].split(":")[1] if "@" in line and ":" in line.split("://")[1].split("@")[0] else "",
        }

    parts = line.split(":")
    if len(parts) == 4:
        ip, port_s, user, pwd = parts
        try:
            port = int(port_s)
        except ValueError:
            return None
        safe_user = quote(user, safe="")
        safe_pwd = quote(pwd, safe="")
        url = f"http://{safe_user}:{safe_pwd}@{ip}:{port}"
        return {
            "url": url,
            "protocol": "http",
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
            "protocol": "http",
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
        Synchronize proxies from a text file into SQLite database.
        """
        path = file_path or self.proxy_file_path
        if not path or not os.path.exists(path):
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

                stmt = select(Proxy).where(Proxy.url == parsed["url"])
                result = await session.execute(stmt)
                existing = result.scalars().first()

                if not existing:
                    proxy = Proxy(
                        url=parsed["url"],
                        protocol=parsed["protocol"],
                        ip=parsed["ip"],
                        port=parsed["port"],
                        username=parsed["username"],
                        password=parsed["password"],
                        status="active",
                        success_count=0,
                        error_count=0,
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

    async def import_from_text(self, text: str, session_id: Optional[str] = None) -> int:
        """Parse and insert multiple proxies from raw multi-line or single-line text."""
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if not lines:
            return 0

        added_or_updated = 0
        async with get_db_session() as session:
            for line in lines:
                parsed = parse_proxy_line(line)
                if not parsed:
                    continue

                stmt = select(Proxy).where(Proxy.url == parsed["url"])
                if session_id:
                    stmt = stmt.where(Proxy.session_id == session_id)
                result = await session.execute(stmt)
                existing = result.scalars().first()

                if not existing:
                    proxy = Proxy(
                        url=parsed["url"],
                        protocol=parsed["protocol"],
                        ip=parsed["ip"],
                        port=parsed["port"],
                        username=parsed["username"],
                        password=parsed["password"],
                        session_id=session_id,
                        status="active",
                        success_count=0,
                        error_count=0
                    )
                    session.add(proxy)
                    added_or_updated += 1
                else:
                    existing.ip = parsed["ip"]
                    existing.port = parsed["port"]
                    existing.username = parsed["username"]
                    existing.password = parsed["password"]
                    existing.status = "active"
                    if session_id:
                        existing.session_id = session_id
                    session.add(existing)

            await session.commit()
        logger.info(f"Imported/Updated {added_or_updated} proxies from text input")
        return added_or_updated

    async def remove_proxy(self, proxy_id: int, session_id: Optional[str] = None) -> bool:
        """Remove a proxy from the database."""
        async with get_db_session() as session:
            stmt = select(Proxy).where(Proxy.id == proxy_id)
            if session_id:
                stmt = stmt.where(Proxy.session_id == session_id)
            result = await session.execute(stmt)
            proxy = result.scalars().first()
            if not proxy:
                return False
            await session.delete(proxy)
            await session.commit()
            return True

    async def delete_all_by_session(self, session_id: str) -> int:
        """Delete all proxies belonging to a session."""
        if not session_id:
            return 0
        async with get_db_session() as session:
            stmt = select(Proxy).where(Proxy.session_id == session_id)
            res = await session.execute(stmt)
            proxies = res.scalars().all()
            count = len(proxies)
            for p in proxies:
                await session.delete(p)
            await session.commit()
            return count

    async def get_all_proxies(self, session_id: Optional[str] = None) -> List[Proxy]:
        """Fetch all proxies from database."""
        async with get_db_session() as session:
            stmt = select(Proxy)
            if session_id:
                stmt = stmt.where(Proxy.session_id == session_id)
            stmt = stmt.order_by(Proxy.id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_active_proxies(self, session_id: Optional[str] = None) -> List[Proxy]:
        """Fetch all active proxies from database."""
        async with get_db_session() as session:
            stmt = select(Proxy).where(Proxy.status == "active")
            if session_id:
                stmt = stmt.where(Proxy.session_id == session_id)
            stmt = stmt.order_by(Proxy.error_count.asc(), Proxy.id.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_next_proxy(self, session_id: Optional[str] = None) -> Optional[Proxy]:
        """
        Round-robin selection of an active proxy.
        Returns None if no active proxies exist.
        """
        async with self._lock:
            proxies = await self.get_active_proxies(session_id=session_id)
            if not proxies:
                return None

            idx = self._current_index % len(proxies)
            self._current_index = (self._current_index + 1) % len(proxies)
            proxy = proxies[idx]

            async with get_db_session() as session:
                stmt = select(Proxy).where(Proxy.id == proxy.id)
                res = await session.execute(stmt)
                db_proxy = res.scalars().first()
                if db_proxy:
                    db_proxy.last_used_at = datetime.now(timezone.utc)
                    session.add(db_proxy)
                    await session.commit()
            return proxy

    async def mark_proxy_success(self, proxy_url: str, latency_ms: Optional[int] = None) -> None:
        """Record successful request through proxy."""
        async with get_db_session() as session:
            stmt = select(Proxy).where(Proxy.url == proxy_url)
            res = await session.execute(stmt)
            proxy = res.scalars().first()
            if proxy:
                proxy.success_count += 1
                proxy.status = "active"
                if latency_ms is not None:
                    proxy.latency_ms = latency_ms
                proxy.last_checked_at = datetime.now(timezone.utc)
                session.add(proxy)
                await session.commit()

    async def mark_proxy_failed(self, proxy_url: str, error_msg: Optional[str] = None) -> None:
        """Record failed request through proxy, mark failing if consecutive errors."""
        async with get_db_session() as session:
            stmt = select(Proxy).where(Proxy.url == proxy_url)
            res = await session.execute(stmt)
            proxy = res.scalars().first()
            if proxy:
                proxy.error_count += 1
                if proxy.error_count >= 5:
                    proxy.status = "failing"
                proxy.last_checked_at = datetime.now(timezone.utc)
                session.add(proxy)
                await session.commit()
                logger.warning(f"Proxy {proxy.ip}:{proxy.port} error count: {proxy.error_count} ({error_msg or 'unknown'})")

    async def check_proxy_health(self, proxy: Proxy, target_url: str = "https://httpbin.org/ip") -> Dict[str, Any]:
        """
        Benchmark a single proxy: measures latency, connectivity, and verifies IP.
        """
        start = datetime.now(timezone.utc)
        try:
            async with httpx.AsyncClient(
                proxy=proxy.url,
                timeout=10.0,
                headers={"User-Agent": settings.DEFAULT_USER_AGENT},
            ) as client:
                resp = await client.get(target_url)
                latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

                if resp.status_code == 200:
                    await self.mark_proxy_success(proxy.url, latency_ms=latency)
                    return {
                        "proxy_id": proxy.id,
                        "ip": proxy.ip,
                        "status": "active",
                        "latency_ms": latency,
                        "error": None,
                    }
                else:
                    await self.mark_proxy_failed(proxy.url, f"HTTP {resp.status_code}")
                    return {
                        "proxy_id": proxy.id,
                        "ip": proxy.ip,
                        "status": "failing",
                        "latency_ms": None,
                        "error": f"HTTP {resp.status_code}",
                    }
        except Exception as e:
            await self.mark_proxy_failed(proxy.url, str(e))
            return {
                "proxy_id": proxy.id,
                "ip": proxy.ip,
                "status": "failing",
                "latency_ms": None,
                "error": str(e),
            }

    async def check_all_proxies(self, session_id: Optional[str] = None, target_url: str = "https://httpbin.org/ip") -> List[Dict[str, Any]]:
        """Run health check across proxies concurrently."""
        proxies = await self.get_all_proxies(session_id=session_id)
        if not proxies:
            return []

        tasks = [self.check_proxy_health(p, target_url) for p in proxies]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, dict)]


proxy_pool = ProxyPool()
