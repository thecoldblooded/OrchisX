from typing import List
from fastapi import APIRouter, HTTPException, Path
from sqlmodel import select
from api.schemas import (
    AddAccountRequest, AccountResponse, ProxyResponse, EngineHealthResponse
)
from pool.account_pool import account_pool
from pool.proxy_pool import proxy_pool
from core.database import get_db_session
from core.models import Account, Proxy, Monitor

router = APIRouter(prefix="/api/v1", tags=["Pool & Health"])


@router.post("/accounts", response_model=AccountResponse)
async def add_account(req: AddAccountRequest):
    """Add or update Twitter account auth tokens in the pool."""
    if req.cookie_string:
        account = await account_pool.import_from_cookie_header(req.cookie_string, req.username)
        if not account:
            raise HTTPException(status_code=400, detail="Could not extract auth_token and ct0 from provided cookie string")
    elif req.auth_token and req.ct0:
        account = await account_pool.add_or_update_account(
            auth_token=req.auth_token,
            ct0=req.ct0,
            username=req.username,
        )
    else:
        raise HTTPException(status_code=400, detail="Must provide either cookie_string or both auth_token and ct0")

    return AccountResponse(
        id=account.id,
        username=account.username,
        status=account.status,
        rate_limit_reset_at=account.rate_limit_reset_at,
        success_count=account.success_count,
        error_count=account.error_count,
        last_used_at=account.last_used_at,
        created_at=account.created_at,
    )


@router.get("/accounts", response_model=List[AccountResponse])
async def list_accounts():
    """List all accounts and their rate limit / error status."""
    accounts = await account_pool.get_all_accounts()
    return [
        AccountResponse(
            id=acc.id,
            username=acc.username,
            status=acc.status,
            rate_limit_reset_at=acc.rate_limit_reset_at,
            success_count=acc.success_count,
            error_count=acc.error_count,
            last_used_at=acc.last_used_at,
            created_at=acc.created_at,
        )
        for acc in accounts
    ]


@router.delete("/accounts/{id}")
async def delete_account(id: int = Path(..., description="Account ID")):
    """Remove an account from the pool."""
    deleted = await account_pool.delete_account(id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"success": True, "message": f"Account {id} removed"}


@router.get("/proxies", response_model=List[ProxyResponse])
async def list_proxies():
    """List all proxies with latency and error metrics."""
    proxies = await proxy_pool.get_all_proxies()
    return [
        ProxyResponse(
            id=p.id,
            url=p.url,
            ip=p.ip,
            port=p.port,
            status=p.status,
            latency_ms=p.latency_ms,
            error_count=p.error_count,
            success_count=p.success_count,
            last_checked_at=p.last_checked_at,
            last_used_at=p.last_used_at,
        )
        for p in proxies
    ]


@router.post("/proxies/check")
async def check_proxies():
    """Trigger parallel health checks across all configured proxies."""
    results = await proxy_pool.check_all_proxies()
    return {"checked_count": len(results), "results": results}


@router.get("/health", response_model=EngineHealthResponse)
async def get_health():
    """Overall engine health and status summary."""
    async with get_db_session() as session:
        # Accounts
        acc_stmt = select(Account)
        acc_res = await session.execute(acc_stmt)
        accounts = list(acc_res.scalars().all())

        # Proxies
        prx_stmt = select(Proxy)
        prx_res = await session.execute(prx_stmt)
        proxies = list(prx_res.scalars().all())

        # Monitors
        mon_stmt = select(Monitor).where(Monitor.status == "active")
        mon_res = await session.execute(mon_stmt)
        monitors = list(mon_res.scalars().all())

    active_acc = sum(1 for a in accounts if a.status == "active")
    rate_limited_acc = sum(1 for a in accounts if a.status == "rate_limited")
    invalid_acc = sum(1 for a in accounts if a.status == "invalid")
    active_prx = sum(1 for p in proxies if p.status == "active")
    failing_prx = sum(1 for p in proxies if p.status == "failing")

    return EngineHealthResponse(
        status="healthy" if (active_prx > 0 or len(proxies) == 0) else "degraded",
        active_accounts=active_acc,
        rate_limited_accounts=rate_limited_acc,
        invalid_accounts=invalid_acc,
        active_proxies=active_prx,
        failing_proxies=failing_prx,
        active_monitors=len(monitors),
    )
