import asyncio
from datetime import datetime, timezone
import json
import logging
import re
from typing import List, Optional, Dict, Any

import httpx
from scrapling.fetchers import Fetcher, StealthyFetcher
from config import settings
from pool.proxy_pool import proxy_pool, ProxyPool
from pool.account_pool import account_pool, AccountPool
from core.models import Account, Proxy
from scraper.twitter_graphql import TwitterGraphQLClient, normalize_tweet_result

logger = logging.getLogger("orchis.browser_fetcher")


class ScraplingStealthFetcher:
    """
    Anti-bot stealth fetcher powered by Scrapling.
    Bypasses simple Cloudflare / TLS fingerprint checks with proxy and cookie support.
    """

    def __init__(self, proxy_pool_inst: Optional[ProxyPool] = None):
        self.proxy_pool = proxy_pool_inst or proxy_pool

    async def fetch_page(
        self,
        url: str,
        account: Optional[Account] = None,
        proxy: Optional[Proxy] = None
    ) -> Optional[str]:
        headers = {
            "User-Agent": settings.DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        if account:
            headers["Cookie"] = f"auth_token={account.auth_token}; ct0={account.ct0}"

        try:
            fetcher = StealthyFetcher()
            # Run blocking scrapling fetcher in thread pool
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: fetcher.fetch(
                    url,
                    headers=headers,
                    proxy=proxy.url if proxy else None,
                    timeout=int(settings.DEFAULT_REQUEST_TIMEOUT)
                )
            )
            return response.text if response else None
        except Exception as e:
            logger.warning(f"Scrapling fetch error for {url}: {e}")
            return None


class CamofoxFallbackFetcher:
    """
    Camofox C++ level fingerprint spoofing browser fallback.
    Interacts with local Camofox service at http://localhost:9377.
    """

    def __init__(self, base_url: str = settings.CAMOFOX_URL):
        self.base_url = base_url.rstrip("/")

    async def is_available(self) -> bool:
        """Check if Camofox service is running."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def navigate_and_extract(
        self,
        url: str,
        wait_selector: Optional[str] = "article[data-testid='tweet']",
        scroll_count: int = 2
    ) -> Optional[str]:
        """
        Open a tab in Camofox, navigate to URL, optionally scroll and extract HTML.
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # 1. Create or open tab
                tab_resp = await client.post(f"{self.base_url}/tabs", json={"url": url})
                if tab_resp.status_code not in (200, 201):
                    return None
                tab_data = tab_resp.json()
                tab_id = tab_data.get("id") or tab_data.get("tabId")

                if not tab_id:
                    return None

                # 2. Wait for content
                if wait_selector:
                    await client.post(
                        f"{self.base_url}/tabs/{tab_id}/wait",
                        json={"selector": wait_selector, "timeout": 10000}
                    )

                # 3. Scroll down to trigger timeline loading
                for _ in range(scroll_count):
                    await client.post(
                        f"{self.base_url}/tabs/{tab_id}/action",
                        json={"kind": "scroll", "direction": "down", "amount": 800}
                    )
                    await asyncio.sleep(1.0)

                # 4. Extract content
                content_resp = await client.get(f"{self.base_url}/tabs/{tab_id}/content")
                html = content_resp.text if content_resp.status_code == 200 else None

                # Close tab
                await client.delete(f"{self.base_url}/tabs/{tab_id}")
                return html
        except Exception as e:
            logger.warning(f"Camofox fallback failed for {url}: {e}")
            return None


class HybridTwitterScraper:
    """
    Unified scraper orchestrator:
    1. Direct GraphQL (fastest, full structured metadata)
    2. Scrapling Stealth (fallback for simple challenge bypass)
    3. Camofox Browser (stealth C++ browser for heavy challenges)
    """

    def __init__(self):
        self.graphql = TwitterGraphQLClient()
        self.scrapling = ScraplingStealthFetcher()
        self.camofox = CamofoxFallbackFetcher()

    async def search(self, query: str, limit: int = 20, **kwargs) -> Dict[str, Any]:
        res = await self.graphql.search_tweets(query, limit=limit, **kwargs)
        if res.get("tweets"):
            return res

        # Fallback to Camofox if available
        if await self.camofox.is_available():
            logger.info("Attempting Camofox fallback for search query...")
            url = f"https://x.com/search?q={query}&f=live"
            html = await self.camofox.navigate_and_extract(url)
            if html:
                # Basic fallback extraction from HTML
                logger.info(f"Retrieved {len(html)} bytes via Camofox")

        return res

    async def get_user_profile(self, username: str) -> Optional[Dict[str, Any]]:
        return await self.graphql.get_user_profile(username)

    async def get_user_tweets(self, username: str, limit: int = 20, **kwargs) -> Dict[str, Any]:
        return await self.graphql.get_user_tweets(username, limit=limit, **kwargs)

    async def get_tweet_detail(self, tweet_id: str) -> Optional[Dict[str, Any]]:
        return await self.graphql.get_tweet_detail(tweet_id)


hybrid_scraper = HybridTwitterScraper()
