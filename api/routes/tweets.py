from typing import Optional, List, Literal
from fastapi import APIRouter, HTTPException, Query, Path, Header
from api.schemas import TweetSearchResponse, TweetResponse, TweetAuthor
from scraper.filters import TweetFilter
from scraper.twitter_graphql import twitter_client

router = APIRouter(prefix="/api/v1/x/tweets", tags=["Tweets"])


def format_tweet_response(t: dict) -> TweetResponse:
    return TweetResponse(
        id=t["id"],
        text=t["text"],
        author=TweetAuthor(
            id=t.get("author_id"),
            username=t.get("author_username"),
            name=t.get("author_name"),
            verified=t.get("author_verified", False),
            profile_image_url=t.get("author_profile_image_url"),
        ),
        author_id=t.get("author_id"),
        author_username=t.get("author_username"),
        author_name=t.get("author_name"),
        author_verified=t.get("author_verified", False),
        author_profile_image_url=t.get("author_profile_image_url"),
        created_at=t.get("created_at"),
        like_count=t.get("like_count", 0),
        retweet_count=t.get("retweet_count", 0),
        reply_count=t.get("reply_count", 0),
        quote_count=t.get("quote_count", 0),
        view_count=t.get("view_count"),
        bookmark_count=t.get("bookmark_count"),
        language=t.get("language"),
        conversation_id=t.get("conversation_id"),
        in_reply_to_tweet_id=t.get("in_reply_to_tweet_id"),
        is_retweet=t.get("is_retweet", False),
        is_quote=t.get("is_quote", False),
        is_reply=t.get("is_reply", False),
        media_urls=t.get("media_urls", []),
        url=t.get("url"),
    )


@router.get("/search", response_model=TweetSearchResponse)
async def search_tweets(
    q: str = Query(..., description="Search keyword or boolean query"),
    queryType: Literal["Top", "Latest"] = Query("Top", description="Ranking strategy"),
    minLikes: Optional[int] = Query(None, ge=0, description="Minimum likes filter"),
    minRetweets: Optional[int] = Query(None, ge=0, description="Minimum retweets filter"),
    minReplies: Optional[int] = Query(None, ge=0, description="Minimum replies filter"),
    language: Optional[str] = Query(None, description="ISO language code (e.g. en, tr)"),
    replies: Literal["include", "exclude", "only"] = Query("include", description="Filter replies"),
    retweets: Literal["include", "exclude", "only"] = Query("include", description="Filter retweets"),
    quotes: Literal["include", "exclude"] = Query("include", description="Filter quote tweets"),
    mediaOnly: Optional[bool] = Query(None, description="Filter for media tweets"),
    sinceDate: Optional[str] = Query(None, description="Since date YYYY-MM-DD"),
    untilDate: Optional[str] = Query(None, description="Until date YYYY-MM-DD"),
    limit: int = Query(20, ge=1, le=200, description="Maximum number of results"),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
):
    """
    Search Twitter with advanced filtering, cursor pagination, and multi-account rotation.
    """
    filters = TweetFilter(
        min_likes=minLikes,
        min_retweets=minRetweets,
        min_replies=minReplies,
        language=language,
        replies=replies,
        retweets=retweets,
        quotes=quotes,
        media_only=mediaOnly,
        since_date=sinceDate,
        until_date=untilDate,
    )

    res = await twitter_client.search_tweets(
        query=q,
        limit=limit,
        query_type=queryType,
        cursor=cursor,
        filters=filters,
        session_id=x_session_id
    )

    formatted_tweets = [format_tweet_response(t) for t in res.get("tweets", [])]

    return TweetSearchResponse(
        query=q,
        count=len(formatted_tweets),
        tweets=formatted_tweets,
        next_cursor=res.get("next_cursor"),
        has_more=res.get("has_more", False),
    )


@router.get("/{id}", response_model=TweetResponse)
async def get_tweet_detail(
    id: str = Path(..., description="Twitter Tweet Snowflake ID"),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
):
    """
    Retrieve single tweet details and metrics by Tweet ID.
    """
    tweet = await twitter_client.get_tweet_detail(id, session_id=x_session_id)
    if not tweet:
        raise HTTPException(status_code=404, detail="Tweet not found or inaccessible")
    return format_tweet_response(tweet)
