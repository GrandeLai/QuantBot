import assert from "node:assert/strict";
import test from "node:test";
import { optionsRequest, overlayPortfolio } from "./optionsOverlay.ts";

test("options holdings reject duplicate symbols, fractional shares and invalid cash", () => {
  assert.deepEqual(overlayPortfolio("0", [{ symbol: "AAPL", quantity: "250" }]), { cash: 0, positions: { AAPL: 250 } });
  for (const quantity of ["", "1.5", "0", "-2", "NaN"]) {
    assert.throws(() => overlayPortfolio("1000", [{ symbol: "AAPL", quantity }]));
  }
  assert.throws(() => overlayPortfolio("", [{ symbol: "AAPL", quantity: "100" }]));
  assert.throws(() => overlayPortfolio("100", [{ symbol: "AAPL", quantity: "100" }, { symbol: " AAPL ", quantity: "100" }]));
  assert.throws(() => overlayPortfolio("100", []));
});

test("options backtest sends explicit inputs and surfaces server rejection", async () => {
  const original = globalThis.fetch;
  let body: unknown;
  globalThis.fetch = (async (input, init) => {
    assert.equal(String(input), "/api/quantbot/options/backtest");
    body = JSON.parse(init?.body as string);
    return new Response(JSON.stringify({ detail: "历史报价缺失" }), { status: 400 });
  }) as typeof fetch;
  try {
    const request = { data: { data_kind: "synthetic" }, portfolio: overlayPortfolio("1000", [{ symbol: "AAPL", quantity: "100" }]), policy: {} };
    await assert.rejects(optionsRequest("/backtest", request), /历史报价缺失/);
    assert.deepEqual(body, request);
  } finally { globalThis.fetch = original; }
});
