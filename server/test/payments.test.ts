import { describe, it, expect } from "vitest";
import {
  formatUsdFromMicrounits,
  toUsdcMicrounits,
} from "../src/payments.js";

describe("formatUsdFromMicrounits", () => {
  it("formats $0.10 with two decimal places", () => {
    expect(formatUsdFromMicrounits(100_000n)).toBe("0.10");
  });

  it("formats $0.15 preserving both digits", () => {
    expect(formatUsdFromMicrounits(150_000n)).toBe("0.15");
  });

  it("formats whole dollars with .00", () => {
    expect(formatUsdFromMicrounits(1_000_000n)).toBe("1.00");
  });

  it("formats $1.20 with trailing zero", () => {
    expect(formatUsdFromMicrounits(1_200_000n)).toBe("1.20");
  });

  it("preserves full precision for sub-cent amounts", () => {
    expect(formatUsdFromMicrounits(1n)).toBe("0.000001");
  });

  it("preserves precision beyond 2 decimals", () => {
    expect(formatUsdFromMicrounits(123_456n)).toBe("0.123456");
  });

  it("handles large amounts", () => {
    expect(formatUsdFromMicrounits(10_500_000n)).toBe("10.50");
  });
});

describe("toUsdcMicrounits", () => {
  it("converts 0.10 to 100000", () => {
    expect(toUsdcMicrounits("0.10")).toBe(100_000n);
  });

  it("converts 0.15 to 150000", () => {
    expect(toUsdcMicrounits("0.15")).toBe(150_000n);
  });

  it("converts whole dollar", () => {
    expect(toUsdcMicrounits("1")).toBe(1_000_000n);
  });

  it("converts max precision", () => {
    expect(toUsdcMicrounits("0.000001")).toBe(1n);
  });

  it("rejects negative values", () => {
    expect(() => toUsdcMicrounits("-1")).toThrow();
  });

  it("rejects too many decimals", () => {
    expect(() => toUsdcMicrounits("0.0000001")).toThrow();
  });

  it("round-trips with formatUsdFromMicrounits", () => {
    const prices = ["0.10", "0.15", "1.00", "0.000001"];
    for (const price of prices) {
      const micro = toUsdcMicrounits(price);
      const display = formatUsdFromMicrounits(micro);
      expect(toUsdcMicrounits(display)).toBe(micro);
    }
  });
});
