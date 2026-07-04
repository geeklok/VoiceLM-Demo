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

function wsBase(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}`;
}

export async function fetchModels(): Promise<ModelsResponse> {
  const r = await fetch("/api/v1/models");
  if (!r.ok) throw new Error(`models ${r.status}`);
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
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || `asr ${r.status}`);
  }
  return r.json();
}

export async function ttsFile(
  text: string,
  voice: string,
  speed: number
): Promise<TtsFileResult> {
  const r = await fetch("/api/v1/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, voice, speed, format: "wav" }),
  });
  if (!r.ok) throw new Error(`tts ${r.status}`);
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

export function openAsrStream(
  sampleRate: number,
  language: string,
  onPartial: (text: string, isFinal: boolean, segmentId: number, node?: string) => void,
  onError: (msg: string) => void,
  model?: string,
  hotwords?: string
): WebSocket {
  const ws = new WebSocket(`${wsBase()}/ws/asr`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () =>
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
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "partial") onPartial(msg.text, false, msg.segment_id ?? 0, msg.node);
    else if (msg.type === "final") onPartial(msg.text, true, msg.segment_id ?? 0, msg.node);
    else if (msg.type === "error") onError(msg.message);
  };
  ws.onerror = () => onError("WebSocket 错误");
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
  systemPrompt?: string;
  voice?: string;
  speed?: number;
  bargeIn?: boolean;
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
  onInterrupted?: (node?: string) => void;
  onError?: (code: string, message: string) => void;
}

// 语音对话 (speech-to-speech): 上行 start 帧 + float32 16k PCM; 下行 ready/state/
// user_partial/user_final/assistant_partial/tts_meta + 二进制 int16 音频/assistant_done/error。
export function openChatStream(opts: ChatStartOptions, h: ChatHandlers): WebSocket {
  const ws = new WebSocket(`${wsBase()}/ws/chat`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () =>
    ws.send(
      JSON.stringify({
        type: "start",
        sample_rate: opts.sampleRate,
        channels: 1,
        language: opts.language,
        system_prompt: opts.systemPrompt || undefined,
        voice: opts.voice || undefined,
        speed: opts.speed,
        barge_in: opts.bargeIn,
      })
    );
  ws.onmessage = (ev) => {
    if (typeof ev.data !== "string") {
      h.onAudio?.(new Int16Array(ev.data as ArrayBuffer));
      return;
    }
    const msg = JSON.parse(ev.data);
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
        h.onInterrupted?.(msg.node);
        break;
      case "error":
        h.onError?.(msg.code || "error", msg.message || "未知错误");
        break;
    }
  };
  ws.onerror = () => h.onError?.("ws", "WebSocket 错误");
  return ws;
}

export function openTtsStream(
  text: string,
  voice: string,
  speed: number,
  onMeta: (sampleRate: number, node?: string, model?: string) => void,
  onChunk: (pcm: Int16Array) => void,
  onDone: (qos?: TtsQos) => void,
  onError: (msg: string) => void
): WebSocket {
  const ws = new WebSocket(`${wsBase()}/ws/tts`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(JSON.stringify({ type: "synthesize", text, voice, speed }));
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      const msg = JSON.parse(ev.data);
      if (msg.type === "meta") onMeta(msg.sample_rate, msg.node, msg.model);
      else if (msg.type === "done") onDone(msg.qos);
      else if (msg.type === "error") onError(msg.message);
    } else {
      onChunk(new Int16Array(ev.data as ArrayBuffer));
    }
  };
  ws.onerror = () => onError("WebSocket 错误");
  return ws;
}
