import asyncio
import hashlib
import hmac
import json
import pytest
from core.database import init_db, get_db_session
from core.models import Monitor, WebhookLog
from engine.webhook import webhook_dispatcher
from engine.monitor import monitor_scheduler
from sqlmodel import select


@pytest.fixture(autouse=True, scope="module")
def setup_db():
    asyncio.run(init_db())

@pytest.mark.asyncio
async def test_webhook_dispatch_and_audit():
    import uuid
    monitor_id = f"test-mon-{uuid.uuid4()}"
    webhook_secret = "super_secret_webhook_key_456"
    payload_data = {
        "tweet_id": "1894000000000000001",
        "author": "elonmusk",
        "text": "Testing webhook delivery engine"
    }

    # Dispatch to dummy URL (will fail network, but execute retry + audit log in DB)
    success = await webhook_dispatcher.dispatch(
        monitor_id=monitor_id,
        webhook_url="http://127.0.0.1:59999/webhook-mock",
        webhook_secret=webhook_secret,
        event_type="tweet.new",
        payload_data=payload_data
    )

    # Check that delivery attempt was audited in DB
    async with get_db_session() as session:
        stmt = select(WebhookLog).where(WebhookLog.monitor_id == monitor_id)
        res = await session.execute(stmt)
        log_entry = res.scalar_one_or_none()

        assert log_entry is not None
        assert log_entry.monitor_id == monitor_id
        assert log_entry.event_type == "tweet.new"
        assert log_entry.attempt >= 1
        assert "Testing webhook delivery engine" in log_entry.payload


@pytest.mark.asyncio
async def test_monitor_scheduler_lifecycle():
    await monitor_scheduler.start()
    assert monitor_scheduler._is_running is True

    mon = Monitor(
        id="test-scheduler-mon-1",
        name="Crypto Alerts",
        query="bitcoin OR eth",
        interval_seconds=60,
        webhook_url="https://example.com/webhook",
        status="active"
    )

    await monitor_scheduler.register_or_update(mon)
    job = monitor_scheduler.scheduler.get_job(f"monitor_{mon.id}")
    assert job is not None

    # Pause / remove
    mon.status = "paused"
    await monitor_scheduler.register_or_update(mon)
    job_paused = monitor_scheduler.scheduler.get_job(f"monitor_{mon.id}")
    assert job_paused is None

    await monitor_scheduler.shutdown()
    assert monitor_scheduler._is_running is False
