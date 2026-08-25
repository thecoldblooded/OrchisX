from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


class TweetAuthor(BaseModel):
    id: Optional[str] = None
    username: Optional[str] = None
    name: Optional[str] = None
    verified: bool = False
    profile_image_url: Optional[str] = None


class TweetResponse(BaseModel):
    id: str
    text: str
    author: Optional[TweetAuthor] = None
    author_id: Optional[str] = None
    author_username: Optional[str] = None
    author_name: Optional[str] = None
    author_verified: bool = False
    author_profile_image_url: Optional[str] = None
    created_at: Optional[str] = None
    like_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    quote_count: int = 0
    view_count: Optional[int] = None
    bookmark_count: Optional[int] = None
    language: Optional[str] = None
    conversation_id: Optional[str] = None
    in_reply_to_tweet_id: Optional[str] = None
    is_retweet: bool = False
    is_quote: bool = False
    is_reply: bool = False
    media_urls: List[str] = Field(default_factory=list)
    url: Optional[str] = None


class TweetSearchResponse(BaseModel):
    query: str
    count: int
    tweets: List[TweetResponse]
    next_cursor: Optional[str] = None
    has_more: bool = False


class UserProfileResponse(BaseModel):
    id: str
    username: str
    name: str
    description: Optional[str] = None
    followers_count: int = 0
    following_count: int = 0
    tweet_count: int = 0
    listed_count: int = 0
    verified: bool = False
    profile_image_url: Optional[str] = None
    profile_banner_url: Optional[str] = None
    created_at: Optional[str] = None


class UserTweetsResponse(BaseModel):
    username: str
    count: int
    tweets: List[TweetResponse]
    next_cursor: Optional[str] = None
    has_more: bool = False


class UserListResponse(BaseModel):
    username: str
    count: int
    users: List[UserProfileResponse]
    next_cursor: Optional[str] = None
    has_more: bool = False


# Bulk Extraction Schemas
class CreateExtractionRequest(BaseModel):
    query: str
    results_limit: int = Field(default=100, ge=1, le=50000)
    tool_type: Literal["search", "user_tweets", "user_followers", "user_following"] = "search"
    format: Literal["csv", "json"] = "csv"
    # Optional search filters
    min_likes: Optional[int] = None
    min_retweets: Optional[int] = None
    language: Optional[str] = None
    replies: Literal["include", "exclude", "only"] = "include"


class ExtractionJobResponse(BaseModel):
    id: str
    tool_type: str
    query: str
    results_limit: int
    status: str
    collected_count: int
    format: str
    output_file_path: Optional[str] = None
    download_url: Optional[str] = None
    error_message: Optional[str] = None
    auto_resume_at: Optional[datetime] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

# Monitor Schemas
class CreateMonitorRequest(BaseModel):
    name: str
    query: str
    monitor_type: Literal["search", "user_timeline"] = "search"
    interval_seconds: int = Field(default=300, ge=30)
    webhook_url: str
    webhook_secret: Optional[str] = None


class MonitorResponse(BaseModel):
    id: str
    name: str
    query: str
    monitor_type: str
    interval_seconds: int
    webhook_url: str
    webhook_secret: str
    status: str
    last_run_at: Optional[datetime] = None
    last_tweet_id: Optional[str] = None
    created_at: datetime


# Pool Schemas
class AddAccountRequest(BaseModel):
    auth_token: Optional[str] = None
    ct0: Optional[str] = None
    cookie_string: Optional[str] = None
    username: Optional[str] = None

class AccountResponse(BaseModel):
    id: int
    username: Optional[str] = None
    status: str
    rate_limit_reset_at: Optional[datetime] = None
    success_count: int
    error_count: int
    last_used_at: Optional[datetime] = None
    created_at: datetime


class ProxyResponse(BaseModel):
    id: int
    url: str
    ip: str
    port: int
    status: str
    latency_ms: Optional[int] = None
    error_count: int
    success_count: int
    last_checked_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


class EngineHealthResponse(BaseModel):
    status: str
    active_accounts: int
    rate_limited_accounts: int
    invalid_accounts: int
    active_proxies: int
    failing_proxies: int
    active_monitors: int
