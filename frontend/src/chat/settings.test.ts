import { describe, expect, it } from "vitest";

import type { ChatModelInfo } from "../api/client";
import { resolveChatDefaults } from "./settings";

function model(overrides: Partial<ChatModelInfo>): ChatModelInfo {
  return {
    name: "test-model",
    label: "Test",
    mode: "cascade",
    provider: "cascade",
    voices: [],
    default: true,
    input_format: "pcm_f32le",
    supports_thinking: true,
    supports_barge_in: true,
    supports_vad_gate: true,
    preserves_paralinguistics: false,
    default_barge_in: false,
    default_vad_gate: true,
    default_capture_profile: "noise_reduction",
    vad_silence_ms_options: [],
    ...overrides,
  };
}

describe("resolveChatDefaults", () => {
  it("uses the native provider's natural capture and configured VAD preset", () => {
    expect(
      resolveChatDefaults(
        model({
          mode: "native",
          provider: "qwen-realtime",
          input_format: "pcm_s16le",
          supports_thinking: false,
          supports_vad_gate: false,
          preserves_paralinguistics: true,
          default_barge_in: true,
          default_vad_gate: false,
          default_capture_profile: "natural",
          vad_silence_ms_options: [800, 1500, 2000],
          default_vad_silence_ms: 2000,
        })
      )
    ).toEqual({
      bargeIn: true,
      vadGate: false,
      captureProfile: "natural",
      vadSilenceMs: 2000,
    });
  });

  it("disables unsupported controls and falls back to the first VAD preset", () => {
    expect(
      resolveChatDefaults(
        model({
          supports_barge_in: false,
          supports_vad_gate: false,
          default_barge_in: true,
          default_vad_gate: true,
          vad_silence_ms_options: [800, 1500],
          default_vad_silence_ms: 999,
        })
      )
    ).toEqual({
      bargeIn: false,
      vadGate: false,
      captureProfile: "noise_reduction",
      vadSilenceMs: 800,
    });
  });
});
