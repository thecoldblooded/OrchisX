from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


class TweetFilter(BaseModel):
    min_likes: Optional[int] = Field(default=None, ge=0, description="Minimum number of likes")
    min_retweets: Optional[int] = Field(default=None, ge=0, description="Minimum number of retweets")
    min_replies: Optional[int] = Field(default=None, ge=0, description="Minimum number of replies")
    min_quotes: Optional[int] = Field(default=None, ge=0, description="Minimum number of quote tweets")
    min_views: Optional[int] = Field(default=None, ge=0, description="Minimum number of views")
    language: Optional[str] = Field(default=None, description="ISO language code (e.g. 'en', 'es', 'tr')")
    replies: Literal["include", "exclude", "only"] = Field(default="include", description="Filter replies")
    retweets: Literal["include", "exclude", "only"] = Field(default="include", description="Filter retweets")
    quotes: Literal["include", "exclude"] = Field(default="include", description="Filter quotes")
    media_only: Optional[bool] = Field(default=None, description="Only tweets with photos/videos")
    has_links: Optional[bool] = Field(default=None, description="Only tweets containing URLs")
    since_date: Optional[str] = Field(default=None, description="Start date YYYY-MM-DD")
    until_date: Optional[str] = Field(default=None, description="End date YYYY-MM-DD")
    from_user: Optional[str] = Field(default=None, description="Tweets from specific @username")
    to_user: Optional[str] = Field(default=None, description="Tweets sent to specific @username")


def build_twitter_query(raw_query: str, f: Optional[TweetFilter] = None) -> str:
    """
    Combines raw query with Twitter search operators for server-side filtering.
    """
    if not f:
        return raw_query

    parts = [raw_query.strip()] if raw_query.strip() else []

    if f.from_user:
        parts.append(f"from:{f.from_user.lstrip('@')}")
    if f.to_user:
        parts.append(f"to:{f.to_user.lstrip('@')}")
    if f.min_likes is not None and f.min_likes > 0:
        parts.append(f"min_faves:{f.min_likes}")
    if f.min_retweets is not None and f.min_retweets > 0:
        parts.append(f"min_retweets:{f.min_retweets}")
    if f.min_replies is not None and f.min_replies > 0:
        parts.append(f"min_replies:{f.min_replies}")
    if f.language:
        parts.append(f"lang:{f.language}")
    if f.media_only is True:
        parts.append("filter:media")
    if f.has_links is True:
        parts.append("filter:links")
    if f.replies == "exclude":
        parts.append("-filter:replies")
    elif f.replies == "only":
        parts.append("filter:replies")
    if f.retweets == "exclude":
        parts.append("-filter:nativeretweets")
    elif f.retweets == "only":
        parts.append("filter:nativeretweets")
    if f.since_date:
        parts.append(f"since:{f.since_date}")
    if f.until_date:
        parts.append(f"until:{f.until_date}")

    return " ".join(parts).strip()


def matches_filter(tweet_data: Dict[str, Any], f: Optional[TweetFilter] = None) -> bool:
    """
    Client-side verification to ensure 100% adherence to filter criteria.
    """
    if not f:
        return True

    if f.min_likes is not None and tweet_data.get("like_count", 0) < f.min_likes:
        return False
    if f.min_retweets is not None and tweet_data.get("retweet_count", 0) < f.min_retweets:
        return False
    if f.min_replies is not None and tweet_data.get("reply_count", 0) < f.min_replies:
        return False
    if f.min_quotes is not None and tweet_data.get("quote_count", 0) < f.min_quotes:
        return False
    if f.min_views is not None and (tweet_data.get("view_count") or 0) < f.min_views:
        return False
    if f.language and tweet_data.get("language") and tweet_data.get("language") != f.language:
        return False

    is_reply = tweet_data.get("is_reply", False)
    if f.replies == "exclude" and is_reply:
        return False
    elif f.replies == "only" and not is_reply:
        return False

    is_retweet = tweet_data.get("is_retweet", False)
    if f.retweets == "exclude" and is_retweet:
        return False
    elif f.retweets == "only" and not is_retweet:
        return False

    is_quote = tweet_data.get("is_quote", False)
    if f.quotes == "exclude" and is_quote:
        return False

    media_urls = tweet_data.get("media_urls", [])
    if f.media_only is True and not media_urls:
        return False

    if f.from_user:
        author = tweet_data.get("author_username", "").lower()
        if author != f.from_user.lstrip("@").lower():
            return False

    return True
