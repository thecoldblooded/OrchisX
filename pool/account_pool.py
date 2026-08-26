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
        username: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Account:
        """Add a new Twitter account or update existing credentials."""
        async with self._lock:
            async with get_db_session() as session:
                stmt = select(Account).where(Account.auth_token == auth_token)
                res = await session.execute(stmt)
                account = res.scalars().first()

                if account:
                    account.ct0 = ct0
                    if username:
                        account.username = username
                    if session_id:
                        account.session_id = session_id
                    account.status = "active"
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
                        session_id=session_id,
                        status="active"
                    )
                    session.add(account)
                    await session.commit()
                    await session.refresh(account)
                    logger.info(f"Added new account {account.username or account.id}")
                    return account

    async def get_active_account(self, session_id: Optional[str] = None) -> Optional[Account]:
        """
        Get the next available active account, rotating by last_used_at.
        Recovers rate-limited accounts whose reset time has expired.
        """
        async with self._lock:
            async with get_db_session() as session:
                now = utc_now()
                # 1. Recover expired rate limits
                recovery_stmt = select(Account).where(
                    Account.status == "rate_limited",
                    Account.rate_limit_reset_at <= now
                )
                if session_id:
                    recovery_stmt = recovery_stmt.where(Account.session_id == session_id)
                rec_res = await session.execute(recovery_stmt)
                for acc in rec_res.scalars().all():
                    acc.status = "active"
                    acc.rate_limit_reset_at = None
                    session.add(acc)
                    logger.info(f"Account {acc.username or acc.id} rate limit expired; restored to active.")
                await session.commit()

                # 2. Select next active account
                stmt = select(Account).where(Account.status == "active")
                if session_id:
                    stmt = stmt.where(Account.session_id == session_id)
                stmt = stmt.order_by(Account.last_used_at.asc().nullsfirst(), Account.id.asc()).limit(1)

                res = await session.execute(stmt)
                account = res.scalars().first()
                if account:
                    account.last_used_at = now
                    session.add(account)
                    await session.commit()
                    await session.refresh(account)
                    return account
                return None

    async def get_next_reset_seconds(self, session_id: Optional[str] = None) -> int:
        """Return seconds remaining until the earliest rate-limited account resets."""
        async with get_db_session() as session:
            stmt = select(Account).where(Account.status == "rate_limited")
            if session_id:
                stmt = stmt.where(Account.session_id == session_id)
            stmt = stmt.order_by(Account.rate_limit_reset_at.asc()).limit(1)
            res = await session.execute(stmt)
            acc = res.scalars().first()
            if acc and acc.rate_limit_reset_at:
                now = utc_now()
                if acc.rate_limit_reset_at > now:
                    delta = int((acc.rate_limit_reset_at - now).total_seconds())
                    return max(5, delta + 2)
        return 30

    async def has_active_account(self, session_id: Optional[str] = None) -> bool:
        """Check if any active account is currently available without modifying last_used_at."""
        async with get_db_session() as session:
            stmt = select(Account).where(Account.status == "active")
            if session_id:
                stmt = stmt.where(Account.session_id == session_id)
            res = await session.execute(stmt.limit(1))
            return res.scalars().first() is not None

    async def mark_rate_limited(self, account_id: int, reset_seconds: Optional[int] = None) -> None:
        """Mark account as rate limited for reset_seconds (default 15m)."""
        duration = reset_seconds or settings.RATE_LIMIT_COOLDOWN_SECONDS
        reset_time = utc_now() + timedelta(seconds=duration)
        async with get_db_session() as session:
            stmt = select(Account).where(Account.id == account_id)
            res = await session.execute(stmt)
            acc = res.scalars().first()
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
            acc = res.scalars().first()
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
            acc = res.scalars().first()
            if acc:
                acc.success_count += 1
                session.add(acc)
                await session.commit()

    async def get_all_accounts(self, session_id: Optional[str] = None) -> List[Account]:
        """Fetch all accounts."""
        async with get_db_session() as session:
            stmt = select(Account)
            if session_id:
                stmt = stmt.where(Account.session_id == session_id)
            stmt = stmt.order_by(Account.id)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def delete_account(self, account_id: int, session_id: Optional[str] = None) -> bool:
        """Delete an account from the pool."""
        async with get_db_session() as session:
            stmt = select(Account).where(Account.id == account_id)
            if session_id:
                stmt = stmt.where(Account.session_id == session_id)
            res = await session.execute(stmt)
            acc = res.scalars().first()
            if acc:
                await session.delete(acc)
                await session.commit()
                return True
            return False

    async def delete_all_by_session(self, session_id: str) -> int:
        """Delete all accounts belonging to a session."""
        if not session_id:
            return 0
        async with get_db_session() as session:
            stmt = select(Account).where(Account.session_id == session_id)
            res = await session.execute(stmt)
            accounts = res.scalars().all()
            count = len(accounts)
            for a in accounts:
                await session.delete(a)
            await session.commit()
            return count

    async def import_from_cookie_header(
        self,
        raw_header: str,
        username: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Optional[Account]:
        """Parse raw Cookie string like 'guest_id=...; auth_token=...; ct0=...; twid=...'."""
        auth_token = None
        ct0 = None
        for part in raw_header.split(";"):
            part = part.strip()
            if part.startswith("auth_token="):
                auth_token = part.split("auth_token=")[1].strip()
            elif part.startswith("ct0="):
                ct0 = part.split("ct0=")[1].strip()

        if auth_token and ct0:
            return await self.add_or_update_account(
                auth_token=auth_token,
                ct0=ct0,
                username=username,
                session_id=session_id
            )
        return None


account_pool = AccountPool()
