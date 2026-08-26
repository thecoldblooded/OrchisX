from typing import List, Optional
from fastapi import APIRouter, HTTPException, Path, Header
from sqlmodel import select, delete
from api.schemas import (
    AddAccountRequest, AccountResponse, AddProxyRequest, ProxyResponse, EngineHealthResponse
)
from pool.account_pool import account_pool
from pool.proxy_pool import proxy_pool
from core.database import get_db_session
from core.models import Account, Proxy, Monitor, ExtractionJob

router = APIRouter(prefix="/api/v1", tags=["Pool & Health"])


@router.post("/accounts", response_model=AccountResponse)
async def add_account(
    req: AddAccountRequest,
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """Add or update Twitter account auth tokens in the pool."""
    if req.cookie_string:
        account = await account_pool.import_from_cookie_header(
            req.cookie_string,
            req.username,
            session_id=x_session_id
        )
        if not account:
            raise HTTPException(status_code=400, detail="Could not extract auth_token and ct0 from provided cookie string")
    elif req.auth_token and req.ct0:
        account = await account_pool.add_or_update_account(
            auth_token=req.auth_token,
            ct0=req.ct0,
            username=req.username,
            session_id=x_session_id
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
async def list_accounts(
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """List accounts belonging to the current session."""
    accounts = await account_pool.get_all_accounts(session_id=x_session_id)
    return [
        AccountResponse(
            id=a.id,
            username=a.username,
            status=a.status,
            rate_limit_reset_at=a.rate_limit_reset_at,
            success_count=a.success_count,
            error_count=a.error_count,
            last_used_at=a.last_used_at,
            created_at=a.created_at,
        )
        for a in accounts
    ]


@router.delete("/accounts/{id}")
async def delete_account(
    id: int = Path(..., description="Account ID"),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """Remove an account from the pool."""
    deleted = await account_pool.delete_account(id, session_id=x_session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Account {id} not found")
    return {"success": True, "message": f"Account {id} removed"}


@router.get("/proxies", response_model=List[ProxyResponse])
async def list_proxies(
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """List proxies belonging to the current session."""
    proxies = await proxy_pool.get_all_proxies(session_id=x_session_id)
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


@router.post("/proxies")
async def add_proxies(
    req: AddProxyRequest,
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """Add single or bulk proxies into the proxy pool for this session."""
    count = await proxy_pool.import_from_text(req.proxies, session_id=x_session_id)
    if count == 0:
        raise HTTPException(status_code=400, detail="Geçerli bir proxy formatı bulunamadı. (Örn: ip:port:user:pass veya protocol://user:pass@ip:port)")
    return {"success": True, "added_count": count, "message": f"{count} proxy havuza eklendi."}


@router.delete("/proxies/{id}")
async def delete_proxy(
    id: int = Path(..., description="Proxy ID"),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """Remove a proxy from the pool."""
    success = await proxy_pool.remove_proxy(id, session_id=x_session_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Proxy {id} bulunamadı.")
    return {"success": True, "message": f"Proxy {id} silindi."}


@router.post("/proxies/check")
async def check_proxies(
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """Trigger parallel health checks across all configured proxies in session."""
    results = await proxy_pool.check_all_proxies(session_id=x_session_id)
    return {"checked_count": len(results), "results": results}


@router.get("/health", response_model=EngineHealthResponse)
async def get_health(
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """Overall engine health and status summary scoped to session."""
    try:
        async with get_db_session() as session:
            # Accounts count
            acc_stmt = select(Account)
            if x_session_id:
                acc_stmt = acc_stmt.where(Account.session_id == x_session_id)
            res_acc = await session.execute(acc_stmt)
            accounts = list(res_acc.scalars().all())

            # Proxies count
            proxy_stmt = select(Proxy)
            if x_session_id:
                proxy_stmt = proxy_stmt.where(Proxy.session_id == x_session_id)
            res_proxy = await session.execute(proxy_stmt)
            proxies = list(res_proxy.scalars().all())

            # Monitors count
            mon_stmt = select(Monitor)
            if x_session_id:
                mon_stmt = mon_stmt.where(Monitor.session_id == x_session_id)
            res_mon = await session.execute(mon_stmt)
            monitors = list(res_mon.scalars().all())

            active_accounts = sum(1 for a in accounts if a.status == "active")
            rate_limited_accounts = sum(1 for a in accounts if a.status == "rate_limited")
            invalid_accounts = sum(1 for a in accounts if a.status == "invalid")
            active_proxies = sum(1 for p in proxies if p.status == "active")
            failing_proxies = sum(1 for p in proxies if p.status == "failing")
            active_monitors = sum(1 for m in monitors if m.status == "active")

            return EngineHealthResponse(
                status="healthy",
                active_accounts=active_accounts,
                rate_limited_accounts=rate_limited_accounts,
                invalid_accounts=invalid_accounts,
                active_proxies=active_proxies,
                failing_proxies=failing_proxies,
                active_monitors=active_monitors,
            )
    except Exception:
        return EngineHealthResponse(
            status="healthy",
            active_accounts=0,
            rate_limited_accounts=0,
            invalid_accounts=0,
            active_proxies=0,
            failing_proxies=0,
            active_monitors=0,
        )


@router.post("/session/reset")
async def reset_session(
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """Delete all accounts, proxies, jobs, and monitors belonging to this session."""
    if not x_session_id:
        return {"success": True, "message": "No session ID specified"}

    acc_count = await account_pool.delete_all_by_session(x_session_id)
    proxy_count = await proxy_pool.delete_all_by_session(x_session_id)

    async with get_db_session() as session:
        # Delete extraction jobs
        job_stmt = select(ExtractionJob).where(ExtractionJob.session_id == x_session_id)
        res_jobs = await session.execute(job_stmt)
        for job in res_jobs.scalars().all():
            await session.delete(job)

        # Delete monitors
        mon_stmt = select(Monitor).where(Monitor.session_id == x_session_id)
        res_mon = await session.execute(mon_stmt)
        for mon in res_mon.scalars().all():
            await session.delete(mon)

        await session.commit()

    return {
        "success": True,
        "message": "Session data cleared successfully.",
        "cleared": {
            "accounts": acc_count,
            "proxies": proxy_count,
        }
    }
