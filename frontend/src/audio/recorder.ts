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
 */
export class MicRecorder {
  private ctx?: AudioContext;
  private stream?: MediaStream;
  private processor?: ScriptProcessorNode;
  private source?: MediaStreamAudioSourceNode;

  constructor(private onChunk: (pcm16k: Float32Array) => void) {}

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
      this.onChunk(resampled);
    };
    this.source.connect(this.processor);
    this.processor.connect(this.ctx.destination);
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
  }
}

export { TARGET_SR };
