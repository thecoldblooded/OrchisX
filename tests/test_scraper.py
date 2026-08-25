import asyncio
from datetime import datetime, timezone
import json
import pytest
from scraper.filters import TweetFilter, build_twitter_query, matches_filter
from scraper.twitter_graphql import normalize_tweet_result, normalize_user_profile, twitter_client
from engine.webhook import webhook_dispatcher


def test_build_twitter_query():
    f = TweetFilter(
        min_likes=100,
        min_retweets=20,
        min_replies=5,
        language="en",
        replies="exclude",
        retweets="exclude",
        media_only=True,
        has_links=True,
        since_date="2025-01-01",
        until_date="2025-02-01",
        from_user="elonmusk"
    )
    q = build_twitter_query("AI", f)
    expected_parts = [
        "AI",
        "from:elonmusk",
        "min_faves:100",
        "min_retweets:20",
        "min_replies:5",
        "lang:en",
        "filter:media",
        "filter:links",
        "-filter:replies",
        "-filter:nativeretweets",
        "since:2025-01-01",
        "until:2025-02-01"
    ]
    for part in expected_parts:
        assert part in q, f"Part '{part}' missing from query '{q}'"


def test_matches_filter():
    tweet_pass = {
        "id": "123",
        "text": "Check out this AI model",
        "like_count": 150,
        "retweet_count": 30,
        "reply_count": 10,
        "quote_count": 5,
        "view_count": 10000,
        "language": "en",
        "is_reply": False,
        "is_retweet": False,
        "is_quote": False,
        "media_urls": ["https://pbs.twimg.com/media/test.jpg"],
        "author_username": "techlead"
    }

    f = TweetFilter(min_likes=100, language="en", replies="exclude", media_only=True)
    assert matches_filter(tweet_pass, f) is True

    # Fails likes
    tweet_low_likes = dict(tweet_pass, like_count=50)
    assert matches_filter(tweet_low_likes, f) is False

    # Fails replies
    tweet_reply = dict(tweet_pass, is_reply=True)
    assert matches_filter(tweet_reply, f) is False

    # Fails media
    tweet_no_media = dict(tweet_pass, media_urls=[])
    assert matches_filter(tweet_no_media, f) is False


def test_normalize_tweet_result():
    sample_raw_node = {
        "__typename": "TweetWithVisibilityResults",
        "tweet": {
            "rest_id": "1894000000000000001",
            "core": {
                "user_results": {
                    "result": {
                        "rest_id": "44196397",
                        "legacy": {
                            "screen_name": "elonmusk",
                            "name": "Elon Musk",
                            "verified": True,
                            "profile_image_url_https": "https://pbs.twimg.com/profile_images/sample.jpg"
                        }
                    }
                }
            },
            "legacy": {
                "full_text": "Grok 3 is now live with incredible reasoning capabilities.",
                "created_at": "Sun Feb 23 18:00:00 +0000 2025",
                "favorite_count": 45000,
                "retweet_count": 8200,
                "reply_count": 5100,
                "quote_count": 1200,
                "bookmark_count": 3400,
                "lang": "en",
                "conversation_id_str": "1894000000000000001",
                "extended_entities": {
                    "media": [
                        {
                            "media_url_https": "https://pbs.twimg.com/media/sample_media.jpg"
                        }
                    ]
                }
            },
            "views": {
                "count": "1500000"
            }
        }
    }

    norm = normalize_tweet_result(sample_raw_node)
    assert norm is not None
    assert norm["id"] == "1894000000000000001"
    assert norm["author_username"] == "elonmusk"
    assert norm["author_name"] == "Elon Musk"
    assert norm["author_verified"] is True
    assert norm["text"] == "Grok 3 is now live with incredible reasoning capabilities."
    assert norm["like_count"] == 45000
    assert norm["retweet_count"] == 8200
    assert norm["view_count"] == 1500000
    assert norm["bookmark_count"] == 3400
    assert norm["language"] == "en"
    assert len(norm["media_urls"]) == 1
    assert norm["url"] == "https://x.com/elonmusk/status/1894000000000000001"


def test_timeline_extraction():
    sample_timeline_data = {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": "sq-I-t-1894000000000000001",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "rest_id": "1894000000000000001",
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "legacy": {"screen_name": "ai_researcher", "name": "AI Researcher"}
                                                                }
                                                            }
                                                        },
                                                        "legacy": {
                                                            "full_text": "Sample tweet content",
                                                            "favorite_count": 10,
                                                            "retweet_count": 2,
                                                            "lang": "en"
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    },
                                    {
                                        "entryId": "cursor-bottom-1894000000000000000",
                                        "content": {
                                            "value": "cursor_token_next_page_xyz"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
    }

    tweets, next_cursor = twitter_client._extract_timeline_entries(sample_timeline_data)
    assert len(tweets) == 1
    assert tweets[0]["id"] == "1894000000000000001"
    assert tweets[0]["author_username"] == "ai_researcher"
    assert next_cursor == "cursor_token_next_page_xyz"


def test_webhook_hmac_signature():
    secret = "test_webhook_secret_key_123"
    payload = b'{"event":"tweet.new","data":{"count":1}}'
    sig = webhook_dispatcher.compute_signature(secret, payload)
    assert sig.startswith("sha256=")
    assert len(sig) == 7 + 64  # 'sha256=' + 64 hex chars


if __name__ == "__main__":
    test_build_twitter_query()
    test_matches_filter()
    test_normalize_tweet_result()
    test_timeline_extraction()
    test_webhook_hmac_signature()
    print("All scraper & protocol unit tests PASSED successfully!")
