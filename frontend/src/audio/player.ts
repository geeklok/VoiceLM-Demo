export interface StreamingPcmPlayerOptions {
  initialBufferMs?: number;
  maxBufferedMs?: number;
  retainAudio?: boolean;
}

export interface StreamingPcmPlayerStats {
  playbackUnderruns: number;
  droppedChunks: number;
}

/** 有界抖动缓冲的流式 Int16 PCM 播放器。 */
export class StreamingPcmPlayer {
  private ctx: AudioContext;
  private sampleRate = 24000;
  private nextTime = 0;
  private chunks: Int16Array[] = [];
  private pending: Int16Array[] = [];
  private pendingMs = 0;
  private sources: Set<AudioBufferSourceNode> = new Set();
  private started = false;
  private turnComplete = false;
  private playbackUnderruns = 0;
  private droppedChunks = 0;
  private initialBufferMs: number;
  private maxBufferedMs: number;
  private retainAudio: boolean;

  constructor(opts: StreamingPcmPlayerOptions = {}) {
    this.ctx = new AudioContext();
    this.initialBufferMs = opts.initialBufferMs ?? 100;
    this.maxBufferedMs = opts.maxBufferedMs ?? 120000;
    this.retainAudio = opts.retainAudio ?? true;
  }

  setSampleRate(sr: number): void {
    this.sampleRate = sr;
  }

  push(pcm16: Int16Array): void {
    if (!pcm16.length) return;
    // 上一回合已标记完成但排队音频尚未播完时，新音频代表下一回合。
    if (this.turnComplete) this.turnComplete = false;
    if (this.retainAudio) this.chunks.push(pcm16.slice());
    if (!this.started) {
      this.pending.push(pcm16);
      this.pendingMs += this.durationMs(pcm16);
      if (this.pendingMs >= this.initialBufferMs) this.flush();
      return;
    }
    this.schedule(pcm16);
  }

  /** 回复完成时放行不足启动水位的尾部音频。 */
  finishTurn(): void {
    this.flush();
    this.turnComplete = true;
    this.resetTurnIfDrained();
  }

  flush(): void {
    if (!this.pending.length) return;
    if (!this.started) {
      this.started = true;
      this.nextTime = this.ctx.currentTime + 0.02;
    }
    const pending = this.pending;
    this.pending = [];
    this.pendingMs = 0;
    for (const chunk of pending) this.schedule(chunk);
  }

  private schedule(pcm16: Int16Array): void {
    const now = this.ctx.currentTime;
    const durationMs = this.durationMs(pcm16);
    const bufferedMs = Math.max(0, this.nextTime - now) * 1000;
    if (bufferedMs + durationMs > this.maxBufferedMs) {
      this.droppedChunks += 1;
      return;
    }
    if (this.nextTime > 0 && this.nextTime < now - 0.02) {
      this.playbackUnderruns += 1;
      this.nextTime = now;
    }
    const f32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) f32[i] = pcm16[i] / 32768;
    const buf = this.ctx.createBuffer(1, f32.length, this.sampleRate);
    buf.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    const start = Math.max(now, this.nextTime);
    src.start(start);
    this.nextTime = start + buf.duration;
    // 保留引用以便 barge-in 打断时停掉所有已排队 source。
    this.sources.add(src);
    src.onended = () => {
      this.sources.delete(src);
      this.resetTurnIfDrained();
    };
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
    this.pending = [];
    this.pendingMs = 0;
    this.nextTime = 0;
    this.started = false;
    this.turnComplete = false;
    if (!this.retainAudio) this.chunks = [];
  }

  getStats(): StreamingPcmPlayerStats {
    return {
      playbackUnderruns: this.playbackUnderruns,
      droppedChunks: this.droppedChunks,
    };
  }

  /** 返回并清零自上次上报以来的播放指标，供每个对话回合独立计数。 */
  takeStats(): StreamingPcmPlayerStats {
    const stats = this.getStats();
    this.playbackUnderruns = 0;
    this.droppedChunks = 0;
    return stats;
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
    this.stop();
    this.ctx.close();
  }

  private durationMs(pcm16: Int16Array): number {
    return pcm16.length / this.sampleRate * 1000;
  }

  private resetTurnIfDrained(): void {
    if (!this.turnComplete || this.sources.size || this.pending.length) return;
    this.nextTime = 0;
    this.started = false;
    this.turnComplete = false;
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
