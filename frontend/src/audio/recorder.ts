const TARGET_SR = 16000;

/** 线性重采样 float32 PCM 到目标采样率。 */
function downsample(buffer: Float32Array, srcSr: number, dstSr: number): Float32Array {
  if (srcSr === dstSr) return buffer;
  const ratio = srcSr / dstSr;
  const newLen = Math.round(buffer.length / ratio);
  const out = new Float32Array(newLen);
  for (let i = 0; i < newLen; i++) {
    const idx = i * ratio;
    const i0 = Math.floor(idx);
    const i1 = Math.min(i0 + 1, buffer.length - 1);
    const frac = idx - i0;
    out[i] = buffer[i0] * (1 - frac) + buffer[i1] * frac;
  }
  return out;
}

/**
 * 麦克风录音 → 16kHz 单声道 float32 PCM 块。
 * MVP 用 ScriptProcessorNode (兼容性最好); 浏览器在浏览器端完成降采样,
 * 减轻后端压力 (见技术方案 5.5)。
 *
 * 可选能量 VAD 门控 (gateVad): 开启后只在"有声"窗口上送 PCM, 静音期不发,
 * 挡掉环境噪声/静音被上送后端误识别 (配合后端有效发言门控双保险)。
 * 用 RMS 能量 + 自适应噪声底 + hangover (掉阈值后继续送一小段, 避免切句尾)。
 */
export interface MicRecorderOptions {
  gateVad?: boolean;          // 是否启用能量 VAD 门控 (默认 false = 原持续上送)
  hangoverMs?: number;        // 判静音后继续上送时长 (默认 400ms, 防切句尾)
  energyMarginDb?: number;    // 高于噪声底多少 dB 判为有声 (默认 6dB)
}

export class MicRecorder {
  private ctx?: AudioContext;
  private stream?: MediaStream;
  private processor?: ScriptProcessorNode;
  private source?: MediaStreamAudioSourceNode;

  // 能量 VAD 状态
  private gateVad: boolean;
  private hangoverMs: number;
  private energyMarginDb: number;
  private noiseFloor = 1e-4;   // 自适应噪声底 (RMS), 初值很小
  private voiced = false;
  private hangoverUntil = 0;   // performance.now() 时间戳, 在此之前继续送

  constructor(
    private onChunk: (pcm16k: Float32Array) => void,
    opts: MicRecorderOptions = {}
  ) {
    this.gateVad = opts.gateVad ?? false;
    this.hangoverMs = opts.hangoverMs ?? 400;
    this.energyMarginDb = opts.energyMarginDb ?? 6;
  }

  async start(): Promise<void> {
    // 开浏览器端 AEC/降噪/自动增益: barge-in 场景抑制外放回声被麦克风采回,
    // 避免把 AI 自己的声音当成用户插话 (自打断)。免耳机的基本保障。
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    this.ctx = new AudioContext();
    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.processor = this.ctx.createScriptProcessor(4096, 1, 1);
    const srcSr = this.ctx.sampleRate;
    this.processor.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      const resampled = downsample(new Float32Array(input), srcSr, TARGET_SR);
      if (!this.gateVad || this.shouldSend(input)) {
        this.onChunk(resampled);
      }
    };
    this.source.connect(this.processor);
    this.processor.connect(this.ctx.destination);
  }

  /** 能量 VAD 判定 + hangover: 返回本块是否应上送。 */
  private shouldSend(frame: Float32Array): boolean {
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    const rms = Math.sqrt(sum / frame.length) || 1e-7;
    // 判定阈值 = 噪声底 * 10^(margin/20)
    const threshold = this.noiseFloor * Math.pow(10, this.energyMarginDb / 20);
    const now = performance.now();
    if (rms >= threshold) {
      this.voiced = true;
      this.hangoverUntil = now + this.hangoverMs;
    } else if (this.voiced && now >= this.hangoverUntil) {
      this.voiced = false;
    }
    // 静音时缓慢抬高/贴合噪声底 (自适应); 有声时不更新, 避免把人声算进底噪。
    if (!this.voiced) {
      this.noiseFloor = 0.95 * this.noiseFloor + 0.05 * rms;
    }
    return this.voiced;
  }

  stop(): void {
    this.processor?.disconnect();
    this.source?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close();
    this.processor = undefined;
    this.source = undefined;
    this.stream = undefined;
    this.ctx = undefined;
    this.voiced = false;
    this.hangoverUntil = 0;
  }
}

export { TARGET_SR };
