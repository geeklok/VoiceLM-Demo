from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response
from fastapi.websockets import WebSocketDisconnect

from app.orchestration.dispatcher import Dispatcher
from app.orchestration.limiter import ConcurrencyLimitError
from app.postprocess.audio_encode import pcm_to_int16_bytes, pcm_to_wav_bytes
from app.schemas.models import TnCategory, TTSRequest
from app.utils.domain_tn import DOMAIN_TN_CATEGORIES
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _dispatcher(request: Request) -> Dispatcher:
    return request.app.state.dispatcher


@router.get("/api/v1/tn-categories", response_model=list[TnCategory])
async def tn_categories() -> list[TnCategory]:
    """领域 TN 类别清单 (单一事实来源)。

    前端拉此接口动态渲染多选框, impl=False 标「实验性」。以往前端手写一份
    DOMAIN_TN_OPTIONS、后端一份 DOMAIN_TN_CATEGORIES, 转正时须改两处易漏; 现由后端
    唯一维护, 前端不再硬编码 (见《TTS文本处理链路-架构说明与排查手册》§5)。
    """
    return [
        TnCategory(id=key, label=label, impl=impl)
        for key, (impl, label) in DOMAIN_TN_CATEGORIES.items()
    ]


@router.post("/api/v1/tts")
async def tts_file(request: Request, req: TTSRequest) -> Response:
    try:
        pcm, sr, qos = await _dispatcher(request).tts_file(
            req.text, req.voice, req.speed, req.model, domain_tn=req.domain_tn
        )
    except ConcurrencyLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("tts error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    wav = pcm_to_wav_bytes(pcm, sr)
    # QoS 经响应头回传 (body 是音频二进制); 前端读 header 展示节点与本次时延。
    headers = {
        "X-Node": str(qos.get("node", "")),
        "X-Model": str(qos.get("model", "")),
        "X-Process-Ms": str(qos.get("process_ms", "")),
        "X-Audio-Ms": str(qos.get("audio_ms", "")),
        "X-RTF": "" if qos.get("rtf") is None else str(qos.get("rtf")),
        "Access-Control-Expose-Headers": "X-Node,X-Model,X-Process-Ms,X-Audio-Ms,X-RTF",
    }
    return Response(content=wav, media_type="audio/wav", headers=headers)


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
    model = req.get("model") or None
    domain_tn = req.get("domain_tn") or None

    try:
        stream, sr, meta, qos_holder = await dispatcher.tts_stream(
            text, voice, speed, model, domain_tn=domain_tn
        )
        await websocket.send_json(
            {
                "type": "meta",
                "sample_rate": sr,
                "format": "pcm_s16le",
                "node": meta.get("node", ""),
                "model": meta.get("model", ""),
            }
        )
        async for pcm_chunk in stream:
            await websocket.send_bytes(pcm_to_int16_bytes(pcm_chunk))
        # 流耗尽后 qos_holder 已被生成器填好; 随 done 帧把本次 QoS 下发给前端。
        await websocket.send_json({"type": "done", "qos": qos_holder})
        await websocket.close()
    except WebSocketDisconnect:
        logger.info("tts ws disconnected")
    except ConcurrencyLimitError as exc:
        try:
            await websocket.send_json(
                {"type": "error", "code": "busy", "retry_after": exc.retry_after, "message": str(exc)}
            )
            await websocket.close()
        except RuntimeError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("tts ws error")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close()
        except RuntimeError:
            pass
