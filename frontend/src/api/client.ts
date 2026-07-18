export interface ASRSegment {
  text: string;
  start_ms?: number;
  end_ms?: number;
}

export interface ASRResponse {
  text: string;
  segments: ASRSegment[];
  audio_duration_ms: number;
  process_ms: number;
  rtf: number;
  model: string;
  degraded?: boolean;
  node?: string;
}

export interface TtsQos {
  node?: string;
  model?: string;
  mode?: string;
  ttfb_ms?: number | null;
  process_ms?: number | null;
  audio_ms?: number | null;
  rtf?: number | null;
}

export interface TtsFileResult {
  blob: Blob;
  qos: TtsQos;
}

export interface ModelInfo {
  name: string;
  kind: string;
  expected_sample_rate: number;
  languages: string[];
  default?: boolean;
  supports_hotwords?: boolean;
}

export interface ModelsResponse {
  asr: ModelInfo[];
  tts: ModelInfo[];
}

export type ChatMode = "cascade" | "native";
export type ChatInputFormat = "pcm_f32le" | "pcm_s16le";

export interface ChatModelInfo {
  name: string;
  label: string;
  mode: ChatMode;
  provider: string;
  voices: string[];
  default: boolean;
  input_format: ChatInputFormat;
  supports_thinking: boolean;
  supports_barge_in: boolean;
  supports_vad_gate: boolean;
  preserves_paralinguistics: boolean;
  default_barge_in: boolean;
  default_vad_gate: boolean;
  default_capture_profile: CaptureProfile;
  vad_silence_ms_options: number[];
  default_vad_silence_ms?: number | null;
}

export type CaptureProfile = "natural" | "noise_reduction";

export interface ChatModelsResponse {
  models: ChatModelInfo[];
}

// 领域 TN 类别 (后端 domain_tn.py 单一事实来源, 前端动态拉取渲染)。
export interface TnCategory {
  id: string;
  label: string;
  impl: boolean;
}

