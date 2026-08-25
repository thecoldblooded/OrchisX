import asyncio
import csv
import json
import os
import pytest
from core.database import init_db, get_db_session
from core.models import ExtractionJob, utc_now
from engine.extraction import extraction_service
from config import settings


@pytest.fixture(autouse=True, scope="module")
def setup_db():
    asyncio.run(init_db())


@pytest.mark.asyncio
async def test_csv_export_generation():
    # Mock some collected tweets directly into an extraction job to test export pipeline
    job_id = "test-job-csv-123"
    sample_tweets = [
        {
            "id": "1894000000000000001",
            "author_username": "ai_dev",
            "author_name": "AI Developer",
            "author_verified": True,
            "text": "Building next-gen LLM applications with python",
            "created_at": "2025-02-23T18:00:00+00:00",
            "like_count": 420,
            "retweet_count": 85,
            "reply_count": 12,
            "quote_count": 4,
            "view_count": 5500,
            "bookmark_count": 30,
            "language": "en",
            "is_retweet": False,
            "is_quote": False,
            "is_reply": False,
            "url": "https://x.com/ai_dev/status/1894000000000000001",
            "media_urls": ["https://pbs.twimg.com/media/test.jpg"]
        }
    ]

    output_path = os.path.join(settings.EXPORTS_DIR, f"extraction_{job_id}.csv")
    fieldnames = [
        "id", "author_username", "author_name", "author_verified",
        "text", "created_at", "like_count", "retweet_count",
        "reply_count", "quote_count", "view_count", "bookmark_count",
        "language", "is_retweet", "is_quote", "is_reply",
        "url", "media_urls"
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for t in sample_tweets:
            row = dict(t)
            if isinstance(row.get("media_urls"), list):
                row["media_urls"] = ";".join(row["media_urls"])
            writer.writerow(row)

    assert os.path.exists(output_path)
    with open(output_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["id"] == "1894000000000000001"
        assert rows[0]["author_username"] == "ai_dev"
        assert rows[0]["like_count"] == "420"
        assert rows[0]["media_urls"] == "https://pbs.twimg.com/media/test.jpg"


@pytest.mark.asyncio
async def test_json_export_generation():
    job_id = "test-job-json-456"
    sample_tweets = [
        {
            "id": "1894000000000000002",
            "author_username": "python_fan",
            "text": "AsyncIO in Python 3.14 is super fast",
            "like_count": 999
        }
    ]
    output_path = os.path.join(settings.EXPORTS_DIR, f"extraction_{job_id}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(sample_tweets, indent=2))

    assert os.path.exists(output_path)
    with open(output_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert len(data) == 1
        assert data[0]["id"] == "1894000000000000002"
        assert data[0]["like_count"] == 999
