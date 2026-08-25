from datetime import datetime, timezone
import json
import secrets
from typing import Optional, List, Dict, Any
import uuid
from sqlmodel import SQLModel, Field, Column, JSON


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Account(SQLModel, table=True):
    __tablename__ = "accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    auth_token: str = Field(index=True, unique=True)
    ct0: str
    username: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="active", index=True)  # active, rate_limited, invalid
    rate_limit_reset_at: Optional[datetime] = None
    success_count: int = Field(default=0)
    error_count: int = Field(default=0)
    last_used_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)


class Proxy(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True, index=True)
    ip: str = Field(index=True)
    port: int
    username: str
    password: str
    status: str = Field(default="active", index=True)  # active, failing, disabled
    latency_ms: Optional[int] = None
    error_count: int = Field(default=0)
    success_count: int = Field(default=0)
    last_checked_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


class Tweet(SQLModel, table=True):
    __tablename__ = "tweets"

    id: str = Field(primary_key=True, index=True)
    author_id: Optional[str] = Field(default=None, index=True)
    author_username: Optional[str] = Field(default=None, index=True)
    author_name: Optional[str] = None
    text: str
    created_at: Optional[datetime] = Field(default=None, index=True)
    like_count: int = Field(default=0)
    retweet_count: int = Field(default=0)
    reply_count: int = Field(default=0)
    quote_count: int = Field(default=0)
    view_count: Optional[int] = None
    bookmark_count: Optional[int] = None
    language: Optional[str] = None
    conversation_id: Optional[str] = None
    in_reply_to_tweet_id: Optional[str] = None
    is_retweet: bool = Field(default=False)
    is_quote: bool = Field(default=False)
    is_reply: bool = Field(default=False)
    media_urls: Optional[str] = None  # JSON encoded list of strings
    raw_json: Optional[str] = None  # JSON encoded tweet data


class UserProfile(SQLModel, table=True):
    __tablename__ = "user_profiles"

    id: str = Field(primary_key=True, index=True)
    username: str = Field(index=True)
    name: str
    description: Optional[str] = None
    followers_count: int = Field(default=0)
    following_count: int = Field(default=0)
    tweet_count: int = Field(default=0)
    listed_count: int = Field(default=0)
    verified: bool = Field(default=False)
    profile_image_url: Optional[str] = None
    profile_banner_url: Optional[str] = None
    created_at: Optional[datetime] = None
    raw_json: Optional[str] = None


class Monitor(SQLModel, table=True):
    __tablename__ = "monitors"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    name: str
    query: str
    monitor_type: str = Field(default="search")  # search, user_timeline
    interval_seconds: int = Field(default=300)
    webhook_url: str
    webhook_secret: str = Field(default_factory=lambda: secrets.token_hex(16))
    status: str = Field(default="active", index=True)  # active, paused
    last_run_at: Optional[datetime] = None
    last_tweet_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class ExtractionJob(SQLModel, table=True):
    __tablename__ = "extraction_jobs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    tool_type: str = Field(default="search")  # search, user_tweets, user_followers, user_following
    query: str
    results_limit: int = Field(default=100)
    status: str = Field(default="queued", index=True)  # queued, running, paused, completed, failed, canceled
    collected_count: int = Field(default=0)
    cursor: Optional[str] = None
    output_file_path: Optional[str] = None
    format: str = Field(default="csv")  # csv, json
    error_message: Optional[str] = None
    filters_json: Optional[str] = None
    auto_resume_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
class WebhookLog(SQLModel, table=True):
    __tablename__ = "webhook_logs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    monitor_id: str = Field(index=True)
    event_type: str = Field(default="tweet.new")
    payload: str
    status_code: Optional[int] = None
    response_body: Optional[str] = None
    delivered_at: datetime = Field(default_factory=utc_now)
    attempt: int = Field(default=1)
    success: bool = Field(default=False)
