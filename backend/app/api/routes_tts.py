from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response
from fastapi.websockets import WebSocketDisconnect

from app.orchestration.dispatcher import Dispatcher
from app.postprocess.audio_encode import pcm_to_int16_bytes, pcm_to_wav_bytes
from app.schemas.models import TTSRequest
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _dispatcher(request: Request) -> Dispatcher:
    return request.app.state.dispatcher


@router.post("/api/v1/tts")
async def tts_file(request: Request, req: TTSRequest) -> Response:
    try:
        pcm, sr = await _dispatcher(request).tts_file(req.text, req.voice, req.speed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("tts error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    wav = pcm_to_wav_bytes(pcm, sr)
    return Response(content=wav, media_type="audio/wav")


@router.websocket("/ws/tts")
async def tts_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    dispatcher: Dispatcher = websocket.app.state.dispatcher
    try:
        req = await websocket.receive_json()
    except (WebSocketDisconnect, json.JSONDecodeError):
        await websocket.close()
        return

    if req.get("type") != "synthesize":
        await websocket.send_json({"type": "error", "message": "首帧需为 synthesize"})
        await websocket.close()
        return

    text = req.get("text", "")
    voice = req.get("voice", "中文女")
    speed = float(req.get("speed", 1.0))

    try:
        stream, sr = await dispatcher.tts_stream(text, voice, speed)
        await websocket.send_json({"type": "meta", "sample_rate": sr, "format": "pcm_s16le"})
        async for pcm_chunk in stream:
            await websocket.send_bytes(pcm_to_int16_bytes(pcm_chunk))
        await websocket.send_json({"type": "done"})
        await websocket.close()
    except WebSocketDisconnect:
        logger.info("tts ws disconnected")
    except Exception as exc:  # noqa: BLE001
        logger.exception("tts ws error")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close()
        except RuntimeError:
            pass
