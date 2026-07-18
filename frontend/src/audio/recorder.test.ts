import { describe, expect, it } from "vitest";

import { captureConstraints } from "./recorder";

describe("captureConstraints", () => {
  it("keeps native speech capture natural while retaining echo cancellation", () => {
    expect(captureConstraints("natural")).toEqual({
      echoCancellation: true,
      noiseSuppression: false,
      autoGainControl: false,
    });
  });

  it("enables noise processing for ASR and cascade capture", () => {
    expect(captureConstraints("noise_reduction")).toEqual({
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    });
  });
});
