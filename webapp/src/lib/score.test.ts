import { describe, expect, it } from "vitest";
import { band } from "./score";

describe("band · 档位边界", () => {
  it("≥4 绿 / ≥3 琥珀 / <3 锈", () => {
    expect(band(5)).toBe("g");
    expect(band(4)).toBe("g");
    expect(band(3.99)).toBe("m");
    expect(band(3)).toBe("m");
    expect(band(2.99)).toBe("l");
    expect(band(1)).toBe("l");
  });
});
