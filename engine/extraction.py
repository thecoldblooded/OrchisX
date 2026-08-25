import asyncio
import csv
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from pathlib import Path
from typing import Optional, List, Dict, Any

import aiofiles
from sqlmodel import select, update, delete
from config import settings
from core.database import get_db_session
from core.models import ExtractionJob, utc_now
from pool.account_pool import account_pool
from scraper.filters import TweetFilter, build_twitter_query, matches_filter
from scraper.twitter_graphql import twitter_client
logger = logging.getLogger("orchis.extraction")

class ExtractionService:
    def __init__(self):
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._stop_flags: Dict[str, str] = {}  # job_id -> "pause" | "cancel"
        self._scheduler_task: Optional[asyncio.Task] = None
        os.makedirs(settings.EXPORTS_DIR, exist_ok=True)

    async def start_scheduler(self):
        """Start background scheduler for auto-resuming rate-limited jobs."""
        if not self._scheduler_task or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._auto_resume_worker())
            logger.info("Extraction auto-resume scheduler started.")

    async def stop_scheduler(self):
        """Stop auto-resume scheduler on app shutdown."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()

    async def _auto_resume_worker(self):
        """Periodic loop checking for paused jobs whose auto_resume_at has arrived."""
        while True:
            try:
                await asyncio.sleep(5)
                now = utc_now()
                async with get_db_session() as session:
                    stmt = select(ExtractionJob).where(
                        ExtractionJob.status == "paused",
                        ExtractionJob.auto_resume_at != None
                    )
                    res = await session.execute(stmt)
                    paused_jobs = res.scalars().all()

                    for j in paused_jobs:
                        resume_at = j.auto_resume_at
                        if resume_at.tzinfo is None:
                            resume_at = resume_at.replace(tzinfo=timezone.utc)

                        # Check if cooldown passed or active accounts exist
                        has_account = await account_pool.has_active_account()
                        if now >= resume_at or has_account:
                            logger.info(f"Auto-resuming job {j.id} (query: {j.query})...")
                            j.auto_resume_at = None
                            j.error_message = None
                            session.add(j)
                            await session.commit()
                            asyncio.create_task(self.resume_job(j.id))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in auto-resume worker: {e}")
                await asyncio.sleep(5)

    def _get_checkpoint_path(self, job_id: str) -> str:
        return os.path.join(settings.EXPORTS_DIR, f"checkpoint_{job_id}.json")

    async def create_job(
        self,
        query: str,
        results_limit: int = 100,
        tool_type: str = "search",
        export_format: str = "csv",
        filters: Optional[TweetFilter] = None
    ) -> ExtractionJob:
        """Create a new extraction job in the database and spawn background execution."""
        filters_json = filters.model_dump_json() if filters else None
        async with get_db_session() as session:
            job = ExtractionJob(
                tool_type=tool_type,
                query=query.strip(),
                results_limit=results_limit,
                format=export_format.lower(),
                status="queued",
                filters_json=filters_json,
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)

        self._stop_flags[job.id] = ""
        task = asyncio.create_task(self._run_extraction(job.id, filters))
        self._running_tasks[job.id] = task
        return job

    async def get_job(self, job_id: str) -> Optional[ExtractionJob]:
        """Fetch job status from database."""
        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def list_jobs(self, limit: int = 50) -> List[ExtractionJob]:
        """List recent extraction jobs."""
        async with get_db_session() as session:
            stmt = select(ExtractionJob).order_by(ExtractionJob.created_at.desc()).limit(limit)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def pause_job(self, job_id: str) -> bool:
        """Signal job to pause gracefully at the current cursor."""
        self._stop_flags[job_id] = "pause"
        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()
            if job and job.status == "running":
                job.status = "paused"
                job.auto_resume_at = None
                job.error_message = "Kullanıcı tarafından duraklatıldı."
                job.updated_at = utc_now()
                session.add(job)
                await session.commit()
                logger.info(f"Job {job_id} marked as paused")
                return True
        return False
    async def resume_job(self, job_id: str, filters: Optional[TweetFilter] = None) -> bool:
        """Resume a job from its saved cursor and existing items."""
        job = await self.get_job(job_id)
        if not job or job.status == "completed":
            return False
        self._stop_flags[job_id] = ""

        if filters is None and job.filters_json:
            try:
                filters = TweetFilter.model_validate_json(job.filters_json)
            except Exception:
                pass

        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            db_job = res.scalar_one_or_none()
            if db_job:
                db_job.status = "running"
                db_job.updated_at = utc_now()
                session.add(db_job)
                await session.commit()

        task = asyncio.create_task(self._run_extraction(job_id, filters))
        self._running_tasks[job_id] = task
        logger.info(f"Job {job_id} resumed from cursor {job.cursor}")
        return True

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel an active or paused job."""
        self._stop_flags[job_id] = "cancel"
        if job_id in self._running_tasks:
            self._running_tasks[job_id].cancel()

        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()
            if job:
                job.status = "canceled"
                job.auto_resume_at = None
                job.updated_at = utc_now()
                session.add(job)
                await session.commit()
                return True
        return False

    async def retry_job(self, job_id: str) -> bool:
        """Restart a failed or canceled job from scratch."""
        # Clean checkpoint if any
        ckpt = self._get_checkpoint_path(job_id)
        if os.path.exists(ckpt):
            try:
                os.remove(ckpt)
            except Exception:
                pass

        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()
            if job:
                job.status = "running"
                job.collected_count = 0
                job.cursor = None
                job.error_message = None
                job.auto_resume_at = None
                job.output_file_path = None
                job.completed_at = None
                job.updated_at = utc_now()
                session.add(job)
                await session.commit()

        self._stop_flags[job_id] = ""
        task = asyncio.create_task(self._run_extraction(job_id))
        self._running_tasks[job_id] = task
        logger.info(f"Job {job_id} restarted")
        return True

    async def delete_job(self, job_id: str) -> bool:
        """Delete job record and any output files."""
        job = await self.get_job(job_id)
        if not job:
            return False

        if job.id in self._running_tasks:
            self._running_tasks[job.id].cancel()

        ckpt = self._get_checkpoint_path(job_id)
        if os.path.exists(ckpt):
            try:
                os.remove(ckpt)
            except Exception:
                pass

        if job.output_file_path and os.path.exists(job.output_file_path):
            try:
                os.remove(job.output_file_path)
            except Exception:
                pass

        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            db_job = res.scalar_one_or_none()
            if db_job:
                await session.delete(db_job)
                await session.commit()
                return True
        return False

    async def _run_extraction(self, job_id: str, filters: Optional[TweetFilter] = None) -> None:
        """Background worker for large bulk extraction with checkpointing and pause/resume support."""
        logger.info(f"Executing extraction worker for job {job_id}")

        try:
            async with get_db_session() as session:
                stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                res = await session.execute(stmt)
                job = res.scalar_one_or_none()
                if not job:
                    return
                job.status = "running"
                job.updated_at = utc_now()
                session.add(job)
                await session.commit()

            # Rebuild filter if not passed directly
            if filters is None and job.filters_json:
                try:
                    filters = TweetFilter.model_validate_json(job.filters_json)
                except Exception:
                    pass

            collected_items = []
            seen_ids = set()
            current_cursor = job.cursor
            is_user_list = job.tool_type in ("user_followers", "user_following")
            ckpt_path = self._get_checkpoint_path(job_id)

            # Restore from checkpoint if resuming
            if os.path.exists(ckpt_path):
                try:
                    with open(ckpt_path, "r", encoding="utf-8") as f:
                        collected_items = json.load(f)
                    seen_ids = {item["id"] for item in collected_items if "id" in item}
                    logger.info(f"Loaded {len(collected_items)} items from checkpoint for job {job_id}")
                except Exception as e:
                    logger.warning(f"Failed to read checkpoint for job {job_id}: {e}")

            consecutive_empty_batches = 0
            stopped_early = False

            while len(collected_items) < job.results_limit:
                # Check stop flags
                flag = self._stop_flags.get(job_id, "")
                if flag == "pause":
                    logger.info(f"Job {job_id} pausing by request at {len(collected_items)} items.")
                    async with get_db_session() as session:
                        stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                        r = await session.execute(stmt)
                        j = r.scalar_one_or_none()
                        if j:
                            j.status = "paused"
                            j.collected_count = len(collected_items)
                            j.cursor = current_cursor
                            j.updated_at = utc_now()
                            session.add(j)
                            await session.commit()
                    return

                elif flag == "cancel":
                    logger.info(f"Job {job_id} canceled by request.")
                    async with get_db_session() as session:
                        stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                        r = await session.execute(stmt)
                        j = r.scalar_one_or_none()
                        if j:
                            j.status = "canceled"
                            j.updated_at = utc_now()
                            session.add(j)
                            await session.commit()
                    return

                batch_limit = min(50, job.results_limit - len(collected_items) + 10)

                if job.tool_type == "user_followers":
                    res = await twitter_client.get_user_followers(
                        username=job.query,
                        limit=batch_limit,
                        cursor=current_cursor
                    )
                    items = res.get("users", [])
                elif job.tool_type == "user_following":
                    res = await twitter_client.get_user_following(
                        username=job.query,
                        limit=batch_limit,
                        cursor=current_cursor
                    )
                    items = res.get("users", [])
                elif job.tool_type == "user_tweets":
                    res = await twitter_client.get_user_tweets(
                        username=job.query,
                        limit=batch_limit,
                        cursor=current_cursor
                    )
                    items = res.get("tweets", [])
                else:
                    res = await twitter_client.search_tweets(
                        query=job.query,
                        limit=batch_limit,
                        cursor=current_cursor,
                        filters=filters
                    )
                    items = res.get("tweets", [])

                next_cursor = res.get("next_cursor")
                new_count = 0
                for item in items:
                    if item["id"] not in seen_ids:
                        seen_ids.add(item["id"])
                        collected_items.append(item)
                        new_count += 1
                        if len(collected_items) >= job.results_limit:
                            break

                if new_count > 0:
                    consecutive_empty_batches = 0
                    current_cursor = next_cursor
                else:
                    consecutive_empty_batches += 1

                # Save checkpoint to disk
                try:
                    with open(ckpt_path, "w", encoding="utf-8") as f:
                        json.dump(collected_items, f, ensure_ascii=False)
                except Exception as e:
                    logger.warning(f"Failed to write checkpoint for job {job_id}: {e}")

                # Update progress & cursor in DB
                async with get_db_session() as session:
                    stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                    r = await session.execute(stmt)
                    current_job = r.scalar_one_or_none()
                    if current_job:
                        current_job.collected_count = len(collected_items)
                        current_job.cursor = current_cursor
                        current_job.updated_at = utc_now()
                        session.add(current_job)
                        await session.commit()

                # Twitter genuinely has no more pages
                if not next_cursor:
                    break

                # True Rate limit from Twitter (HTTP 429) -> Auto-wait 15 min with live countdown!
                if res.get("rate_limited"):
                    wait_seconds = await account_pool.get_next_reset_seconds()
                    resume_at = utc_now() + timedelta(seconds=wait_seconds)
                    logger.warning(f"Job {job_id} hit rate limit. Auto-resuming at {resume_at.isoformat()} ({wait_seconds}s)...")
                    
                    async with get_db_session() as session:
                        stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                        r = await session.execute(stmt)
                        cj = r.scalar_one_or_none()
                        if cj:
                            cj.status = "paused"
                            cj.auto_resume_at = resume_at
                            cj.error_message = f"Twitter 15-dk limitine takıldı (429). {wait_seconds // 60} dk sonra otomatik devam edecek."
                            cj.updated_at = utc_now()
                            session.add(cj)
                            await session.commit()

                    wait_left = wait_seconds
                    user_stopped = False

                    while wait_left > 0:
                        # Check user stop flags
                        flag = self._stop_flags.get(job_id, "")
                        if flag in ("pause", "cancel"):
                            user_stopped = True
                            break

                        # Check if a new active account was added to the pool
                        if await account_pool.has_active_account():
                            logger.info(f"Active account available in pool! Resuming job {job_id} immediately.")
                            break

                        step = min(2, wait_left)
                        await asyncio.sleep(step)
                        wait_left -= step

                    if user_stopped:
                        stopped_early = True
                        break

                    # Cooldown finished or new account added! Clear auto_resume_at and error_message
                    async with get_db_session() as session:
                        stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                        r = await session.execute(stmt)
                        cj = r.scalar_one_or_none()
                        if cj:
                            cj.status = "running"
                            cj.auto_resume_at = None
                            cj.error_message = None
                            cj.updated_at = utc_now()
                            session.add(cj)
                            await session.commit()

                    consecutive_empty_batches = 0
                    logger.info(f"Cooldown complete. Job {job_id} automatically resuming extraction from cursor {current_cursor}...")
                    await asyncio.sleep(0.5)
                    continue

                # If not rate limited but 5 consecutive empty pages returned, timeline is exhausted
                if consecutive_empty_batches >= 5:
                    logger.info(f"Job {job_id} reached end of accessible items (5 empty batches).")
                    break

                await asyncio.sleep(0.4)

            if stopped_early:
                # Graceful user pause: keep checkpoint as source of truth, do not write partial final export
                async with get_db_session() as session:
                    stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                    r = await session.execute(stmt)
                    current_job = r.scalar_one_or_none()
                    if current_job:
                        current_job.collected_count = len(collected_items)
                        current_job.status = "paused"
                        current_job.auto_resume_at = None
                        current_job.error_message = "Kullanıcı tarafından duraklatıldı. İstediğiniz zaman Devam Et butonuna basabilirsiniz."
                        current_job.updated_at = utc_now()
                        await session.commit()
                logger.info(f"Job {job_id} paused at {len(collected_items)} items. Checkpoint saved.")
                return
            # Full completion: Export collected items to file
            timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"extraction_{job_id}_{timestamp_str}.{job.format}"
            output_path = os.path.join(settings.EXPORTS_DIR, filename)

            # Remove prior export file if exists
            if job.output_file_path and os.path.exists(job.output_file_path):
                try:
                    os.remove(job.output_file_path)
                except Exception:
                    pass

            if job.format == "json":
                async with aiofiles.open(output_path, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(collected_items, indent=2, ensure_ascii=False))
            else:
                # CSV Export
                if is_user_list:
                    fieldnames = [
                        "id", "username", "name", "followers_count", "following_count",
                        "tweet_count", "listed_count", "verified", "description",
                        "profile_image_url", "profile_banner_url", "created_at"
                    ]
                else:
                    fieldnames = [
                        "id", "author_username", "author_name", "author_verified",
                        "text", "created_at", "like_count", "retweet_count",
                        "reply_count", "quote_count", "view_count", "bookmark_count",
                        "language", "is_retweet", "is_quote", "is_reply",
                        "url", "media_urls"
                    ]

                with open(output_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    for item in collected_items:
                        row = dict(item)
                        if isinstance(row.get("media_urls"), list):
                            row["media_urls"] = ";".join(row["media_urls"])
                        writer.writerow(row)

            # Clean checkpoint on full completion
            if os.path.exists(ckpt_path):
                try:
                    os.remove(ckpt_path)
                except Exception:
                    pass

            async with get_db_session() as session:
                stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                r = await session.execute(stmt)
                current_job = r.scalar_one_or_none()
                if current_job:
                    current_job.status = "completed"
                    current_job.collected_count = len(collected_items)
                    current_job.output_file_path = output_path
                    current_job.completed_at = utc_now()
                    current_job.error_message = None
                    current_job.updated_at = utc_now()
                    session.add(current_job)
                    await session.commit()

            logger.info(f"Job {job_id} completed successfully. Saved {len(collected_items)} items to {output_path}")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Job {job_id} failed with error: {error_msg}")
            async with get_db_session() as session:
                stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                r = await session.execute(stmt)
                current_job = r.scalar_one_or_none()
                if current_job:
                    current_job.status = "failed"
                    current_job.error_message = error_msg
                    current_job.updated_at = utc_now()
                    session.add(current_job)
                    await session.commit()
        finally:
            self._running_tasks.pop(job_id, None)
            self._stop_flags.pop(job_id, None)

extraction_service = ExtractionService()
