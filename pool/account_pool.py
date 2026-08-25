import asyncio
from datetime import datetime, timezone, timedelta
import json
import logging
from typing import List, Optional, Dict, Any, Union

from sqlmodel import select, update, delete
from config import settings
from core.database import get_db_session
from core.models import Account, utc_now

logger = logging.getLogger("orchis.account_pool")


class AccountPool:
    def __init__(self):
        self._lock = asyncio.Lock()

    async def add_or_update_account(
        self,
        auth_token: str,
        ct0: str,
        username: Optional[str] = None
    ) -> Account:
        """Add a new Twitter account or update existing credentials."""
        auth_token = auth_token.strip()
        ct0 = ct0.strip()
        if username:
            username = username.strip().lstrip("@")

        async with get_db_session() as session:
            stmt = select(Account).where(Account.auth_token == auth_token)
            result = await session.execute(stmt)
            account = result.scalar_one_or_none()

            if account:
                account.ct0 = ct0
                if username:
                    account.username = username
                account.status = "active"
                account.rate_limit_reset_at = None
                session.add(account)
                await session.commit()
                await session.refresh(account)
                logger.info(f"Updated account {account.username or account.id}")
                return account
            else:
                account = Account(
                    auth_token=auth_token,
                    ct0=ct0,
                    username=username,
                    status="active"
                )
                session.add(account)
                await session.commit()
                await session.refresh(account)
                logger.info(f"Added new account {account.username or account.id}")
                return account

    async def get_active_account(self) -> Optional[Account]:
        """
        Get next available active account, prioritizing least recently used.
        Automatically recovers rate_limited accounts whose reset time has expired.
        """
        now = utc_now()
        async with self._lock:
            async with get_db_session() as session:
                # 1. Recover expired rate-limited accounts
                recovery_stmt = select(Account).where(
                    Account.status == "rate_limited",
                    Account.rate_limit_reset_at != None,
                    Account.rate_limit_reset_at <= now
                )
                res = await session.execute(recovery_stmt)
                for acc in res.scalars().all():
                    acc.status = "active"
                    acc.rate_limit_reset_at = None
                    session.add(acc)
                await session.commit()

                # 2. Pick least recently used active account
                active_stmt = select(Account).where(
                    Account.status == "active"
                ).order_by(
                    Account.last_used_at.asc().nullsfirst(),
                    Account.id.asc()
                ).limit(1)
                res = await session.execute(active_stmt)
                account = res.scalars().first()

                if account:
                    account.last_used_at = now
                    session.add(account)
                    await session.commit()
                    await session.refresh(account)
                    return account

                return None

    async def get_next_reset_seconds(self) -> int:
        """Return seconds remaining until the earliest rate-limited account resets."""
        now = utc_now()
        async with get_db_session() as session:
            stmt = select(Account).where(
                Account.status == "rate_limited",
                Account.rate_limit_reset_at != None
            ).order_by(Account.rate_limit_reset_at.asc()).limit(1)
            res = await session.execute(stmt)
            acc = res.scalars().first()
            if acc and acc.rate_limit_reset_at:
                # Ensure timezone aware comparison
                reset_at = acc.rate_limit_reset_at
                if reset_at.tzinfo is None:
                    reset_at = reset_at.replace(tzinfo=timezone.utc)
                remaining = int((reset_at - now).total_seconds())
                return max(5, remaining)
        return 30
    async def has_active_account(self) -> bool:
        """Check if any active account is currently available without modifying last_used_at."""
        now = utc_now()
        async with get_db_session() as session:
            stmt = select(Account).where(
                (Account.status == "active") |
                ((Account.status == "rate_limited") & (Account.rate_limit_reset_at != None) & (Account.rate_limit_reset_at <= now))
            ).limit(1)
            res = await session.execute(stmt)
            return res.scalars().first() is not None

    async def mark_rate_limited(self, account_id: int, reset_seconds: Optional[int] = None) -> None:
        """Mark account as rate limited for reset_seconds (default 15m)."""
        duration = reset_seconds or settings.RATE_LIMIT_COOLDOWN_SECONDS
        reset_time = utc_now() + timedelta(seconds=duration)
        async with get_db_session() as session:
            stmt = select(Account).where(Account.id == account_id)
            res = await session.execute(stmt)
            acc = res.scalar_one_or_none()
            if acc:
                acc.status = "rate_limited"
                acc.rate_limit_reset_at = reset_time
                acc.error_count += 1
                session.add(acc)
                await session.commit()
                logger.warning(f"Account {acc.username or acc.id} rate limited until {reset_time.isoformat()}")

    async def mark_invalid(self, account_id: int, reason: str = "Auth token expired or invalid") -> None:
        """Mark account credentials as invalid."""
        async with get_db_session() as session:
            stmt = select(Account).where(Account.id == account_id)
            res = await session.execute(stmt)
            acc = res.scalar_one_or_none()
            if acc:
                acc.status = "invalid"
                acc.error_count += 1
                session.add(acc)
                await session.commit()
                logger.error(f"Account {acc.username or acc.id} marked INVALID: {reason}")

    async def mark_success(self, account_id: int) -> None:
        """Record successful request for account."""
        async with get_db_session() as session:
            stmt = select(Account).where(Account.id == account_id)
            res = await session.execute(stmt)
            acc = res.scalar_one_or_none()
            if acc:
                acc.success_count += 1
                acc.last_used_at = utc_now()
                session.add(acc)
                await session.commit()

    async def get_all_accounts(self) -> List[Account]:
        """Fetch all accounts."""
        async with get_db_session() as session:
            stmt = select(Account).order_by(Account.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def delete_account(self, account_id: int) -> bool:
        """Delete an account from the pool."""
        async with get_db_session() as session:
            stmt = select(Account).where(Account.id == account_id)
            res = await session.execute(stmt)
            acc = res.scalar_one_or_none()
            if acc:
                await session.delete(acc)
                await session.commit()
                return True
            return False

    async def import_from_json(self, raw_json: Union[str, List[Dict[str, Any]]]) -> int:
        """
        Import accounts from JSON:
        Supports either:
        - List of objects with {"auth_token": "...", "ct0": "...", "username": "..."}
        - Array of browser cookies with name/value pairs
        """
        if isinstance(raw_json, str):
            data = json.loads(raw_json)
        else:
            data = raw_json

        imported_count = 0
        if isinstance(data, list):
            # Check if it's cookie array or account list
            has_auth_token_field = any("auth_token" in item for item in data if isinstance(item, dict))
            if has_auth_token_field:
                for item in data:
                    if isinstance(item, dict) and "auth_token" in item and "ct0" in item:
                        await self.add_or_update_account(
                            auth_token=item["auth_token"],
                            ct0=item["ct0"],
                            username=item.get("username")
                        )
                        imported_count += 1
            else:
                # Treat as list of cookies exported from browser
                cookies_map = {item.get("name"): item.get("value") for item in data if isinstance(item, dict)}
                if "auth_token" in cookies_map and "ct0" in cookies_map:
                    await self.add_or_update_account(
                        auth_token=cookies_map["auth_token"],
                        ct0=cookies_map["ct0"]
                    )
                    imported_count += 1

        elif isinstance(data, dict):
            if "auth_token" in data and "ct0" in data:
                await self.add_or_update_account(
                    auth_token=data["auth_token"],
                    ct0=data["ct0"],
                    username=data.get("username")
                )
                imported_count += 1

        return imported_count

    async def import_from_netscape(self, cookies_text: str) -> int:
        """Parse Netscape/cookies.txt format string."""
        cookies = {}
        for line in cookies_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name = parts[5]
                value = parts[6]
                cookies[name] = value

        if "auth_token" in cookies and "ct0" in cookies:
            await self.add_or_update_account(
                auth_token=cookies["auth_token"],
                ct0=cookies["ct0"]
            )
            return 1
        return 0
    async def import_from_cookie_header(self, raw_header: str, username: Optional[str] = None) -> Optional[Account]:
        """Parse raw Cookie string like 'guest_id=...; auth_token=...; ct0=...; twid=...'."""
        cookies = {}
        for part in raw_header.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip().strip('"')

        auth_token = cookies.get("auth_token")
        ct0 = cookies.get("ct0")

        if auth_token and ct0:
            return await self.add_or_update_account(
                auth_token=auth_token,
                ct0=ct0,
                username=username
            )
        return None


account_pool = AccountPool()
