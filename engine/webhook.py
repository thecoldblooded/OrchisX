import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from typing import Optional, Dict, Any

import httpx
from config import settings
from core.database import get_db_session
from core.models import WebhookLog, utc_now

logger = logging.getLogger("orchis.webhook")


class WebhookDispatcher:
    def __init__(self, timeout: float = settings.WEBHOOK_TIMEOUT, max_retries: int = settings.WEBHOOK_RETRY_ATTEMPTS):
        self.timeout = timeout
        self.max_retries = max_retries

    @staticmethod
    def compute_signature(secret: str, payload_bytes: bytes) -> str:
        """Calculate HMAC-SHA256 signature for webhook payload verification."""
        digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    async def dispatch(
        self,
        monitor_id: str,
        webhook_url: str,
        webhook_secret: str,
        event_type: str,
        payload_data: Dict[str, Any]
    ) -> bool:
        """
        Send HMAC-signed webhook event with exponential retry and audit logging.
        """
        envelope = {
            "event": event_type,
            "monitor_id": monitor_id,
            "timestamp": utc_now().isoformat(),
            "data": payload_data
        }
        payload_json = json.dumps(envelope, ensure_ascii=False)
        payload_bytes = payload_json.encode("utf-8")
        signature = self.compute_signature(webhook_secret, payload_bytes)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "OrchisX-Webhook-Engine/1.0",
            "X-Orchis-Signature": signature,
            "X-Event-Type": event_type,
        }

        success = False
        last_status_code = None
        last_response_body = None

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(webhook_url, content=payload_bytes, headers=headers)
                    last_status_code = resp.status_code
                    last_response_body = resp.text[:1000]

                    if 200 <= resp.status_code < 300:
                        success = True
                        logger.info(f"Webhook delivered for monitor {monitor_id} (HTTP {resp.status_code}) on attempt {attempt}")
                        break
                    else:
                        logger.warning(f"Webhook rejected for monitor {monitor_id} with HTTP {resp.status_code} (attempt {attempt}/{self.max_retries})")
            except Exception as e:
                last_response_body = str(e)[:1000]
                logger.warning(f"Webhook connection error for monitor {monitor_id}: {e} (attempt {attempt}/{self.max_retries})")

            if attempt < self.max_retries:
                await asyncio.sleep(2 ** (attempt - 1))

        # Log delivery to database
        try:
            async with get_db_session() as session:
                log_entry = WebhookLog(
                    monitor_id=monitor_id,
                    event_type=event_type,
                    payload=payload_json,
                    status_code=last_status_code,
                    response_body=last_response_body,
                    attempt=attempt,
                    success=success
                )
                session.add(log_entry)
                await session.commit()
        except Exception as e:
            logger.error(f"Failed to log webhook delivery to DB: {e}")

        return success


webhook_dispatcher = WebhookDispatcher()
