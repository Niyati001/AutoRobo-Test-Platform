"""
Notification Service — subscribes to Redis pub/sub channels for fault and anomaly events,
delivers alerts via WebSocket, webhooks, and in-memory notification store.

Port: 8008
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Deque, Dict, List, Optional, Set

import httpx
import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import BaseModel, HttpUrl

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger("notification-service")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8008"))

# Redis channels to subscribe to
CHANNELS = [
    "diagnostics:anomalies",
    "faults:events",
    "validation:results",
]

MAX_NOTIFICATIONS = 1000

# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

NOTIFICATIONS_SENT = Counter("notifications_sent_total", "Notifications sent", ["channel", "delivery"])
WEBHOOK_DELIVERIES = Counter("webhook_deliveries_total", "Webhook delivery attempts", ["status"])
ACTIVE_WS = Gauge("notification_active_ws", "Active WebSocket subscribers")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Notification(BaseModel):
    notification_id: str
    channel: str
    severity: str
    title: str
    body: str
    payload: Dict[str, Any]
    timestamp: float
    read: bool = False


class WebhookConfig(BaseModel):
    webhook_id: str
    url: str
    channels: List[str]
    secret: Optional[str] = None
    enabled: bool = True


class WebhookRequest(BaseModel):
    url: str
    channels: List[str] = ["diagnostics:anomalies", "faults:events", "validation:results"]
    secret: Optional[str] = None


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_redis: Optional[aioredis.Redis] = None
_notifications: Deque[Notification] = deque(maxlen=MAX_NOTIFICATIONS)
_webhooks: Dict[str, WebhookConfig] = {}
_ws_subscribers: Set[WebSocket] = set()
_pubsub_task: Optional[asyncio.Task] = None
_http_client: Optional[httpx.AsyncClient] = None


def _parse_notification(channel: str, data: str) -> Optional[Notification]:
    """Parse raw Redis pub/sub message into a Notification."""
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None

    severity = payload.get("severity", "INFO")
    if channel == "diagnostics:anomalies":
        metric = payload.get("metric", "unknown")
        robot_id = payload.get("robot_id", "?")
        title = f"Anomaly: {metric} on {robot_id}"
        body = payload.get("description", f"Anomaly detected in {metric}")
    elif channel == "faults:events":
        fault_type = payload.get("fault_type", "UNKNOWN")
        robot_id = payload.get("robot_id", "?")
        title = f"Fault: {fault_type} on {robot_id}"
        body = f"{fault_type} fault injected (severity: {severity})"
    elif channel == "validation:results":
        run_id = payload.get("run_id", "?")
        passed = payload.get("overall_passed", False)
        title = f"Validation {'PASSED' if passed else 'FAILED'}: {run_id}"
        body = f"Pass rate: {payload.get('pass_rate', 0):.1%}"
        severity = "LOW" if passed else "HIGH"
    else:
        title = f"Event on {channel}"
        body = str(data)[:200]

    return Notification(
        notification_id=str(uuid.uuid4()),
        channel=channel,
        severity=severity,
        title=title,
        body=body,
        payload=payload,
        timestamp=time.time(),
    )


async def _deliver_webhook(webhook: WebhookConfig, notification: Notification) -> None:
    if not webhook.enabled or _http_client is None:
        return
    if notification.channel not in webhook.channels:
        return
    try:
        headers = {"Content-Type": "application/json"}
        if webhook.secret:
            headers["X-Webhook-Secret"] = webhook.secret
        resp = await _http_client.post(
            webhook.url,
            content=notification.model_dump_json(),
            headers=headers,
            timeout=10.0,
        )
        status_bucket = "2xx" if resp.status_code < 300 else "4xx" if resp.status_code < 500 else "5xx"
        WEBHOOK_DELIVERIES.labels(status=status_bucket).inc()
        log.info("webhook_delivered", url=webhook.url, status=resp.status_code)
    except Exception as exc:
        WEBHOOK_DELIVERIES.labels(status="error").inc()
        log.warning("webhook_delivery_failed", url=webhook.url, error=str(exc))


async def _broadcast_notification(notification: Notification) -> None:
    """Persist notification and fan out to all delivery channels."""
    _notifications.appendleft(notification)
    msg = notification.model_dump_json()
    NOTIFICATIONS_SENT.labels(channel=notification.channel, delivery="store").inc()

    # Fan out to WebSocket subscribers
    dead = set()
    for ws in _ws_subscribers.copy():
        try:
            await ws.send_text(msg)
            NOTIFICATIONS_SENT.labels(channel=notification.channel, delivery="websocket").inc()
        except Exception:
            dead.add(ws)
    _ws_subscribers -= dead

    # Deliver to webhooks
    for webhook in _webhooks.values():
        if webhook.enabled and notification.channel in webhook.channels:
            asyncio.create_task(_deliver_webhook(webhook, notification))


async def _pubsub_listener() -> None:
    if _redis is None:
        return
    pubsub = _redis.pubsub()
    await pubsub.subscribe(*CHANNELS)
    log.info("pubsub_subscribed", channels=CHANNELS)

    while True:
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message and message.get("type") == "message":
                channel = message.get("channel", "")
                data = message.get("data", "")
                notification = _parse_notification(channel, data)
                if notification:
                    await _broadcast_notification(notification)
            else:
                await asyncio.sleep(0.05)
        except aioredis.RedisError as exc:
            log.warning("pubsub_redis_error", error=str(exc))
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("pubsub_error", error=str(exc))
            await asyncio.sleep(0.5)

    await pubsub.unsubscribe(*CHANNELS)
    await pubsub.aclose()
    log.info("pubsub_listener_stopped")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis, _pubsub_task, _http_client
    _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    _pubsub_task = asyncio.create_task(_pubsub_listener())
    log.info("notification_service_started", port=SERVICE_PORT)
    yield
    if _pubsub_task:
        _pubsub_task.cancel()
        try:
            await _pubsub_task
        except asyncio.CancelledError:
            pass
    if _redis:
        await _redis.aclose()
    if _http_client:
        await _http_client.aclose()
    log.info("notification_service_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Notification Service",
    version="1.0.0",
    description="Event-driven alerts via WebSocket, webhooks, and in-memory store",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)
SKIP_AUTH = {"/health", "/metrics", "/docs", "/openapi.json", "/redoc"}


async def verify_jwt(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if request.url.path in SKIP_AUTH:
        return {}
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        return jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict:
    redis_ok = False
    if _redis:
        try:
            await _redis.ping()
            redis_ok = True
        except Exception:
            pass
    return {
        "status": "ok" if redis_ok else "degraded",
        "service": "notification-service",
        "redis": "ok" if redis_ok else "error",
        "notification_count": len(_notifications),
        "webhook_count": len(_webhooks),
        "active_ws": len(_ws_subscribers),
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/notifications", dependencies=[Depends(verify_jwt)])
async def list_notifications(
    limit: int = Query(default=50, ge=1, le=500),
    unread_only: bool = Query(default=False),
    channel: Optional[str] = Query(default=None),
) -> dict:
    items = list(_notifications)
    if unread_only:
        items = [n for n in items if not n.read]
    if channel:
        items = [n for n in items if n.channel == channel]
    return {
        "total": len(items),
        "notifications": [n.model_dump() for n in items[:limit]],
    }


@app.patch("/notifications/{notification_id}/read", dependencies=[Depends(verify_jwt)])
async def mark_read(notification_id: str) -> dict:
    for n in _notifications:
        if n.notification_id == notification_id:
            n.read = True
            return {"status": "ok", "notification_id": notification_id}
    raise HTTPException(status_code=404, detail=f"Notification '{notification_id}' not found")


@app.patch("/notifications/read-all", dependencies=[Depends(verify_jwt)])
async def mark_all_read() -> dict:
    count = 0
    for n in _notifications:
        if not n.read:
            n.read = True
            count += 1
    return {"status": "ok", "marked_read": count}


@app.post("/notifications/webhooks", dependencies=[Depends(verify_jwt)], status_code=status.HTTP_201_CREATED)
async def register_webhook(req: WebhookRequest) -> WebhookConfig:
    webhook = WebhookConfig(
        webhook_id=str(uuid.uuid4()),
        url=req.url,
        channels=req.channels,
        secret=req.secret,
    )
    _webhooks[webhook.webhook_id] = webhook
    log.info("webhook_registered", webhook_id=webhook.webhook_id, url=webhook.url)
    return webhook


@app.get("/notifications/webhooks", dependencies=[Depends(verify_jwt)])
async def list_webhooks() -> List[WebhookConfig]:
    return list(_webhooks.values())


@app.delete("/notifications/webhooks/{webhook_id}", dependencies=[Depends(verify_jwt)])
async def delete_webhook(webhook_id: str) -> dict:
    if webhook_id not in _webhooks:
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")
    del _webhooks[webhook_id]
    return {"status": "deleted", "webhook_id": webhook_id}


@app.post("/notifications/test", dependencies=[Depends(verify_jwt)], status_code=status.HTTP_202_ACCEPTED)
async def send_test_notification(
    title: str = Query(default="Test Notification"),
    severity: str = Query(default="LOW"),
) -> dict:
    """Send a test notification to all subscribers."""
    notification = Notification(
        notification_id=str(uuid.uuid4()),
        channel="test",
        severity=severity,
        title=title,
        body="This is a test notification from the ARVP platform.",
        payload={"test": True},
        timestamp=time.time(),
    )
    await _broadcast_notification(notification)
    return {"status": "sent", "notification_id": notification.notification_id}


# ---------------------------------------------------------------------------
# WebSocket: live notification stream
# ---------------------------------------------------------------------------

@app.websocket("/ws/notifications")
async def ws_notifications(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_subscribers.add(websocket)
    ACTIVE_WS.inc()
    log.info("notification_ws_connected", total=len(_ws_subscribers))

    # Send last 10 unread notifications on connect
    recent = [n for n in list(_notifications)[:10]]
    if recent:
        await websocket.send_text(json.dumps({"type": "history", "notifications": [n.model_dump() for n in recent]}))

    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_subscribers.discard(websocket)
        ACTIVE_WS.dec()
        log.info("notification_ws_disconnected")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=False)
