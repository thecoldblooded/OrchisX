import asyncio
from datetime import datetime, timezone
import json
import logging
import re
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urlencode, quote

from curl_cffi.requests import AsyncSession
from config import settings
from pool.account_pool import account_pool, AccountPool
from pool.proxy_pool import proxy_pool, ProxyPool
from core.models import Account, Proxy, utc_now
from scraper.filters import TweetFilter, build_twitter_query, matches_filter

logger = logging.getLogger("orchis.scraper.graphql")

# Standard Public GraphQL Query IDs from Twitter Web
QUERY_IDS = {
    "SearchTimeline": "flaR-PUMYrlaFWsnGR9dqA",
    "UserByScreenName": "sLVLhk0bGj3MVFEKTdax1w",
    "UserTweets": "V7H0Ap3_Hh2FyS75OCDO3Q",
    "TweetDetail": "zXaXixnIQ6srXErWfd0hhQ",
    "Followers": "rrxzbByqCrqDY2ZmQYCWnA",
    "Following": "t-BPOrWh7ihrqNwv55tS5w",
}

DEFAULT_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


def parse_twitter_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        # Twitter standard date format: "Wed Oct 10 20:19:24 +0000 2018"
        return datetime.strptime(dt_str, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        try:
            return datetime.fromisoformat(dt_str)
        except Exception:
            return None


def normalize_tweet_result(tweet_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(tweet_result, dict):
        return None

    res = tweet_result.get("result", tweet_result)
    typename = res.get("__typename")
    if typename == "TweetWithVisibilityResults" or "tweet" in res:
        res = res.get("tweet", res)

    if not res:
        return None

    rest_id = res.get("rest_id")
    core = res.get("core", {}).get("user_results", {}).get("result", {})
    user_core = core.get("core", {})
    user_legacy = core.get("legacy", {})
    legacy = res.get("legacy", {})

    # Extract user info
    screen_name = user_core.get("screen_name") or user_legacy.get("screen_name") or ""
    name = user_core.get("name") or user_legacy.get("name") or screen_name
    author_id = core.get("rest_id")
    verified = user_legacy.get("verified", False) or core.get("is_blue_verified", False)
    profile_image = user_legacy.get("profile_image_url_https") or core.get("avatar", {}).get("image_url")

    # Extract metrics
    favorite_count = legacy.get("favorite_count", 0)
    retweet_count = legacy.get("retweet_count", 0)
    reply_count = legacy.get("reply_count", 0)
    quote_count = legacy.get("quote_count", 0)
    bookmark_count = legacy.get("bookmark_count", 0)
    views_info = res.get("views", {})
    views_count = int(views_info.get("count", 0)) if views_info.get("count") else None

    # Extract text & created_at
    full_text = legacy.get("full_text", "")
    created_at_str = legacy.get("created_at")
    created_at = parse_twitter_datetime(created_at_str)

    # Extract media URLs
    media_urls = []
    entities = legacy.get("extended_entities", {}) or legacy.get("entities", {})
    for m in entities.get("media", []):
        m_url = m.get("media_url_https") or m.get("media_url")
        if m_url:
            media_urls.append(m_url)

    # Extract quoted / reply status
    is_reply = bool(legacy.get("in_reply_to_status_id_str"))
    is_quote = bool(res.get("quoted_status_result"))
    is_retweet = full_text.startswith("RT @") or bool(legacy.get("retweeted_status_result"))

    tweet_url = f"https://x.com/{screen_name}/status/{rest_id}" if screen_name and rest_id else None

    return {
        "id": str(rest_id),
        "text": full_text,
        "author_id": str(author_id) if author_id else None,
        "author_username": screen_name,
        "author_name": name,
        "author_verified": verified,
        "author_profile_image_url": profile_image,
        "created_at": created_at.isoformat() if created_at else None,
        "like_count": favorite_count,
        "retweet_count": retweet_count,
        "reply_count": reply_count,
        "quote_count": quote_count,
        "bookmark_count": bookmark_count,
        "view_count": views_count,
        "language": legacy.get("lang"),
        "is_reply": is_reply,
        "is_retweet": is_retweet,
        "is_quote": is_quote,
        "media_urls": media_urls,
        "url": tweet_url,
        "raw_json": json.dumps(res),
    }

def normalize_user_profile(user_results: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(user_results, dict):
        return None

    user_node = user_results.get("result", user_results)
    if user_node.get("__typename") == "UserUnavailable":
        return None

    rest_id = user_node.get("rest_id")
    core = user_node.get("core", {})
    legacy = user_node.get("legacy", {})
    verification = user_node.get("verification_info", {})
    avatar = user_node.get("avatar", {})
    banner = user_node.get("banner", {})

    screen_name = core.get("screen_name") or legacy.get("screen_name") or ""
    name = core.get("name") or legacy.get("name") or screen_name
    desc = legacy.get("description") or ""

    followers = legacy.get("followers_count", 0)
    following = legacy.get("friends_count", 0)
    tweets = legacy.get("statuses_count", 0)
    listed = legacy.get("listed_count", 0)

    verified = legacy.get("verified", False) or verification.get("verified", False) or user_node.get("is_blue_verified", False)
    profile_image = legacy.get("profile_image_url_https") or avatar.get("image_url")
    profile_banner = legacy.get("profile_banner_url") or banner.get("image_url")

    created_str = legacy.get("created_at") or core.get("created_at")
    created_at = parse_twitter_datetime(created_str)

    return {
        "id": str(rest_id) if rest_id else screen_name,
        "username": screen_name,
        "name": name,
        "description": desc,
        "followers_count": followers,
        "following_count": following,
        "tweet_count": tweets,
        "listed_count": listed,
        "verified": verified,
        "profile_image_url": profile_image,
        "profile_banner_url": profile_banner,
        "created_at": created_at.isoformat() if created_at else None,
        "raw_json": json.dumps(user_node),
    }


class TwitterGraphQLClient:
    def __init__(
        self,
        account_pool_inst: Optional[AccountPool] = None,
        proxy_pool_inst: Optional[ProxyPool] = None,
        timeout: float = settings.DEFAULT_REQUEST_TIMEOUT,
    ):
        self.account_pool = account_pool_inst or account_pool
        self.proxy_pool = proxy_pool_inst or proxy_pool
        self.timeout = timeout

    def _build_headers(self, account: Optional[Account] = None) -> Dict[str, str]:
        headers = {
            "authorization": settings.DEFAULT_BEARER_TOKEN,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "x-twitter-auth-type": "OAuth2Session",
            "user-agent": settings.DEFAULT_USER_AGENT,
            "content-type": "application/json",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "referer": "https://x.com/",
            "origin": "https://x.com",
        }
        if account:
            headers["x-csrf-token"] = account.ct0
            headers["cookie"] = f"auth_token={account.auth_token}; ct0={account.ct0}"
        return headers

    async def _send_graphql_request(
        self,
        endpoint_name: str,
        variables: Dict[str, Any],
        account: Optional[Account] = None,
        proxy: Optional[Proxy] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        query_id = QUERY_IDS.get(endpoint_name, "")
        url = f"https://x.com/i/api/graphql/{query_id}/{endpoint_name}"
        headers = self._build_headers(account)

        proxies_dict = {"http": proxy.url, "https": proxy.url} if proxy else None
        async with AsyncSession(impersonate="chrome120", proxies=proxies_dict) as session:
            if endpoint_name in ("SearchTimeline", "Followers"):
                payload = {
                    "variables": variables,
                    "features": DEFAULT_FEATURES,
                    "queryId": query_id
                }
                resp = await session.post(url, json=payload, headers=headers, timeout=int(self.timeout))
            else:
                params = {
                    "variables": json.dumps(variables),
                    "features": json.dumps(DEFAULT_FEATURES),
                }
                resp = await session.get(url, params=params, headers=headers, timeout=int(self.timeout))

            try:
                data = resp.json()
            except Exception:
                data = {"text": resp.text}
            return resp.status_code, data

    def _extract_timeline_entries(self, data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        tweets = []
        next_cursor = None

        instructions = (
            data.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        if not instructions:
            instructions = (
                data.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline_v2", {})
                .get("timeline", {})
                .get("instructions", [])
            )
        if not instructions:
            instructions = (
                data.get("data", {})
                .get("threaded_conversation_with_injections_v2", {})
                .get("instructions", [])
            )

        for inst in instructions:
            inst_type = inst.get("type")
            entries = inst.get("entries", [])
            if inst_type == "TimelineAddEntries":
                for entry in entries:
                    content = entry.get("content", {})
                    entry_type = content.get("entryType")
                    t_item = content.get("itemContent", {}).get("tweet_results", {})
                    if t_item:
                        norm = normalize_tweet_result(t_item)
                        if norm:
                            tweets.append(norm)
                    elif entry_type == "TimelineTimelineCursor" or "cursor" in entry.get("entryId", "").lower():
                        if content.get("cursorType") == "Bottom" or "bottom" in entry.get("entryId", "").lower():
                            next_cursor = content.get("value")
                        elif not next_cursor and content.get("value"):
                            next_cursor = content.get("value")
                    elif entry_type == "TimelineTimelineModule":
                        items = content.get("items", [])
                        for item in items:
                            m_content = item.get("item", {}).get("itemContent", {})
                            m_tweet = m_content.get("tweet_results", {}).get("result")
                            if m_tweet:
                                norm = normalize_tweet_result(m_tweet)
                                if norm:
                                    tweets.append(norm)
        return tweets, next_cursor

    def _extract_user_list_entries(self, data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        users = []
        next_cursor = None

        instructions = (
            data.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        for inst in instructions:
            entries = inst.get("entries", [])
            for entry in entries:
                content = entry.get("content", {})
                item_content = content.get("itemContent", {})
                user_results = item_content.get("user_results", {}).get("result")
                if user_results:
                    user = normalize_user_profile(user_results)
                    if user:
                        users.append(user)
                elif "cursor-bottom" in entry.get("entryId", "") or content.get("cursorType") == "Bottom":
                    next_cursor = content.get("value")

        return users, next_cursor

    async def search_tweets(
        self,
        query: str,
        limit: int = 20,
        query_type: str = "Top",
        cursor: Optional[str] = None,
        filters: Optional[TweetFilter] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Search tweets matching query and filter with automatic cursor pagination.
        """
        final_query = build_twitter_query(query, filters)
        collected_tweets = []
        seen_ids = set()
        current_cursor = cursor
        retries = 0
        max_retries = settings.MAX_RETRIES_PER_QUERY

        while len(collected_tweets) < limit:
            account = await self.account_pool.get_active_account(session_id=session_id)
            proxy = await self.proxy_pool.get_next_proxy(session_id=session_id)

            variables = {
                "rawQuery": final_query,
                "count": min(50, limit - len(collected_tweets) + 10),
                "querySource": "typed_query",
                "product": "Latest" if query_type.lower() == "latest" else "Top",
            }
            if current_cursor:
                variables["cursor"] = current_cursor

            try:
                status_code, data = await self._send_graphql_request("SearchTimeline", variables, account, proxy)

                if status_code == 200:
                    if account:
                        await self.account_pool.mark_success(account.id)
                    if proxy:
                        await self.proxy_pool.mark_proxy_success(proxy.url)

                    tweets, next_cursor = self._extract_timeline_entries(data)
                    if not tweets and not next_cursor:
                        break

                    new_items_found = False
                    for t in tweets:
                        if t["id"] not in seen_ids:
                            seen_ids.add(t["id"])
                            if matches_filter(t, filters):
                                collected_tweets.append(t)
                                new_items_found = True
                                if len(collected_tweets) >= limit:
                                    break

                    if not next_cursor or next_cursor == current_cursor or not new_items_found:
                        break

                    current_cursor = next_cursor
                    await asyncio.sleep(0.3)

                elif status_code == 429:
                    if account:
                        await self.account_pool.mark_rate_limited(account.id)
                    retries += 1
                    if retries > max_retries:
                        break
                    await asyncio.sleep(1.0)

                elif status_code in (401, 403):
                    if account:
                        await self.account_pool.mark_invalid(account.id, f"HTTP {status_code}")
                    retries += 1
                    if retries > max_retries:
                        break

                else:
                    if proxy:
                        await self.proxy_pool.mark_proxy_failed(proxy.url, f"HTTP {status_code}")
                    retries += 1
                    if retries > max_retries:
                        break
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.warning(f"Search request exception: {e}")
                if proxy:
                    await self.proxy_pool.mark_proxy_failed(proxy.url, str(e))
                retries += 1
                if retries > max_retries:
                    break
                await asyncio.sleep(0.5)

        return {
            "query": query,
            "count": len(collected_tweets),
            "tweets": collected_tweets,
            "next_cursor": current_cursor,
            "has_more": bool(current_cursor and len(collected_tweets) >= limit),
        }

    async def get_user_profile(self, username: str, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Fetch public Twitter user profile by screen name.
        """
        screen_name = username.strip().lstrip("@")
        account = await self.account_pool.get_active_account(session_id=session_id)
        proxy = await self.proxy_pool.get_next_proxy(session_id=session_id)

        variables = {
            "screen_name": screen_name,
            "withSafetyModeUserFields": True,
        }

        try:
            status_code, data = await self._send_graphql_request("UserByScreenName", variables, account, proxy)
            if status_code == 200:
                user_res = data.get("data", {}).get("user", {}).get("result")
                if user_res:
                    return normalize_user_profile(user_res)
            return None
        except Exception as e:
            logger.error(f"Error fetching user profile for {screen_name}: {e}")
            return None

    async def get_user_tweets(
        self,
        username: str,
        limit: int = 20,
        cursor: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Fetch tweets posted by a user with cursor pagination.
        """
        profile = await self.get_user_profile(username, session_id=session_id)
        if not profile:
            return await self.search_tweets(f"from:{username}", limit=limit, cursor=cursor, session_id=session_id)

        user_id = profile["id"]
        collected_tweets = []
        seen_ids = set()
        current_cursor = cursor
        retries = 0
        max_retries = settings.MAX_RETRIES_PER_QUERY

        while len(collected_tweets) < limit:
            account = await self.account_pool.get_active_account(session_id=session_id)
            proxy = await self.proxy_pool.get_next_proxy(session_id=session_id)

            variables = {
                "userId": str(user_id),
                "count": min(50, limit - len(collected_tweets) + 10),
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            }
            if current_cursor:
                variables["cursor"] = current_cursor

            try:
                status_code, data = await self._send_graphql_request("UserTweets", variables, account, proxy)
                if status_code == 200:
                    tweets, next_cursor = self._extract_timeline_entries(data)
                    if not tweets and not next_cursor:
                        break

                    for t in tweets:
                        if t["id"] not in seen_ids:
                            seen_ids.add(t["id"])
                            collected_tweets.append(t)
                            if len(collected_tweets) >= limit:
                                break

                    if not next_cursor or next_cursor == current_cursor:
                        break
                    current_cursor = next_cursor
                    await asyncio.sleep(0.3)
                else:
                    retries += 1
                    if retries > max_retries:
                        break
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error in UserTweets request: {e}")
                break

        return {
            "username": username,
            "count": len(collected_tweets),
            "tweets": collected_tweets,
            "next_cursor": current_cursor,
            "has_more": bool(current_cursor and len(collected_tweets) >= limit),
        }

    async def get_tweet_detail(self, tweet_id: str, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Fetch single tweet details by ID including conversation thread.
        """
        account = await self.account_pool.get_active_account(session_id=session_id)
        proxy = await self.proxy_pool.get_next_proxy(session_id=session_id)

        variables = {
            "focalTweetId": str(tweet_id),
            "with_rux_injections": False,
            "includePromotedContent": False,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
        }

        try:
            status_code, data = await self._send_graphql_request("TweetDetail", variables, account, proxy)
            if status_code == 200:
                tweets, _ = self._extract_timeline_entries(data)
                for t in tweets:
                    if t["id"] == str(tweet_id):
                        return t
                if tweets:
                    return tweets[0]
            return None
        except Exception as e:
            logger.error(f"Error fetching tweet detail {tweet_id}: {e}")
            return None

    async def get_user_followers(
        self,
        username: str,
        limit: int = 20,
        cursor: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Fetch follower profiles for a user with cursor pagination.
        """
        profile = await self.get_user_profile(username, session_id=session_id)
        if not profile:
            return {"username": username, "count": 0, "users": [], "next_cursor": None, "has_more": False}

        user_id = profile["id"]
        collected_users = []
        seen_ids = set()
        current_cursor = cursor
        retries = 0
        max_retries = settings.MAX_RETRIES_PER_QUERY
        rate_limited_hit = False

        while len(collected_users) < limit:
            account = await self.account_pool.get_active_account(session_id=session_id)
            if not account:
                logger.warning("No active accounts available in pool for followers extraction.")
                rate_limited_hit = True
                break

            proxy = await self.proxy_pool.get_next_proxy(session_id=session_id)

            variables = {
                "userId": str(user_id),
                "count": min(50, limit - len(collected_users) + 10),
                "includePromotedContent": False,
            }
            if current_cursor:
                variables["cursor"] = current_cursor

            try:
                status_code, data = await self._send_graphql_request("Followers", variables, account, proxy)
                if status_code == 200:
                    if account:
                        await self.account_pool.mark_success(account.id)
                    if proxy:
                        await self.proxy_pool.mark_proxy_success(proxy.url)

                    users, next_cursor = self._extract_user_list_entries(data)
                    if not users and not next_cursor:
                        current_cursor = None
                        break

                    new_found = False
                    for u in users:
                        if u["id"] not in seen_ids:
                            seen_ids.add(u["id"])
                            collected_users.append(u)
                            new_found = True
                            if len(collected_users) >= limit:
                                break

                    if not next_cursor or next_cursor == current_cursor or not new_found:
                        current_cursor = next_cursor if next_cursor != current_cursor else None
                        break
                    current_cursor = next_cursor
                    await asyncio.sleep(0.3)

                elif status_code == 429:
                    rate_limited_hit = True
                    if account:
                        await self.account_pool.mark_rate_limited(account.id)
                    retries += 1
                    if retries > max_retries:
                        break
                    await asyncio.sleep(1.0)

                elif status_code in (401, 403):
                    if account:
                        await self.account_pool.mark_invalid(account.id, f"HTTP {status_code}")
                    retries += 1
                    if retries > max_retries:
                        break

                else:
                    if proxy:
                        await self.proxy_pool.mark_proxy_failed(proxy.url, f"HTTP {status_code}")
                    retries += 1
                    if retries > max_retries:
                        break
            except Exception as e:
                logger.error(f"Error fetching followers for {username}: {e}")
                if proxy:
                    await self.proxy_pool.mark_proxy_failed(proxy.url, str(e))
                retries += 1
                if retries > max_retries:
                    break
                await asyncio.sleep(0.5)

        return {
            "username": username,
            "count": len(collected_users),
            "users": collected_users,
            "next_cursor": current_cursor,
            "has_more": bool(current_cursor),
            "rate_limited": rate_limited_hit,
        }

    async def get_user_following(
        self,
        username: str,
        limit: int = 20,
        cursor: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Fetch following accounts for a user with cursor pagination.
        """
        profile = await self.get_user_profile(username, session_id=session_id)
        if not profile:
            return {"username": username, "count": 0, "users": [], "next_cursor": None, "has_more": False}

        user_id = profile["id"]
        collected_users = []
        seen_ids = set()
        current_cursor = cursor
        retries = 0
        max_retries = settings.MAX_RETRIES_PER_QUERY
        rate_limited_hit = False

        while len(collected_users) < limit:
            account = await self.account_pool.get_active_account(session_id=session_id)
            if not account:
                logger.warning("No active accounts available in pool for following extraction.")
                rate_limited_hit = True
                break

            proxy = await self.proxy_pool.get_next_proxy(session_id=session_id)

            variables = {
                "userId": str(user_id),
                "count": min(50, limit - len(collected_users) + 10),
                "includePromotedContent": False,
            }
            if current_cursor:
                variables["cursor"] = current_cursor

            try:
                status_code, data = await self._send_graphql_request("Following", variables, account, proxy)
                if status_code == 200:
                    if account:
                        await self.account_pool.mark_success(account.id)
                    if proxy:
                        await self.proxy_pool.mark_proxy_success(proxy.url)

                    users, next_cursor = self._extract_user_list_entries(data)
                    if not users and not next_cursor:
                        current_cursor = None
                        break

                    new_found = False
                    for u in users:
                        if u["id"] not in seen_ids:
                            seen_ids.add(u["id"])
                            collected_users.append(u)
                            new_found = True
                            if len(collected_users) >= limit:
                                break

                    if not next_cursor or next_cursor == current_cursor or not new_found:
                        current_cursor = next_cursor if next_cursor != current_cursor else None
                        break
                    current_cursor = next_cursor
                    await asyncio.sleep(0.3)

                elif status_code == 429:
                    rate_limited_hit = True
                    if account:
                        await self.account_pool.mark_rate_limited(account.id)
                    retries += 1
                    if retries > max_retries:
                        break
                    await asyncio.sleep(1.0)

                elif status_code in (401, 403):
                    if account:
                        await self.account_pool.mark_invalid(account.id, f"HTTP {status_code}")
                    retries += 1
                    if retries > max_retries:
                        break

                else:
                    if proxy:
                        await self.proxy_pool.mark_proxy_failed(proxy.url, f"HTTP {status_code}")
                    retries += 1
                    if retries > max_retries:
                        break
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error fetching following for {username}: {e}")
                if proxy:
                    await self.proxy_pool.mark_proxy_failed(proxy.url, str(e))
                retries += 1
                if retries > max_retries:
                    break
                await asyncio.sleep(0.5)

        return {
            "username": username,
            "count": len(collected_users),
            "users": collected_users,
            "next_cursor": current_cursor,
            "has_more": bool(current_cursor),
            "rate_limited": rate_limited_hit,
        }


twitter_client = TwitterGraphQLClient()
