from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.websockets import WebSocketDisconnect

from app.config import get_settings
from app.orchestration.dispatcher import Dispatcher
from app.orchestration.limiter import ConcurrencyLimitError
from app.schemas.models import ASRResponse
from app.utils.errors import AudioProcessingError
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _dispatcher(request: Request) -> Dispatcher:
    return request.app.state.dispatcher


@router.post("/api/v1/asr", response_model=ASRResponse)
async def asr_file(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form("auto"),
    hotwords: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
) -> ASRResponse:
    settings = get_settings()
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="音频文件过大")
    if not data:
        raise HTTPException(status_code=400, detail="空文件")

    hw = [w.strip() for w in hotwords.split(",") if w.strip()] if hotwords else None
    try:
        return await _dispatcher(request).asr_file(
            data, language=language, hotwords=hw, model=model
        )
    except ConcurrencyLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except AudioProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.websocket("/ws/asr")
async def asr_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    dispatcher: Dispatcher = websocket.app.state.dispatcher
    sample_rate = 16000
    channels = 1
    language = "auto"
    model: Optional[str] = None
    hotwords: Optional[list[str]] = None

    # 第一帧: start 配置
    try:
        start = await websocket.receive_json()
    except (WebSocketDisconnect, json.JSONDecodeError):
        await websocket.close()
        return
    if start.get("type") == "start":
        sample_rate = int(start.get("sample_rate", 16000))
        channels = int(start.get("channels", 1))
        language = start.get("language", "auto")
        model = start.get("model") or None
        # 热词: 逗号分隔字符串 (与文件式 /api/v1/asr 一致)。仅对 funasr-streaming
        # 的句末定稿生效 (改用 seaco 重解码); 其它引擎忽略。
        hw = start.get("hotwords")
        if hw:
            hotwords = [w.strip() for w in hw.split(",") if w.strip()] or None

    async def chunk_iter() -> AsyncIterator[tuple[np.ndarray, int, int]]:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                pcm = np.frombuffer(msg["bytes"], dtype=np.float32).copy()
                yield pcm, sample_rate, channels
            elif "text" in msg and msg["text"] is not None:
                ctrl = json.loads(msg["text"])
                if ctrl.get("type") == "end":
                    break

    try:
        async for partial in dispatcher.asr_stream(
            chunk_iter(), language=language, model=model, hotwords=hotwords
        ):
            await websocket.send_json(
                {
                    "type": "final" if partial.is_final else "partial",
                    "text": partial.text,
                    "segment_id": partial.segment_id,
                    "node": dispatcher.node,
                }
            )
        await websocket.close()
    except WebSocketDisconnect:
        logger.info("asr ws disconnected")
    except ConcurrencyLimitError as exc:
        try:
            await websocket.send_json(
                {"type": "error", "code": "busy", "retry_after": exc.retry_after, "message": str(exc)}
            )
            await websocket.close()
        except RuntimeError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("asr ws error")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close()
        except RuntimeError:
            pass
