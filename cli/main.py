import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import sys
from typing import Optional, List
from pathlib import Path

project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn

from config import settings
from core.database import init_db
from pool.proxy_pool import proxy_pool
from pool.account_pool import account_pool
from scraper.filters import TweetFilter
from scraper.twitter_graphql import twitter_client
from engine.extraction import extraction_service
from engine.monitor import monitor_scheduler
from mcp_server.server import main as run_mcp_server

app = typer.Typer(
    name="orchis",
    help="OrchisX — High-Performance Twitter & Web Scraping Intelligence Platform",
    add_completion=False
)
account_app = typer.Typer(help="Manage Twitter cookie credentials pool")
proxy_app = typer.Typer(help="Manage proxy pool & health benchmarks")
user_app = typer.Typer(help="Query Twitter user profiles & timelines")
monitor_app = typer.Typer(help="Manage 24/7 keyword & timeline monitors")

app.add_typer(account_app, name="account")
app.add_typer(proxy_app, name="proxy")
app.add_typer(user_app, name="user")
app.add_typer(monitor_app, name="monitor")

console = Console()


def run_async(coro):
    return asyncio.run(coro)


# ==========================================
# Core Server & MCP Commands
# ==========================================

@app.command()
def server(
    host: str = typer.Option(settings.API_HOST, help="Bind host address"),
    port: int = typer.Option(settings.API_PORT, help="Port to listen on"),
    reload: bool = typer.Option(False, help="Enable auto-reload for development")
):
    """Launch the FastAPI REST server & background monitor scheduler."""
    console.print(Panel.fit(
        f"[bold cyan]Starting OrchisX Scraping Engine REST API[/bold cyan]\n"
        f"[green]Host:[/green] {host}  [green]Port:[/green] {port}\n"
        f"[green]Interactive Docs:[/green] http://{host}:{port}/docs",
        title="OrchisX Server",
        border_style="cyan"
    ))
    uvicorn.run("api.app:app", host=host, port=port, reload=reload)


@app.command()
def mcp():
    """Launch the FastMCP stdio server for AI agents (Cursor, Claude Code, Windsurf)."""
    run_mcp_server()


# ==========================================
# Live Search Command
# ==========================================

