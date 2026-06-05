"""
TelemetryPublisher: async Redis Streams publisher for robot telemetry.

Features:
- Connects to Redis using redis.asyncio
- Publishes to `telemetry:stream` Redis Stream
- Backpressure: trims stream when length > 10 000
- Batching: buffers up to 50 messages then flushes with pipeline
- Prometheus counter for published packets
- Exponential backoff retry on connection failures
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import redis.asyncio as aioredis
import structlog
from prometheus_client import Counter, Gauge

from .models import TelemetryPacket

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

TELEMETRY_PACKETS_PUBLISHED = Counter(
    "telemetry_packets_published_total",
    "Total number of telemetry packets published to Redis Streams",
    ["robot_id"],
)

REDIS_PUBLISH_ERRORS = Counter(
    "telemetry_redis_publish_errors_total",
    "Total Redis publish errors",
)

STREAM_LENGTH_GAUGE = Gauge(
    "telemetry_stream_length",
    "Current length of the telemetry Redis stream",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STREAM_KEY = "telemetry:stream"
MAX_STREAM_LEN = 10_000
BATCH_SIZE = 50
FLUSH_INTERVAL_S = 0.1          # Max time to hold a partial batch
MAX_BACKOFF_S = 60.0
INITIAL_BACKOFF_S = 0.5


class TelemetryPublisher:
    """
    Batching, backpressure-aware Redis Streams publisher.

    Typical usage::

        publisher = TelemetryPublisher(redis_url="redis://localhost:6379")
        await publisher.connect()
        await publisher.publish(packet)
        await publisher.close()
    """

    def __init__(
        self,
        redis_url: str = "redis://redis:6379",
        stream_key: str = STREAM_KEY,
        max_stream_len: int = MAX_STREAM_LEN,
        batch_size: int = BATCH_SIZE,
        flush_interval_s: float = FLUSH_INTERVAL_S,
    ) -> None:
        self._redis_url = redis_url
        self._stream_key = stream_key
        self._max_stream_len = max_stream_len
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s

        self._redis: Optional[aioredis.Redis] = None
        self._buffer: list[TelemetryPacket] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._connected = False
        self._running = False

        self._log = logger.bind(component="telemetry_publisher")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish Redis connection with retry."""
        backoff = INITIAL_BACKOFF_S
        attempt = 0
        while True:
            try:
                self._redis = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=10,
                    retry_on_timeout=True,
                    health_check_interval=30,
                )
                await self._redis.ping()
                self._connected = True
                self._running = True
                self._log.info("redis_connected", url=self._redis_url)

                # Start background flush loop
                self._flush_task = asyncio.create_task(
                    self._flush_loop(), name="telemetry_flush_loop"
                )
                return

            except Exception as exc:
                attempt += 1
                self._log.warning(
                    "redis_connect_failed",
                    attempt=attempt,
                    backoff_s=backoff,
                    error=str(exc),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def close(self) -> None:
        """Flush remaining messages and close the Redis connection."""
        self._running = False

        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self._flush_buffer()

        if self._redis:
            await self._redis.aclose()
            self._redis = None
            self._connected = False
        self._log.info("telemetry_publisher_closed")

    # ------------------------------------------------------------------
    # Public publish interface
    # ------------------------------------------------------------------

    async def publish(self, packet: TelemetryPacket) -> None:
        """
        Buffer a packet for publishing.  If buffer reaches batch_size,
        the flush is triggered immediately.
        """
        async with self._buffer_lock:
            self._buffer.append(packet)
            should_flush = len(self._buffer) >= self._batch_size

        if should_flush:
            await self._flush_buffer()

    async def publish_fault_event(self, robot_id: str, fault_type: str, data: dict) -> None:
        """Publish a fault event to the faults:stream."""
        if not self._redis or not self._connected:
            return
        try:
            payload = json.dumps({
                "robot_id": robot_id,
                "fault_type": fault_type,
                "timestamp": time.time(),
                **data,
            })
            await self._redis.xadd("faults:stream", {"data": payload, "robot_id": robot_id})
        except Exception as exc:
            self._log.error("fault_event_publish_error", error=str(exc))

    # ------------------------------------------------------------------
    # Private: flush logic
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Background task: flush buffer every flush_interval_s."""
        try:
            while self._running:
                await asyncio.sleep(self._flush_interval_s)
                await self._flush_buffer()
        except asyncio.CancelledError:
            pass

    async def _flush_buffer(self) -> None:
        """Drain the buffer and send via Redis pipeline."""
        async with self._buffer_lock:
            if not self._buffer:
                return
            batch = self._buffer.copy()
            self._buffer.clear()

        if not self._redis or not self._connected:
            self._log.warning("flush_skipped_no_connection", batch_size=len(batch))
            return

        await self._send_batch_with_retry(batch)

    async def _send_batch_with_retry(
        self, batch: list[TelemetryPacket], max_attempts: int = 5
    ) -> None:
        backoff = INITIAL_BACKOFF_S
        for attempt in range(1, max_attempts + 1):
            try:
                await self._send_batch(batch)
                return
            except Exception as exc:
                REDIS_PUBLISH_ERRORS.inc()
                self._log.warning(
                    "redis_send_failed",
                    attempt=attempt,
                    batch_size=len(batch),
                    error=str(exc),
                )
                if attempt == max_attempts:
                    self._log.error(
                        "redis_send_abandoned",
                        batch_size=len(batch),
                        error=str(exc),
                    )
                    return

                # Try to reconnect
                await self._reconnect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _send_batch(self, batch: list[TelemetryPacket]) -> None:
        """Send a batch of packets via Redis pipeline."""
        if not self._redis:
            raise RuntimeError("Redis not connected")

        pipe = self._redis.pipeline(transaction=False)

        # Check stream length for backpressure trimming (once per batch)
        stream_len = await self._redis.xlen(self._stream_key)
        STREAM_LENGTH_GAUGE.set(stream_len)

        needs_trim = stream_len > self._max_stream_len

        for packet in batch:
            fields = {
                "data": packet.model_dump_json(),
                "robot_id": packet.robot_id,
                "ts": str(packet.timestamp),
            }
            if needs_trim:
                pipe.xadd(
                    self._stream_key,
                    fields,
                    maxlen=self._max_stream_len,
                    approximate=True,
                )
            else:
                pipe.xadd(self._stream_key, fields)

        await pipe.execute()

        # Update Prometheus counters per robot
        robot_counts: dict[str, int] = {}
        for packet in batch:
            robot_counts[packet.robot_id] = robot_counts.get(packet.robot_id, 0) + 1
        for robot_id, count in robot_counts.items():
            TELEMETRY_PACKETS_PUBLISHED.labels(robot_id=robot_id).inc(count)

        self._log.debug(
            "batch_flushed",
            batch_size=len(batch),
            stream_len=stream_len,
            trimmed=needs_trim,
        )

    async def _reconnect(self) -> None:
        """Attempt to re-establish the Redis connection."""
        self._connected = False
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

        try:
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=10,
                retry_on_timeout=True,
            )
            await self._redis.ping()
            self._connected = True
            self._log.info("redis_reconnected")
        except Exception as exc:
            self._log.error("redis_reconnect_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Health check helper
    # ------------------------------------------------------------------

    async def is_healthy(self) -> bool:
        """Return True if the Redis connection is alive."""
        if not self._redis or not self._connected:
            return False
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False