function wsBase(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}`;
}

async function responseError(r: Response, fallback: string): Promise<Error> {
  const body = await r.json().catch(() => ({}));
  return new Error(body.detail || `${fallback} ${r.status}`);
}

function armWebSocket(
  ws: WebSocket,
  onTimeout: () => void,
  timeoutMs = 10000
): () => void {
  const timer = window.setTimeout(() => {
    if (ws.readyState === WebSocket.CONNECTING) {
      onTimeout();
      ws.close();
    }
  }, timeoutMs);
  return () => window.clearTimeout(timer);
}

export async function fetchModels(): Promise<ModelsResponse> {
  const r = await fetch("/api/v1/models");
  if (!r.ok) throw new Error(`models ${r.status}`);
  return r.json();
}

export async function fetchChatModels(): Promise<ChatModelsResponse> {
  const r = await fetch("/api/v1/chat/models");
  if (!r.ok) throw new Error(`chat models ${r.status}`);
  return r.json();
}

export async function fetchTnCategories(): Promise<TnCategory[]> {
  const r = await fetch("/api/v1/tn-categories");
  if (!r.ok) throw new Error(`tn-categories ${r.status}`);
  return r.json();
}

export async function checkReady(): Promise<boolean> {
  try {
    const r = await fetch("/readyz");
    return r.ok && (await r.json()).ready;
  } catch {
    return false;
  }
}

export async function asrFile(
  file: Blob,
  language: string,
  hotwords?: string,
  model?: string
): Promise<ASRResponse> {
  const fd = new FormData();
  fd.append("file", file, "audio.wav");
  fd.append("language", language);
  if (hotwords) fd.append("hotwords", hotwords);
  if (model) fd.append("model", model);
  const r = await fetch("/api/v1/asr", { method: "POST", body: fd });
  if (!r.ok) throw await responseError(r, "asr");
  return r.json();
}

export async function ttsFile(
  text: string,
  voice: string,
  speed: number,
  domainTn?: string[]
): Promise<TtsFileResult> {
  const r = await fetch("/api/v1/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      voice,
      speed,
      format: "wav",
      domain_tn: domainTn && domainTn.length ? domainTn : undefined,
    }),
  });
  if (!r.ok) throw await responseError(r, "tts");
  const num = (h: string): number | null => {
    const v = r.headers.get(h);
    return v === null || v === "" ? null : Number(v);
  };
  const qos: TtsQos = {
    node: r.headers.get("X-Node") || undefined,
    model: r.headers.get("X-Model") || undefined,
    process_ms: num("X-Process-Ms"),
    audio_ms: num("X-Audio-Ms"),
    rtf: num("X-RTF"),
  };
  return { blob: await r.blob(), qos };
}

export interface AsrStreamHandlers {
  onReady: (node?: string, model?: string) => void;
  onPartial: (
    text: string,
    isFinal: boolean,
    segmentId: number,
    node?: string
  ) => void;
  onError: (message: string, code?: string, retryAfter?: number) => void;
  onClose: (expected: boolean) => void;
}

export function openAsrStream(
  sampleRate: number,
  language: string,
  handlers: AsrStreamHandlers,
  model?: string,
  hotwords?: string
): WebSocket {
  const ws = new WebSocket(`${wsBase()}/ws/asr`);
  ws.binaryType = "arraybuffer";
  let expectedClose = false;
  const clearTimeout = armWebSocket(ws, () =>
    handlers.onError("连接 ASR 服务超时", "timeout")
  );
  ws.onopen = () => {
    clearTimeout();
    ws.send(
      JSON.stringify({
        type: "start",
        sample_rate: sampleRate,
        channels: 1,
        language,
        model,
        hotwords: hotwords || undefined,
      })
    );
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "ready") handlers.onReady(msg.node, msg.model);
      else if (msg.type === "partial")
        handlers.onPartial(msg.text, false, msg.segment_id ?? 0, msg.node);
      else if (msg.type === "final")
        handlers.onPartial(msg.text, true, msg.segment_id ?? 0, msg.node);
      else if (msg.type === "error") {
        expectedClose = true;
        handlers.onError(msg.message, msg.code, msg.retry_after);
      }
    } catch {
      handlers.onError("ASR 服务返回了无法解析的消息", "protocol");
    }
  };
  ws.onerror = () => handlers.onError("ASR WebSocket 错误", "ws");
  ws.onclose = () => {
    clearTimeout();
    handlers.onClose(expectedClose);
  };
  return ws;
}

export interface ChatQos {
  node?: string;
  first_token_ms?: number | null;
  tts_ttfb_ms?: number | null;
  total_ms?: number | null;
}

export interface ChatStartOptions {
  sampleRate: number;
  language: string;
  mode: ChatMode;
  inputFormat: ChatInputFormat;
  systemPrompt?: string;
  voice?: string;
  speed?: number;
  bargeIn?: boolean;
  model?: string;
  enableThinking?: boolean;
  vadSilenceMs?: number;
}

export interface ChatHandlers {
  onReady?: (node?: string) => void;
  onState?: (state: string, node?: string) => void;
  onUserPartial?: (text: string, node?: string) => void;
  onUserFinal?: (text: string, node?: string) => void;
  onAssistantPartial?: (text: string, node?: string) => void;
  onTtsMeta?: (sampleRate: number, node?: string, model?: string) => void;
  onAudio?: (pcm: Int16Array) => void;
  onAssistantDone?: (text: string, qos?: ChatQos, node?: string) => void;
  onInterrupted?: (text?: string, node?: string) => void;
  onError?: (code: string, message: string) => void;
  onClose?: (expected: boolean) => void;
}

// 语音对话 (speech-to-speech): 上行 start 帧 + float32 16k PCM; 下行 ready/state/
// user_partial/user_final/assistant_partial/tts_meta + 二进制 int16 音频/assistant_done/error。
export function openChatStream(opts: ChatStartOptions, h: ChatHandlers): WebSocket {
  const ws = new WebSocket(`${wsBase()}/ws/chat`);
  ws.binaryType = "arraybuffer";
  let expectedClose = false;
  const clearTimeout = armWebSocket(ws, () =>
    h.onError?.("timeout", "连接语音聊天服务超时")
  );
  ws.onopen = () => {
    clearTimeout();
    ws.send(
      JSON.stringify({
        type: "start",
        mode: opts.mode,
        sample_rate: opts.sampleRate,
        channels: 1,
        input_format: opts.inputFormat,
        language: opts.language,
        system_prompt: opts.systemPrompt || undefined,
        voice: opts.voice || undefined,
        speed: opts.speed,
        barge_in: opts.bargeIn,
        model: opts.model || undefined,
        enable_thinking: opts.enableThinking,
        vad_silence_ms: opts.vadSilenceMs,
      })
    );
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== "string") {
      h.onAudio?.(new Int16Array(ev.data as ArrayBuffer));
      return;
    }
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      h.onError?.("protocol", "语音聊天服务返回了无法解析的消息");
      return;
    }
    switch (msg.type) {
      case "ready":
        h.onReady?.(msg.node);
        break;
      case "state":
        h.onState?.(msg.state, msg.node);
        break;
      case "user_partial":
        h.onUserPartial?.(msg.text, msg.node);
        break;
      case "user_final":
        h.onUserFinal?.(msg.text, msg.node);
        break;
      case "assistant_partial":
        h.onAssistantPartial?.(msg.text, msg.node);
        break;
      case "tts_meta":
        h.onTtsMeta?.(msg.sample_rate, msg.node, msg.model);
        break;
      case "assistant_done":
        h.onAssistantDone?.(msg.text, msg.qos, msg.node);
        break;
      case "interrupted":
        h.onInterrupted?.(msg.text, msg.node);
        break;
      case "error":
        expectedClose = true;
        h.onError?.(msg.code || "error", msg.message || "未知错误");
        break;
    }
  };
  ws.onerror = () => h.onError?.("ws", "WebSocket 错误");
  ws.onclose = () => {
    clearTimeout();
    h.onClose?.(expectedClose);
  };
  return ws;
}

export function openTtsStream(
  text: string,
  voice: string,
  speed: number,
  onMeta: (sampleRate: number, node?: string, model?: string) => void,
  onChunk: (pcm: Int16Array) => void,
  onDone: (qos?: TtsQos) => void,
  onError: (msg: string) => void,
  onClose: (expected: boolean) => void,
  domainTn?: string[]
): WebSocket {
  const ws = new WebSocket(`${wsBase()}/ws/tts`);
  ws.binaryType = "arraybuffer";
  let expectedClose = false;
  const clearTimeout = armWebSocket(ws, () =>
    onError("连接 TTS 服务超时")
  );
  ws.onopen = () => {
    clearTimeout();
    ws.send(
      JSON.stringify({
        type: "synthesize",
        text,
        voice,
        speed,
        domain_tn: domainTn && domainTn.length ? domainTn : undefined,
      })
    );
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "meta") onMeta(msg.sample_rate, msg.node, msg.model);
        else if (msg.type === "done") {
          expectedClose = true;
          onDone(msg.qos);
        } else if (msg.type === "error") {
          expectedClose = true;
          onError(msg.message);
        }
      } catch {
        onError("TTS 服务返回了无法解析的消息");
      }
    } else {
      onChunk(new Int16Array(ev.data as ArrayBuffer));
    }
  };
  ws.onerror = () => onError("WebSocket 错误");
  ws.onclose = () => {
    clearTimeout();
    onClose(expectedClose);
  };
  return ws;
}
