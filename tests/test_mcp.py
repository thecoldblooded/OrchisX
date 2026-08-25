import asyncio
import json
import pytest
from core.database import init_db
from mcp_server.server import (
    mcp_server,
    orchis_get_pool_status,
    orchis_create_bulk_extraction,
    orchis_create_monitor,
    orchis_search_tweets,
    orchis_get_user_profile
)


@pytest.fixture(autouse=True, scope="module")
def setup_db():
    asyncio.run(init_db())


@pytest.mark.asyncio
async def test_mcp_tool_listing():
    tools = await mcp_server.list_tools()
    tool_names = [t.name for t in tools]
    assert "orchis_search_tweets" in tool_names
    assert "orchis_get_user_profile" in tool_names
    assert "orchis_get_user_tweets" in tool_names
    assert "orchis_get_tweet_detail" in tool_names
    assert "orchis_create_bulk_extraction" in tool_names
    assert "orchis_create_monitor" in tool_names
    assert "orchis_get_pool_status" in tool_names


@pytest.mark.asyncio
async def test_mcp_get_pool_status():
    result_str = await orchis_get_pool_status()
    data = json.loads(result_str)
    assert "status" in data
    assert "accounts" in data
    assert "proxies" in data
    assert data["proxies"]["total"] >= 10


@pytest.mark.asyncio
async def test_mcp_create_bulk_extraction():
    result_str = await orchis_create_bulk_extraction(
        query="langchain agent",
        results_limit=10,
        format="csv"
    )
    data = json.loads(result_str)
    assert "job_id" in data
    assert data["query"] == "langchain agent"
    assert data["status"] in ("queued", "running", "completed")


@pytest.mark.asyncio
async def test_mcp_create_monitor():
    result_str = await orchis_create_monitor(
        name="MCP Agent Monitor",
        query="agentic workflow",
        webhook_url="https://example.com/webhook",
        interval_seconds=60
    )
    data = json.loads(result_str)
    assert "monitor_id" in data
    assert data["name"] == "MCP Agent Monitor"
    assert data["status"] == "active"
    assert "webhook_secret" in data
