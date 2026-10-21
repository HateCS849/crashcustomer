"""Resumable SSE snapshots over durable records; no in-memory event queue."""
import asyncio
import hashlib
import json

from fastapi.responses import StreamingResponse


def encode_snapshot(record):
    data = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest(), data


async def event_frames(get_record, disconnected, last_id="", interval=.3, heartbeat=10):
    quiet = 0
    while not await disconnected():
        record = get_record()
        if record is None:
            return
        event_id, data = encode_snapshot(record)
        if event_id != last_id:
            yield f"id: {event_id}\nevent: update\ndata: {data}\n\n"
            last_id, quiet = event_id, 0
        if record.get("status") not in {"queued", "running", "replaying"}:
            yield f"event: done\ndata: {{\"id\":\"{event_id}\"}}\n\n"
            return
        await asyncio.sleep(interval)
        quiet += interval
        if quiet >= heartbeat:
            yield ": heartbeat\n\n"
            quiet = 0


def stream_records(request, get_record):
    return StreamingResponse(event_frames(get_record, request.is_disconnected,
                             request.headers.get("last-event-id", "")),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