@app.command()
def search(
    query: str = typer.Argument(..., help="Search keyword or query expression"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of tweets to retrieve"),
    query_type: str = typer.Option("Top", "--type", "-t", help="Top or Latest"),
    min_likes: Optional[int] = typer.Option(None, "--min-likes", help="Minimum likes"),
    min_retweets: Optional[int] = typer.Option(None, "--min-retweets", help="Minimum retweets"),
    min_replies: Optional[int] = typer.Option(None, "--min-replies", help="Minimum replies"),
    language: Optional[str] = typer.Option(None, "--lang", help="Language code (e.g. en, tr)"),
    replies: str = typer.Option("include", "--replies", help="include | exclude | only"),
):
    """Search Twitter for tweets matching keywords with filters."""
    async def _search():
        await init_db()
        filters = TweetFilter(
            min_likes=min_likes,
            min_retweets=min_retweets,
            min_replies=min_replies,
            language=language,
            replies=replies,  # type: ignore
        )

        with console.status(f"[bold green]Searching Twitter for '{query}'...[/bold green]"):
            res = await twitter_client.search_tweets(
                query=query,
                limit=limit,
                query_type=query_type,
                filters=filters
            )

        tweets = res.get("tweets", [])
        if not tweets:
            console.print(f"[yellow]No tweets found matching '{query}'[/yellow]")
            return

        console.print(f"\n[bold green]Found {len(tweets)} tweets for query:[/bold green] [cyan]{query}[/cyan]\n")
        for i, t in enumerate(tweets, 1):
            author_badge = "✓" if t.get("author_verified") else ""
            author_str = f"[bold white]@{t.get('author_username')}[/bold white] ({t.get('author_name', '')}) {author_badge}"
            metrics_str = f"❤️  {t.get('like_count', 0):,}  |  🔁  {t.get('retweet_count', 0):,}  |  💬  {t.get('reply_count', 0):,}  |  👁️  {(t.get('view_count') or 0):,}"
            created = t.get("created_at") or "Unknown"

            content = f"{t.get('text', '')}\n\n[dim]{created}[/dim]  •  [blue]{t.get('url', '')}[/blue]\n[dim]{metrics_str}[/dim]"
            if t.get("media_urls"):
                content += f"\n[yellow]Media ({len(t['media_urls'])}):[/yellow] " + ", ".join(t["media_urls"][:2])

            console.print(Panel(content, title=f"#{i} {author_str}", border_style="blue"))

    run_async(_search())


# ==========================================
# Account Management Subcommands
# ==========================================

@account_app.command("add")
def account_add(
    auth_token: str = typer.Option(..., "--auth-token", "-a", help="Twitter auth_token cookie"),
    ct0: str = typer.Option(..., "--ct0", "-c", help="Twitter ct0 csrf cookie"),
    username: Optional[str] = typer.Option(None, "--username", "-u", help="Optional Twitter username"),
):
    """Add Twitter credentials to the cookie pool."""
    async def _add():
        await init_db()
        acc = await account_pool.add_or_update_account(auth_token, ct0, username)
        console.print(f"[green]Successfully saved account ID [bold]{acc.id}[/bold] (@{acc.username or 'unnamed'})[/green]")

    run_async(_add())


@account_app.command("list")
def account_list():
    """List all accounts in the pool with status and rate limits."""
    async def _list():
        await init_db()
        accounts = await account_pool.get_all_accounts()
        if not accounts:
            console.print("[yellow]No accounts in pool. Add one with `orchis account add`.[/yellow]")
            return

        table = Table(title="Twitter Account Cookie Pool", border_style="cyan")
        table.add_column("ID", justify="right", style="cyan")
        table.add_column("Username", style="white")
        table.add_column("Status", style="bold")
        table.add_column("Reset Time (UTC)")
        table.add_column("Success", justify="right", style="green")
        table.add_column("Errors", justify="right", style="red")
        table.add_column("Last Used")

        for acc in accounts:
            status_style = "green" if acc.status == "active" else ("yellow" if acc.status == "rate_limited" else "red")
            reset_str = acc.rate_limit_reset_at.strftime("%H:%M:%S") if acc.rate_limit_reset_at else "-"
            last_used_str = acc.last_used_at.strftime("%Y-%m-%d %H:%M") if acc.last_used_at else "Never"

            table.add_row(
                str(acc.id),
                f"@{acc.username}" if acc.username else "(none)",
                f"[{status_style}]{acc.status}[/{status_style}]",
                reset_str,
                str(acc.success_count),
                str(acc.error_count),
                last_used_str
            )

        console.print(table)

    run_async(_list())


@account_app.command("import")
def account_import(
    file_path: str = typer.Argument(..., help="Path to JSON or Netscape cookies text file")
):
    """Import account credentials from JSON or Netscape cookie file."""
    async def _import():
        await init_db()
        if not os.path.exists(file_path):
            console.print(f"[red]File not found: {file_path}[/red]")
            return

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        if file_path.endswith(".json"):
            count = await account_pool.import_from_json(content)
        else:
            count = await account_pool.import_from_netscape(content)

        console.print(f"[green]Successfully imported {count} account(s) from {file_path}[/green]")

    run_async(_import())


@account_app.command("delete")
def account_delete(account_id: int = typer.Argument(..., help="Account ID to delete")):
    """Delete an account from the pool."""
    async def _del():
        await init_db()
        success = await account_pool.delete_account(account_id)
        if success:
            console.print(f"[green]Account {account_id} deleted successfully[/green]")
        else:
            console.print(f"[red]Account {account_id} not found[/red]")

    run_async(_del())


# ==========================================
# Proxy Management Subcommands
# ==========================================

@proxy_app.command("sync")
def proxy_sync(
    file_path: Optional[str] = typer.Option(None, "--file", "-f", help="Proxy list path")
):
    """Sync proxies from text file into database."""
    async def _sync():
        await init_db()
        count = await proxy_pool.sync_from_file(file_path)
        console.print(f"[green]Synced {count} proxies into database[/green]")

    run_async(_sync())


@proxy_app.command("list")
def proxy_list():
    """List all proxies with latency and error metrics."""
    async def _list():
        await init_db()
        proxies = await proxy_pool.get_all_proxies()
        if not proxies:
            console.print("[yellow]No proxies loaded. Run `orchis proxy sync`.[/yellow]")
            return

        table = Table(title="Proxy Pool Status", border_style="magenta")
        table.add_column("ID", justify="right")
        table.add_column("IP:Port", style="white")
        table.add_column("Status", style="bold")
        table.add_column("Latency", justify="right")
        table.add_column("Success", justify="right", style="green")
        table.add_column("Errors", justify="right", style="red")

        for p in proxies:
            status_style = "green" if p.status == "active" else "red"
            lat = f"{p.latency_ms}ms" if p.latency_ms is not None else "-"
            table.add_row(
                str(p.id),
                f"{p.ip}:{p.port}",
                f"[{status_style}]{p.status}[/{status_style}]",
                lat,
                str(p.success_count),
                str(p.error_count)
            )

        console.print(table)

    run_async(_list())


@proxy_app.command("check")
def proxy_check(
    target: str = typer.Option("https://httpbin.org/ip", "--target", "-t", help="Target URL to probe")
):
    """Benchmark and check connectivity across all proxies concurrently."""
    async def _check():
        await init_db()
        with console.status("[bold green]Testing proxies in parallel...[/bold green]"):
            results = await proxy_pool.check_all_proxies(target)

        table = Table(title="Proxy Health Check Results", border_style="green")
        table.add_column("IP:Port", style="white")
        table.add_column("Status", style="bold")
        table.add_column("Latency", justify="right")
        table.add_column("Details")

        for r in results:
            status_style = "green" if r["success"] else "red"
            status_text = "PASS" if r["success"] else "FAIL"
            lat = f"{r.get('latency_ms', 0)}ms"
            detail = f"HTTP {r.get('status_code', '-')}" if r["success"] else r.get("error", "Error")

            table.add_row(
                f"{r['ip']}:{r['port']}",
                f"[{status_style}]{status_text}[/{status_style}]",
                lat,
                str(detail)[:40]
            )

        console.print(table)

    run_async(_check())


# ==========================================
# User Queries Subcommands
# ==========================================

@user_app.command("profile")
def user_profile(username: str = typer.Argument(..., help="Twitter screen name")):
    """Fetch Twitter user profile and stats."""
    async def _profile():
        await init_db()
        with console.status(f"[bold green]Fetching profile @{username}...[/bold green]"):
            p = await twitter_client.get_user_profile(username)

        if not p:
            console.print(f"[red]User @{username} not found or inaccessible[/red]")
            return

        verified_badge = "[blue]✓ Verified[/blue]" if p.get("verified") else ""
        content = (
            f"[bold white]{p.get('name')}[/bold white] (@{p.get('username')}) {verified_badge}\n\n"
            f"{p.get('description', '')}\n\n"
            f"👥  Followers: [bold cyan]{p.get('followers_count', 0):,}[/bold cyan]  |  "
            f"Following: [bold cyan]{p.get('following_count', 0):,}[/bold cyan]  |  "
            f"Tweets: [bold cyan]{p.get('tweet_count', 0):,}[/bold cyan]"
        )
        console.print(Panel(content, title=f"User Profile: @{username}", border_style="cyan"))

    run_async(_profile())


@user_app.command("tweets")
def user_tweets(
    username: str = typer.Argument(..., help="Twitter screen name"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of tweets")
):
    """Fetch recent tweets posted by user."""
    async def _tweets():
        await init_db()
        with console.status(f"[bold green]Fetching tweets for @{username}...[/bold green]"):
            res = await twitter_client.get_user_tweets(username=username, limit=limit)

        tweets = res.get("tweets", [])
        if not tweets:
            console.print(f"[yellow]No tweets found for @{username}[/yellow]")
            return

        console.print(f"\n[bold green]Recent {len(tweets)} tweets from @{username}:[/bold green]\n")
        for i, t in enumerate(tweets, 1):
            metrics_str = f"❤️  {t.get('like_count', 0):,}  |  🔁  {t.get('retweet_count', 0):,}  |  💬  {t.get('reply_count', 0):,}"
            created = t.get("created_at") or "Unknown"
            console.print(Panel(f"{t.get('text', '')}\n\n[dim]{created}  •  {metrics_str}[/dim]", title=f"#{i}", border_style="blue"))

    run_async(_tweets())


# ==========================================
# Bulk Extraction & Monitor Commands
# ==========================================

@app.command("extract")
def extract(
    query: str = typer.Argument(..., help="Search query or username to extract"),
    limit: int = typer.Option(50, "--limit", "-l", help="Number of tweets to collect"),
    format: str = typer.Option("csv", "--format", "-f", help="csv or json"),
    tool_type: str = typer.Option("search", "--type", "-t", help="search or user_tweets")
):
    """Run bulk extraction and stream output to CSV or JSON."""
    async def _extract():
        await init_db()
        job = await extraction_service.create_job(
            query=query,
            results_limit=limit,
            tool_type=tool_type,
            export_format=format
        )
        console.print(f"[green]Extraction job created with ID: [bold]{job.id}[/bold][/green]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            task = progress.add_task(f"Collecting up to {limit} tweets...", total=limit)

            while True:
                current_job = await extraction_service.get_job(job.id)
                if not current_job:
                    break

                progress.update(task, description=f"Collected {current_job.collected_count}/{limit} tweets (status: {current_job.status})")

                if current_job.status in ("completed", "failed"):
                    break
                await asyncio.sleep(1.0)

        if current_job and current_job.status == "completed":
            console.print(Panel.fit(
                f"[bold green]Bulk Extraction Completed![/bold green]\n"
                f"[white]Collected:[/white] {current_job.collected_count} tweets\n"
                f"[white]Output file:[/white] [cyan]{current_job.output_file_path}[/cyan]",
                border_style="green"
            ))
        else:
            console.print(f"[red]Extraction failed: {current_job.error_message if current_job else 'Unknown error'}[/red]")

    run_async(_extract())


@monitor_app.command("add")
def monitor_add(
    name: str = typer.Option(..., "--name", "-n", help="Monitor label"),
    query: str = typer.Option(..., "--query", "-q", help="Search query or @username"),
    url: str = typer.Option(..., "--url", "-u", help="Webhook receiver URL"),
    interval: int = typer.Option(300, "--interval", "-i", help="Interval in seconds"),
    monitor_type: str = typer.Option("search", "--type", "-t", help="search or user_timeline")
):
    """Create a new 24/7 background monitor."""
    async def _add():
        await init_db()
        from core.database import get_db_session
        from core.models import Monitor
        import secrets

        secret = secrets.token_hex(16)
        mon = Monitor(
            name=name,
            query=query,
            monitor_type=monitor_type,
            interval_seconds=interval,
            webhook_url=url,
            webhook_secret=secret,
            status="active"
        )
        async with get_db_session() as session:
            session.add(mon)
            await session.commit()
            await session.refresh(mon)

        console.print(Panel.fit(
            f"[bold green]Monitor Created Successfully![/bold green]\n"
            f"[white]ID:[/white] {mon.id}\n"
            f"[white]Name:[/white] {mon.name}\n"
            f"[white]Query:[/white] {mon.query}\n"
            f"[white]Webhook URL:[/white] {mon.webhook_url}\n"
            f"[white]Webhook Secret:[/white] [cyan]{mon.webhook_secret}[/cyan]",
            border_style="green"
        ))

    run_async(_add())


@monitor_app.command("list")
def monitor_list():
    """List all active background monitors."""
    async def _list():
        await init_db()
        from core.database import get_db_session
        from core.models import Monitor
        from sqlmodel import select

        async with get_db_session() as session:
            stmt = select(Monitor).order_by(Monitor.created_at.desc())
            res = await session.execute(stmt)
            monitors = list(res.scalars().all())

        if not monitors:
            console.print("[yellow]No monitors registered. Create one with `orchis monitor add`.[/yellow]")
            return

        table = Table(title="Active Keyword & Timeline Monitors", border_style="cyan")
        table.add_column("ID", style="dim")
        table.add_column("Name", style="bold white")
        table.add_column("Query", style="cyan")
        table.add_column("Type")
        table.add_column("Interval", justify="right")
        table.add_column("Webhook URL", style="blue")
        table.add_column("Status", style="green")

        for m in monitors:
            table.add_row(
                m.id[:8] + "...",
                m.name,
                m.query,
                m.monitor_type,
                f"{m.interval_seconds}s",
                m.webhook_url[:30] + ("..." if len(m.webhook_url) > 30 else ""),
                m.status
            )

        console.print(table)

    run_async(_list())


def main():
    app()


if __name__ == "__main__":
    main()
