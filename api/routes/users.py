from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query, Path
from api.schemas import UserProfileResponse, UserTweetsResponse, UserListResponse
from api.routes.tweets import format_tweet_response
from scraper.twitter_graphql import twitter_client

router = APIRouter(prefix="/api/v1/x/users", tags=["Users"])

@router.get("/{username}", response_model=UserProfileResponse)
async def get_user_profile(
    username: str = Path(..., description="Twitter @screen_name")
):
    """
    Fetch public Twitter profile info, follower/following counts, and verification status.
    """
    profile = await twitter_client.get_user_profile(username)
    if not profile:
        raise HTTPException(status_code=404, detail=f"User @{username} not found")

    return UserProfileResponse(
        id=profile["id"],
        username=profile["username"],
        name=profile["name"],
        description=profile.get("description"),
        followers_count=profile.get("followers_count", 0),
        following_count=profile.get("following_count", 0),
        tweet_count=profile.get("tweet_count", 0),
        listed_count=profile.get("listed_count", 0),
        verified=profile.get("verified", False),
        profile_image_url=profile.get("profile_image_url"),
        profile_banner_url=profile.get("profile_banner_url"),
        created_at=profile.get("created_at"),
    )


@router.get("/{username}/tweets", response_model=UserTweetsResponse)
async def get_user_tweets(
    username: str = Path(..., description="Twitter @screen_name"),
    limit: int = Query(20, ge=1, le=200, description="Max tweets to return"),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
):
    """
    Fetch tweets posted by a user with cursor pagination.
    """
    res = await twitter_client.get_user_tweets(username=username, limit=limit, cursor=cursor)
    formatted_tweets = [format_tweet_response(t) for t in res.get("tweets", [])]

    return UserTweetsResponse(
        username=username,
        count=len(formatted_tweets),
        tweets=formatted_tweets,
        next_cursor=res.get("next_cursor"),
        has_more=res.get("has_more", False),
    )


def format_user_profile_response(u: dict) -> UserProfileResponse:
    return UserProfileResponse(
        id=u["id"],
        username=u["username"],
        name=u["name"],
        description=u.get("description"),
        followers_count=u.get("followers_count", 0),
        following_count=u.get("following_count", 0),
        tweet_count=u.get("tweet_count", 0),
        listed_count=u.get("listed_count", 0),
        verified=u.get("verified", False),
        profile_image_url=u.get("profile_image_url"),
        profile_banner_url=u.get("profile_banner_url"),
        created_at=u.get("created_at"),
    )


@router.get("/{username}/followers", response_model=UserListResponse)
async def get_user_followers(
    username: str = Path(..., description="Twitter @screen_name"),
    limit: int = Query(50, ge=1, le=200, description="Max users to return"),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
):
    """
    Fetch followers list for a user with cursor pagination.
    """
    res = await twitter_client.get_user_followers(username=username, limit=limit, cursor=cursor)
    formatted_users = [format_user_profile_response(u) for u in res.get("users", [])]

    return UserListResponse(
        username=username,
        count=len(formatted_users),
        users=formatted_users,
        next_cursor=res.get("next_cursor"),
        has_more=res.get("has_more", False),
    )


@router.get("/{username}/following", response_model=UserListResponse)
async def get_user_following(
    username: str = Path(..., description="Twitter @screen_name"),
    limit: int = Query(50, ge=1, le=200, description="Max users to return"),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
):
    """
    Fetch following list for a user with cursor pagination.
    """
    res = await twitter_client.get_user_following(username=username, limit=limit, cursor=cursor)
    formatted_users = [format_user_profile_response(u) for u in res.get("users", [])]

    return UserListResponse(
        username=username,
        count=len(formatted_users),
        users=formatted_users,
        next_cursor=res.get("next_cursor"),
        has_more=res.get("has_more", False),
    )
