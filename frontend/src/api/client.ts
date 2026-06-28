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
  model?: string
): WebSocket {
  const ws = new WebSocket(`${wsBase()}/ws/asr`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () =>
    ws.send(
      JSON.stringify({ type: "start", sample_rate: sampleRate, channels: 1, language, model })
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
