from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from config import settings
from core.database import init_db
from pool.proxy_pool import proxy_pool
from engine.monitor import monitor_scheduler
from engine.extraction import extraction_service
from api.routes.tweets import router as tweets_router
from api.routes.users import router as users_router
from api.routes.extractions import router as extractions_router
from api.routes.monitors import router as monitors_router
from api.routes.pool import router as pool_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("orchis.api")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing OrchisX Scraper Engine...")
    await init_db()
    synced_proxies = await proxy_pool.sync_from_file()
    logger.info(f"Loaded {synced_proxies} proxies into pool")
    # Sweep orphaned running extraction jobs to paused
    try:
        from core.database import get_db_session
        from core.models import ExtractionJob
        from sqlmodel import select
        async with get_db_session() as session:
            from core.models import utc_now
            stmt = select(ExtractionJob).where(ExtractionJob.status == "running")
            res = await session.execute(stmt)
            for j in res.scalars().all():
                j.status = "paused"
                j.auto_resume_at = utc_now()
                session.add(j)
            await session.commit()
    except Exception as e:
        logger.warning(f"Startup sweep failed: {e}")

    await extraction_service.start_scheduler()
    await monitor_scheduler.start()
    yield
    # Shutdown
    logger.info("Shutting down monitor and extraction schedulers...")
    await extraction_service.stop_scheduler()
    await monitor_scheduler.shutdown()

app = FastAPI(
    title="OrchisX Engine API",
    description="Self-hosted, high-performance X/Twitter intelligence and scraping platform.",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Static Files if directory exists
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Routers
app.include_router(tweets_router)
app.include_router(users_router)
app.include_router(extractions_router)
app.include_router(monitors_router)
app.include_router(pool_router)


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file, media_type="text/html")
    return HTMLResponse("<h1>OrchisX Engine</h1><p><a href='/docs'>Swagger API Docs</a></p>")


@app.get("/health")
async def health():
    return {"status": "ok"}
