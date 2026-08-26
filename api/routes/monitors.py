import secrets
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Path, Header
from sqlmodel import select, delete
from api.schemas import CreateMonitorRequest, MonitorResponse
from core.database import get_db_session
from core.models import Monitor
from engine.monitor import monitor_scheduler

router = APIRouter(prefix="/api/v1/monitors", tags=["Monitors & Webhooks"])


@router.post("", response_model=MonitorResponse)
async def create_monitor(
    req: CreateMonitorRequest,
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """
    Create a new keyword or account monitor with scheduled polling and HMAC-signed webhooks.
    """
    secret = req.webhook_secret or secrets.token_hex(16)
    monitor = Monitor(
        name=req.name,
        query=req.query,
        monitor_type=req.monitor_type,
        interval_seconds=req.interval_seconds,
        webhook_url=req.webhook_url,
        webhook_secret=secret,
        status="active",
        session_id=x_session_id,
    )
    async with get_db_session() as session:
        session.add(monitor)
        await session.commit()
        await session.refresh(monitor)

    await monitor_scheduler.register_or_update(monitor)

    return MonitorResponse(
        id=monitor.id,
        name=monitor.name,
        query=monitor.query,
        monitor_type=monitor.monitor_type,
        interval_seconds=monitor.interval_seconds,
        webhook_url=monitor.webhook_url,
        webhook_secret=monitor.webhook_secret,
        status=monitor.status,
        last_run_at=monitor.last_run_at,
        last_tweet_id=monitor.last_tweet_id,
        created_at=monitor.created_at,
    )


@router.get("", response_model=List[MonitorResponse])
async def list_monitors(
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """List all registered keyword and timeline monitors for this session."""
    async with get_db_session() as session:
        stmt = select(Monitor)
        if x_session_id:
            stmt = stmt.where(Monitor.session_id == x_session_id)
        stmt = stmt.order_by(Monitor.created_at.desc())
        res = await session.execute(stmt)
        monitors = list(res.scalars().all())
    return [
        MonitorResponse(
            id=m.id,
            name=m.name,
            query=m.query,
            monitor_type=m.monitor_type,
            interval_seconds=m.interval_seconds,
            webhook_url=m.webhook_url,
            webhook_secret=m.webhook_secret,
            status=m.status,
            last_run_at=m.last_run_at,
            last_tweet_id=m.last_tweet_id,
            created_at=m.created_at,
        )
        for m in monitors
    ]


@router.delete("/{id}")
async def delete_monitor(id: str = Path(..., description="Monitor ID")):
    """Delete a monitor and cancel its background scheduled job."""
    monitor_scheduler.remove_monitor_job(id)
    async with get_db_session() as session:
        stmt = select(Monitor).where(Monitor.id == id)
        res = await session.execute(stmt)
        monitor = res.scalar_one_or_none()
        if not monitor:
            raise HTTPException(status_code=404, detail="Monitor not found")
        await session.delete(monitor)
        await session.commit()

    return {"success": True, "message": f"Monitor {id} deleted"}
