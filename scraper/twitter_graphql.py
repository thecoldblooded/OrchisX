import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urlencode, quote

import httpx
from curl_cffi.requests import AsyncSession
from config import settings
from pool.proxy_pool import proxy_pool, ProxyPool
from pool.account_pool import account_pool, AccountPool
from core.models import Account, Proxy, Tweet, UserProfile, utc_now
from scraper.filters import TweetFilter, build_twitter_query, matches_filter

logger = logging.getLogger("orchis.graphql")

# Known Twitter GraphQL Query IDs & Endpoints
QUERY_IDS = {
    "SearchTimeline": "hyPfJYJ_XAtDYoslQc-Rgg",
    "UserByScreenName": "Gb-d6r0vxPOADdG62OEBpQ",
    "UserTweets": "SXVCYB8XHSS25nzIljNtZA",
    "TweetDetail": "XMOz5h24KAZ86qKffKTLdQ",
    "TweetResultByRestId": "GZsN2Pc4knAoit6pXa4HSA",
    "Followers": "JNyQdTISpzCkj_1fqxDvFg",
    "Following": "qGZZDF3mp91q7X22s3HxpA",
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
    "freedom_of_speech_not_reached_appeal_enabled": True,
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
        # Example: "Thu Feb 20 12:00:00 +0000 2025"
        return datetime.strptime(dt_str, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            return None


def extract_media_urls(legacy: Dict[str, Any]) -> List[str]:
    urls = []
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    for media in entities.get("media", []):
        if "video_info" in media:
            variants = media["video_info"].get("variants", [])
            # pick highest bitrate mp4
            mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and "bitrate" in v]
            if mp4s:
                best = max(mp4s, key=lambda x: x.get("bitrate", 0))
                urls.append(best["url"])
            elif variants:
                urls.append(variants[0].get("url"))
        elif "media_url_https" in media:
            urls.append(media["media_url_https"])
    return urls


def normalize_tweet_result(raw_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalizes GraphQL raw Tweet node into standard OrchisX dictionary format.
    """
    if not isinstance(raw_result, dict):
        return None

    # Handle TweetWithVisibilityResults wrapper
    if raw_result.get("__typename") == "TweetWithVisibilityResults" and "tweet" in raw_result:
        tweet_node = raw_result["tweet"]
    else:
        tweet_node = raw_result

    if not isinstance(tweet_node, dict) or "legacy" not in tweet_node:
        return None

    legacy = tweet_node.get("legacy", {})
    rest_id = tweet_node.get("rest_id") or legacy.get("id_str")
    if not rest_id:
        return None

    # Extract user (supports both legacy schema and new 2025/2026 schema)
    user_results = tweet_node.get("core", {}).get("user_results", {}).get("result", {})
    user_legacy = user_results.get("legacy", {})
    user_core = user_results.get("core", {})
    author_id = user_results.get("rest_id") or user_results.get("id")
    author_username = user_legacy.get("screen_name") or user_core.get("screen_name") or user_results.get("screen_name")
    author_name = user_legacy.get("name") or user_core.get("name") or user_results.get("name") or author_username
    author_verified = (
        user_legacy.get("verified", False)
        or user_results.get("verification", {}).get("verified", False)
        or user_results.get("is_blue_verified", False)
        or user_results.get("verification", {}).get("verified_type") is not None
    )
    author_profile_image = (
        user_legacy.get("profile_image_url_https")
        or user_results.get("avatar", {}).get("image_url")
        or user_results.get("profile_image_url_https")
    )
    like_count = legacy.get("favorite_count", 0)
    retweet_count = legacy.get("retweet_count", 0)
    reply_count = legacy.get("reply_count", 0)
    quote_count = legacy.get("quote_count", 0)
    views_obj = tweet_node.get("views", {})
    view_count = int(views_obj.get("count")) if views_obj.get("count") and views_obj.get("count").isdigit() else None
    bookmark_count = legacy.get("bookmark_count")

    # Full text (handle note_tweet for longform)
    note_tweet = tweet_node.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
    full_text = note_tweet.get("text") or legacy.get("full_text") or legacy.get("text") or ""

    created_at = parse_twitter_datetime(legacy.get("created_at"))
    media_urls = extract_media_urls(legacy)

    is_retweet = "retweeted_status_result" in legacy or full_text.startswith("RT @")
    is_quote = legacy.get("is_quote_status", False)
    is_reply = bool(legacy.get("in_reply_to_status_id_str"))

    return {
        "id": str(rest_id),
        "author_id": str(author_id) if author_id else None,
        "author_username": author_username,
        "author_name": author_name,
        "author_verified": author_verified,
        "author_profile_image_url": author_profile_image,
        "text": full_text,
        "created_at": created_at.isoformat() if created_at else None,
        "like_count": like_count,
        "retweet_count": retweet_count,
        "reply_count": reply_count,
        "quote_count": quote_count,
        "view_count": view_count,
        "bookmark_count": bookmark_count,
        "language": legacy.get("lang"),
        "conversation_id": str(legacy.get("conversation_id_str")) if legacy.get("conversation_id_str") else None,
        "in_reply_to_tweet_id": str(legacy.get("in_reply_to_status_id_str")) if legacy.get("in_reply_to_status_id_str") else None,
        "is_retweet": is_retweet,
        "is_quote": is_quote,
        "is_reply": is_reply,
        "media_urls": media_urls,
        "url": f"https://x.com/{author_username}/status/{rest_id}" if author_username else f"https://x.com/i/web/status/{rest_id}",
        "raw_json": json.dumps(tweet_node),
    }


def normalize_user_profile(user_results: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(user_results, dict):
        return None

    user_node = user_results.get("result", user_results)
    if not isinstance(user_node, dict):
        return None

    legacy = user_node.get("legacy", {})
    core = user_node.get("core", {})
    rel_counts = user_node.get("relationship_counts", {})
    tweet_counts = user_node.get("tweet_counts", {})
    profile_bio = user_node.get("profile_bio", {})
    avatar = user_node.get("avatar", {})
    banner = user_node.get("banner", {})
    verification = user_node.get("verification", {})

    rest_id = user_node.get("rest_id") or user_node.get("id")
    screen_name = legacy.get("screen_name") or core.get("screen_name") or user_node.get("screen_name")
    if not screen_name:
        return None

    name = legacy.get("name") or core.get("name") or user_node.get("name") or screen_name
    desc = legacy.get("description") or profile_bio.get("description") or ""
    followers = legacy.get("followers_count") or rel_counts.get("followers") or rel_counts.get("followers_count") or 0
    following = legacy.get("friends_count") or rel_counts.get("following") or rel_counts.get("following_count") or 0
    tweets = legacy.get("statuses_count") or tweet_counts.get("tweets") or tweet_counts.get("statuses_count") or 0
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

        # Search instructions structure
        instructions = []
        try:
            timeline = data.get("data", {}).get("search_by_raw_query", {}).get("search_timeline", {}).get("timeline", {})
            instructions = timeline.get("instructions", [])
        except Exception:
            pass

        # User timeline instructions structure
        if not instructions:
            try:
                instructions = data.get("data", {}).get("user", {}).get("result", {}).get("timeline_v2", {}).get("timeline", {}).get("instructions", [])
            except Exception:
                pass

        if not instructions:
            try:
                instructions = data.get("data", {}).get("user", {}).get("result", {}).get("timeline", {}).get("timeline", {}).get("instructions", [])
            except Exception:
                pass

        for instr in instructions:
            itype = instr.get("type")
            entries = []
            if itype == "TimelineAddEntries":
                entries = instr.get("entries", [])
            elif itype == "TimelineAddToModule":
                entries = instr.get("moduleItems", [])
            elif "entries" in instr:
                entries = instr.get("entries", [])

            for entry in entries:
                entry_id = entry.get("entryId", "")

                # Check cursor
                if "cursor-bottom" in entry_id or "cursor_type" in entry.get("content", {}) or entry.get("content", {}).get("cursorType") == "Bottom":
                    next_cursor = entry.get("content", {}).get("value") or entry.get("content", {}).get("operation", {}).get("cursor", {}).get("value")
                    continue

                # Single tweet entry
                item_content = entry.get("content", {}).get("itemContent", {})
                tweet_result = item_content.get("tweet_results", {}).get("result")
                if tweet_result:
                    norm = normalize_tweet_result(tweet_result)
                    if norm:
                        tweets.append(norm)

                # Thread/Module items
                items = entry.get("content", {}).get("items", [])
                for module_item in items:
                    m_content = module_item.get("item", {}).get("itemContent", {})
                    m_tweet = m_content.get("tweet_results", {}).get("result")
                    if m_tweet:
                        norm = normalize_tweet_result(m_tweet)
                        if norm:
                            tweets.append(norm)

        return tweets, next_cursor

    async def search_tweets(
        self,
        query: str,
        limit: int = 20,
        query_type: str = "Top",  # "Top" or "Latest"
        cursor: Optional[str] = None,
        filters: Optional[TweetFilter] = None,
    ) -> Dict[str, Any]:
        """
        Search tweets matching query and filter with automatic cursor pagination and LRU account rotation.
        """
        final_query = build_twitter_query(query, filters)
        collected_tweets = []
        seen_ids = set()
        current_cursor = cursor
        retries = 0
        max_retries = settings.MAX_RETRIES_PER_QUERY

        while len(collected_tweets) < limit:
            account = await self.account_pool.get_active_account()
            proxy = await self.proxy_pool.get_next_proxy()

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
                    # Small pacing delay to avoid tight loop
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

    async def get_user_profile(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Fetch public Twitter user profile by screen name.
        """
        screen_name = username.strip().lstrip("@")
        account = await self.account_pool.get_active_account()
        proxy = await self.proxy_pool.get_next_proxy()

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
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch tweets posted by a user with cursor pagination.
        """
        profile = await self.get_user_profile(username)
        if not profile:
            # Fallback search query "from:username"
            return await self.search_tweets(f"from:{username}", limit=limit, cursor=cursor)

        user_id = profile["id"]
        collected_tweets = []
        seen_ids = set()
        current_cursor = cursor
        retries = 0
        max_retries = settings.MAX_RETRIES_PER_QUERY

        while len(collected_tweets) < limit:
            account = await self.account_pool.get_active_account()
            proxy = await self.proxy_pool.get_next_proxy()

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

    async def get_tweet_detail(self, tweet_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch single tweet details by ID including conversation thread.
        """
        account = await self.account_pool.get_active_account()
        proxy = await self.proxy_pool.get_next_proxy()

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
    def _extract_user_list_entries(self, data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        users = []
        next_cursor = None
        instructions = []
        try:
            instructions = data.get("data", {}).get("user", {}).get("result", {}).get("timeline", {}).get("timeline", {}).get("instructions", [])
        except Exception:
            pass

        for instr in instructions:
            entries = instr.get("entries", [])
            for entry in entries:
                content = entry.get("content", {})
                user_results = content.get("itemContent", {}).get("user_results", {})
                if user_results:
                    user = normalize_user_profile(user_results)
                    if user:
                        users.append(user)
                elif "cursor-bottom" in entry.get("entryId", "") or entry.get("content", {}).get("cursorType") == "Bottom":
                    next_cursor = content.get("value")

        return users, next_cursor

    async def get_user_followers(
        self,
        username: str,
        limit: int = 50,
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch follower profiles for a user with cursor pagination.
        """
        profile = await self.get_user_profile(username)
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
            account = await self.account_pool.get_active_account()
            if not account:
                logger.warning("No active accounts available in pool for followers extraction.")
                rate_limited_hit = True
                break

            proxy = await self.proxy_pool.get_next_proxy()

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
                    await asyncio.sleep(0.5)
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
        limit: int = 50,
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch following accounts for a user with cursor pagination.
        """
        profile = await self.get_user_profile(username)
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
            account = await self.account_pool.get_active_account()
            if not account:
                logger.warning("No active accounts available in pool for following extraction.")
                rate_limited_hit = True
                break

            proxy = await self.proxy_pool.get_next_proxy()

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
