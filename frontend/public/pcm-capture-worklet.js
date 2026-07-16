class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const frameMs = options.processorOptions?.frameMs ?? 20;
    this.frameSamples = Math.max(128, Math.round(sampleRate * frameMs / 1000));
    this.buffer = new Float32Array(this.frameSamples);
    this.offset = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    let sourceOffset = 0;
    while (sourceOffset < input.length) {
      const count = Math.min(
        input.length - sourceOffset,
        this.frameSamples - this.offset
      );
      this.buffer.set(
        input.subarray(sourceOffset, sourceOffset + count),
        this.offset
      );
      sourceOffset += count;
      this.offset += count;
      if (this.offset === this.frameSamples) {
        const frame = this.buffer;
        this.port.postMessage(frame, [frame.buffer]);
        this.buffer = new Float32Array(this.frameSamples);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture-processor", PcmCaptureProcessor);
