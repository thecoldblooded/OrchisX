import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional, List, Dict, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select, update
from core.database import get_db_session
from core.models import Monitor, utc_now
from engine.webhook import webhook_dispatcher
from scraper.twitter_graphql import twitter_client

logger = logging.getLogger("orchis.monitor")


class MonitorScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._is_running = False

    async def start(self) -> None:
        """Start the background monitor scheduler and load active monitors from DB."""
        if self._is_running:
            return
        self.scheduler.start()
        self._is_running = True
        logger.info("Monitor scheduler started")
        await self.sync_all_monitors()

    async def shutdown(self) -> None:
        """Stop the background monitor scheduler."""
        if self._is_running:
            self.scheduler.shutdown(wait=False)
            self._is_running = False
            logger.info("Monitor scheduler stopped")

    async def sync_all_monitors(self) -> None:
        """Load all active monitors from database and register their recurring jobs."""
        async with get_db_session() as session:
            stmt = select(Monitor).where(Monitor.status == "active")
            res = await session.execute(stmt)
            monitors = list(res.scalars().all())

        for mon in monitors:
            self._schedule_job(mon)

        logger.info(f"Registered {len(monitors)} active monitors in scheduler")

    def _schedule_job(self, monitor: Monitor) -> None:
        """Register or replace a scheduled job for a monitor."""
        job_id = f"monitor_{monitor.id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        self.scheduler.add_job(
            self._run_monitor_job,
            "interval",
            seconds=max(30, monitor.interval_seconds),
            id=job_id,
            args=[monitor.id],
            replace_existing=True
        )

    def remove_monitor_job(self, monitor_id: str) -> None:
        """Remove a monitor job from the scheduler."""
        job_id = f"monitor_{monitor_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    async def register_or_update(self, monitor: Monitor) -> None:
        """Add or update a monitor and schedule if active."""
        if monitor.status == "active":
            self._schedule_job(monitor)
        else:
            self.remove_monitor_job(monitor.id)

    async def _run_monitor_job(self, monitor_id: str) -> None:
        """Executes one polling cycle for a monitor."""
        async with get_db_session() as session:
            stmt = select(Monitor).where(Monitor.id == monitor_id)
            res = await session.execute(stmt)
            monitor = res.scalar_one_or_none()

            if not monitor or monitor.status != "active":
                return

        logger.info(f"Checking monitor {monitor.name} ({monitor.query})")

        try:
            if monitor.monitor_type == "user_timeline":
                resp = await twitter_client.get_user_tweets(username=monitor.query, limit=20)
            else:
                resp = await twitter_client.search_tweets(query=monitor.query, limit=20, query_type="Latest")

            tweets = resp.get("tweets", [])
            new_tweets = []

            # Filter for tweets newer than last_tweet_id
            if monitor.last_tweet_id:
                for t in tweets:
                    # Tweet IDs are snowflake IDs: larger integer string = newer tweet
                    try:
                        if int(t["id"]) > int(monitor.last_tweet_id):
                            new_tweets.append(t)
                    except ValueError:
                        if t["id"] != monitor.last_tweet_id:
                            new_tweets.append(t)
            else:
                # First run: capture latest tweets as baseline without spamming webhook
                if tweets:
                    newest_id = str(max([int(t["id"]) for t in tweets if t["id"].isdigit()] or [tweets[0]["id"]]))
                    async with get_db_session() as session:
                        stmt = select(Monitor).where(Monitor.id == monitor_id)
                        r = await session.execute(stmt)
                        m = r.scalar_one_or_none()
                        if m:
                            m.last_tweet_id = newest_id
                            m.last_run_at = utc_now()
                            session.add(m)
                            await session.commit()
                    return

            if new_tweets:
                logger.info(f"Monitor {monitor.name} found {len(new_tweets)} new tweets! Dispatching webhook...")
                await webhook_dispatcher.dispatch(
                    monitor_id=monitor.id,
                    webhook_url=monitor.webhook_url,
                    webhook_secret=monitor.webhook_secret,
                    event_type="tweet.new",
                    payload_data={
                        "monitor_id": monitor.id,
                        "monitor_name": monitor.name,
                        "query": monitor.query,
                        "new_tweets_count": len(new_tweets),
                        "tweets": new_tweets
                    }
                )

                newest_id = str(max([int(t["id"]) for t in new_tweets if t["id"].isdigit()] or [new_tweets[0]["id"]]))
                async with get_db_session() as session:
                    stmt = select(Monitor).where(Monitor.id == monitor_id)
                    r = await session.execute(stmt)
                    m = r.scalar_one_or_none()
                    if m:
                        m.last_tweet_id = newest_id
                        m.last_run_at = utc_now()
                        session.add(m)
                        await session.commit()
            else:
                async with get_db_session() as session:
                    stmt = select(Monitor).where(Monitor.id == monitor_id)
                    r = await session.execute(stmt)
                    m = r.scalar_one_or_none()
                    if m:
                        m.last_run_at = utc_now()
                        session.add(m)
                        await session.commit()

        except Exception as e:
            logger.error(f"Error during monitor execution for {monitor_id}: {e}")


monitor_scheduler = MonitorScheduler()
