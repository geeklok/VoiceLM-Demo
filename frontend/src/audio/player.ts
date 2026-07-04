/** 将流式 Int16 PCM 块累积并用 Web Audio 播放, 同时收集为可下载 WAV。 */
export class StreamingPcmPlayer {
  private ctx: AudioContext;
  private sampleRate = 24000;
  private nextTime = 0;
  private chunks: Int16Array[] = [];
  private sources: Set<AudioBufferSourceNode> = new Set();

  constructor() {
    this.ctx = new AudioContext();
  }

  setSampleRate(sr: number): void {
    this.sampleRate = sr;
  }

  push(pcm16: Int16Array): void {
    this.chunks.push(pcm16);
    const f32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) f32[i] = pcm16[i] / 32768;
    const buf = this.ctx.createBuffer(1, f32.length, this.sampleRate);
    buf.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    const now = this.ctx.currentTime;
    const start = Math.max(now, this.nextTime);
    src.start(start);
    this.nextTime = start + buf.duration;
    // 保留引用以便 barge-in 打断时停掉所有已排队 source。
    this.sources.add(src);
    src.onended = () => this.sources.delete(src);
  }

  /** barge-in 打断: 立即停掉所有已排队/在播的音频, 复位播放游标。 */
  stop(): void {
    for (const src of this.sources) {
      try {
        src.onended = null;
        src.stop();
      } catch {
        /* 已停止/未开始, 忽略 */
      }
    }
    this.sources.clear();
    this.nextTime = 0;
  }

  /** 合并所有块为 WAV Blob, 供下载。 */
  toWavBlob(): Blob {
    const total = this.chunks.reduce((n, c) => n + c.length, 0);
    const merged = new Int16Array(total);
    let off = 0;
    for (const c of this.chunks) {
      merged.set(c, off);
      off += c.length;
    }
    return encodeWav(merged, this.sampleRate);
  }

  close(): void {
    this.ctx.close();
  }
}

function encodeWav(pcm: Int16Array, sampleRate: number): Blob {
  const dataSize = pcm.length * 2;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, dataSize, true);
  let off = 44;
  for (let i = 0; i < pcm.length; i++, off += 2) view.setInt16(off, pcm[i], true);
  return new Blob([buffer], { type: "audio/wav" });
}
