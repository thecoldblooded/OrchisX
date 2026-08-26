import os
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, Path, Header
from fastapi.responses import FileResponse
from api.schemas import CreateExtractionRequest, ExtractionJobResponse
from engine.extraction import extraction_service
from scraper.filters import TweetFilter
from core.models import utc_now
router = APIRouter(prefix="/api/v1/extractions", tags=["Bulk Extractions"])


def format_job_response(job) -> ExtractionJobResponse:
    download_url = f"/api/v1/extractions/{job.id}/download" if getattr(job, "output_file_path", None) and os.path.exists(job.output_file_path) else None
    return ExtractionJobResponse(
        id=str(job.id),
        tool_type=getattr(job, "tool_type", "search") or "search",
        query=getattr(job, "query", ""),
        results_limit=getattr(job, "results_limit", 100) or 100,
        status=getattr(job, "status", "queued") or "queued",
        collected_count=getattr(job, "collected_count", 0) or 0,
        format=getattr(job, "format", "csv") or "csv",
        output_file_path=getattr(job, "output_file_path", None),
        download_url=download_url,
        error_message=getattr(job, "error_message", None),
        auto_resume_at=getattr(job, "auto_resume_at", None),
        created_at=getattr(job, "created_at", None) or utc_now(),
        completed_at=getattr(job, "completed_at", None),
    )

@router.post("", response_model=ExtractionJobResponse)
async def create_bulk_extraction(
    req: CreateExtractionRequest,
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """
    Trigger a background bulk extraction job to collect up to 50,000 tweets to CSV or JSON.
    """
    filters = TweetFilter(
        min_likes=req.min_likes,
        min_retweets=req.min_retweets,
        language=req.language,
        replies=req.replies,
    )
    job = await extraction_service.create_job(
        query=req.query,
        results_limit=req.results_limit,
        tool_type=req.tool_type,
        export_format=req.format,
        filters=filters,
        session_id=x_session_id
    )
    return format_job_response(job)


@router.get("", response_model=List[ExtractionJobResponse])
async def list_extractions(
    limit: int = Query(50, ge=1, le=100),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID")
):
    """List recent bulk extraction jobs for this session."""
    jobs = await extraction_service.list_jobs(limit=limit, session_id=x_session_id)
    return [format_job_response(j) for j in jobs]


@router.get("/{id}", response_model=ExtractionJobResponse)
async def get_extraction_status(id: str = Path(..., description="Extraction Job ID")):
    """Get real-time status and collected count for an extraction job."""
    job = await extraction_service.get_job(id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return format_job_response(job)


@router.get("/{id}/download")
async def download_extraction_result(id: str = Path(..., description="Extraction Job ID")):
    """Download exported CSV or JSON result file."""
    job = await extraction_service.get_job(id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")

    if not job.output_file_path or not os.path.exists(job.output_file_path):
        raise HTTPException(status_code=400, detail="Extraction output file not ready or job failed")

    media_type = "text/csv" if job.format == "csv" else "application/json"
    filename = os.path.basename(job.output_file_path)
    return FileResponse(
        path=job.output_file_path,
        media_type=media_type,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.post("/{id}/pause", response_model=ExtractionJobResponse)
async def pause_extraction(id: str = Path(..., description="Extraction Job ID")):
    """Pause an active extraction job and save its position."""
    success = await extraction_service.pause_job(id)
    job = await extraction_service.get_job(id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return format_job_response(job)


@router.post("/{id}/resume", response_model=ExtractionJobResponse)
async def resume_extraction(id: str = Path(..., description="Extraction Job ID")):
    """Resume a paused extraction job from where it left off."""
    success = await extraction_service.resume_job(id)
    job = await extraction_service.get_job(id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return format_job_response(job)


@router.post("/{id}/cancel", response_model=ExtractionJobResponse)
async def cancel_extraction(id: str = Path(..., description="Extraction Job ID")):
    """Cancel an extraction job."""
    success = await extraction_service.cancel_job(id)
    job = await extraction_service.get_job(id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return format_job_response(job)


@router.post("/{id}/retry", response_model=ExtractionJobResponse)
async def retry_extraction(id: str = Path(..., description="Extraction Job ID")):
    """Restart a failed or canceled extraction job from the beginning."""
    success = await extraction_service.retry_job(id)
    job = await extraction_service.get_job(id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return format_job_response(job)


@router.delete("/{id}")
async def delete_extraction(id: str = Path(..., description="Extraction Job ID")):
    """Delete an extraction job and its export files."""
    success = await extraction_service.delete_job(id)
    if not success:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return {"success": True, "message": f"Extraction job {id} deleted"}
