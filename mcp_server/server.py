import asyncio
import json
import logging
from typing import Optional, List, Dict, Any

from mcp.server.mcpserver import MCPServer
from core.database import init_db, get_db_session
from pool.proxy_pool import proxy_pool
from pool.account_pool import account_pool
from scraper.filters import TweetFilter
from scraper.twitter_graphql import twitter_client
from engine.extraction import extraction_service
from engine.monitor import monitor_scheduler
from core.models import Account, Proxy, Monitor
from sqlmodel import select

logger = logging.getLogger("orchis.mcp")

# Initialize MCP Server
mcp_server = MCPServer(
    name="orchisx-scraper",
    instructions="High-speed, self-hosted X/Twitter intelligence and scraping tool suite for AI agents."
)


@mcp_server.tool(
    name="orchis_search_tweets",
    description="Search Twitter for tweets matching keywords or advanced queries with metric filters (likes, retweets, replies, language) and pagination."
)
async def orchis_search_tweets(
    query: str,
    limit: int = 20,
    min_likes: Optional[int] = None,
    min_retweets: Optional[int] = None,
    min_replies: Optional[int] = None,
    language: Optional[str] = None,
    replies: str = "include",
    query_type: str = "Top",
    cursor: Optional[str] = None,
) -> str:
    """Search Twitter/X with advanced filters."""
    await init_db()
    filters = TweetFilter(
        min_likes=min_likes,
        min_retweets=min_retweets,
        min_replies=min_replies,
        language=language,
        replies=replies,  # type: ignore
    )
    res = await twitter_client.search_tweets(
        query=query,
        limit=limit,
        query_type=query_type,
        cursor=cursor,
        filters=filters
    )
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp_server.tool(
    name="orchis_get_user_profile",
    description="Fetch public Twitter user profile by screen name, including follower/following counts, bio, and verification status."
)
async def orchis_get_user_profile(username: str) -> str:
    """Get public Twitter user profile."""
    await init_db()
    profile = await twitter_client.get_user_profile(username)
    if not profile:
        return json.dumps({"error": f"User @{username} not found or inaccessible"})
    return json.dumps(profile, indent=2, ensure_ascii=False)


@mcp_server.tool(
    name="orchis_get_user_tweets",
    description="Fetch recent tweets posted by a specific Twitter user with cursor pagination."
)
async def orchis_get_user_tweets(
    username: str,
    limit: int = 20,
    cursor: Optional[str] = None
) -> str:
    """Fetch user timeline tweets."""
    await init_db()
    res = await twitter_client.get_user_tweets(username=username, limit=limit, cursor=cursor)
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp_server.tool(
    name="orchis_get_tweet_detail",
    description="Retrieve a single tweet's full metadata, metrics, author details, and media URLs by its Tweet ID."
)
async def orchis_get_tweet_detail(tweet_id: str) -> str:
    """Retrieve full tweet details by ID."""
    await init_db()
    tweet = await twitter_client.get_tweet_detail(tweet_id)
    if not tweet:
        return json.dumps({"error": f"Tweet {tweet_id} not found"})
    return json.dumps(tweet, indent=2, ensure_ascii=False)


@mcp_server.tool(
    name="orchis_create_bulk_extraction",
    description="Spawn a background bulk extraction job to scrape up to 50,000 tweets matching a query or user into CSV/JSON."
)
async def orchis_create_bulk_extraction(
    query: str,
    results_limit: int = 100,
    format: str = "csv",
    tool_type: str = "search"
) -> str:
    """Create background bulk extraction job."""
    await init_db()
    job = await extraction_service.create_job(
        query=query,
        results_limit=results_limit,
        tool_type=tool_type,
        export_format=format
    )
    return json.dumps({
        "job_id": job.id,
        "query": job.query,
        "results_limit": job.results_limit,
        "format": job.format,
        "status": job.status,
        "message": "Bulk extraction queued and running in background"
    }, indent=2)


@mcp_server.tool(
    name="orchis_create_monitor",
    description="Create a 24/7 background monitor for keywords or user timeline with periodic polling and HMAC-signed webhook alerts."
)
async def orchis_create_monitor(
    name: str,
    query: str,
    webhook_url: str,
    interval_seconds: int = 300,
    monitor_type: str = "search"
) -> str:
    """Register recurring keyword/timeline monitor."""
    await init_db()
    monitor = Monitor(
        name=name,
        query=query,
        monitor_type=monitor_type,
        interval_seconds=interval_seconds,
        webhook_url=webhook_url,
        status="active"
    )
    async with get_db_session() as session:
        session.add(monitor)
        await session.commit()
        await session.refresh(monitor)

    await monitor_scheduler.register_or_update(monitor)
    return json.dumps({
        "monitor_id": monitor.id,
        "name": monitor.name,
        "query": monitor.query,
        "interval_seconds": monitor.interval_seconds,
        "webhook_url": monitor.webhook_url,
        "webhook_secret": monitor.webhook_secret,
        "status": monitor.status
    }, indent=2)


@mcp_server.tool(
    name="orchis_get_pool_status",
    description="Inspect health, counts, and status of proxy pool and Twitter cookie accounts."
)
async def orchis_get_pool_status() -> str:
    """Inspect proxy and account pool health status."""
    await init_db()
    accounts = await account_pool.get_all_accounts()
    proxies = await proxy_pool.get_all_proxies()

    active_acc = sum(1 for a in accounts if a.status == "active")
    rate_limited_acc = sum(1 for a in accounts if a.status == "rate_limited")
    invalid_acc = sum(1 for a in accounts if a.status == "invalid")
    active_prx = sum(1 for p in proxies if p.status == "active")
    failing_prx = sum(1 for p in proxies if p.status == "failing")

    summary = {
        "status": "healthy" if (active_prx > 0 or len(proxies) == 0) else "degraded",
        "accounts": {
            "total": len(accounts),
            "active": active_acc,
            "rate_limited": rate_limited_acc,
            "invalid": invalid_acc,
        },
        "proxies": {
            "total": len(proxies),
            "active": active_prx,
            "failing": failing_prx,
        }
    }
    return json.dumps(summary, indent=2)


def main():
    """Run MCP server in stdio mode."""
    asyncio.run(mcp_server.run_stdio_async())


if __name__ == "__main__":
    main()
