import type { CaptureProfile, ChatModelInfo } from "../api/client";

export interface ChatRuntimeDefaults {
  bargeIn: boolean;
  vadGate: boolean;
  captureProfile: CaptureProfile;
  vadSilenceMs?: number;
}

export function resolveChatDefaults(
  model: ChatModelInfo
): ChatRuntimeDefaults {
  const configuredSilence = model.default_vad_silence_ms ?? undefined;
  const vadSilenceMs =
    configuredSilence &&
    model.vad_silence_ms_options.includes(configuredSilence)
      ? configuredSilence
      : model.vad_silence_ms_options[0];

  return {
    bargeIn: model.supports_barge_in ? model.default_barge_in : false,
    vadGate: model.supports_vad_gate ? model.default_vad_gate : false,
    captureProfile: model.default_capture_profile,
    vadSilenceMs,
  };
}
