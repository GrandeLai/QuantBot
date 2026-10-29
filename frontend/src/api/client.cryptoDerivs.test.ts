import assert from "node:assert/strict";
import test from "node:test";

import {
  fetchCryptoDerivsSnapshot,
  fetchETFFlowStats,
  fetchFundingStats,
} from "./client.ts";

interface CapturedCall {
  url: string;
  init: RequestInit | undefined;
  bodyJson: unknown;
}

function installMockFetch(responseJson: unknown, status = 200): () => CapturedCall {
  const captured: CapturedCall = { url: "", init: undefined, bodyJson: undefined };
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    captured.url = String(input);
    captured.init = init;
    captured.bodyJson = init?.body ? JSON.parse(String(init.body)) : undefined;
    return new Response(JSON.stringify(responseJson), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  return () => {
    globalThis.fetch = original;
    return captured;
  };
}

test("fetchCryptoDerivsSnapshot posts asset to /api/crypto-derivs/snapshot", async () => {
  const teardown = installMockFetch({
    asset: "BTC",
    timestamp: "2026-04-29T00:00:00+00:00",
    funding: { binance: null, okx: null },
    open_interest: { binance: null, okx: null },
    errors: {},
  });
  const result = await fetchCryptoDerivsSnapshot("BTC");
  const captured = teardown();
  assert.equal(captured.url, "/api/crypto-derivs/snapshot");
  assert.equal(captured.init?.method, "POST");
  assert.deepEqual(captured.bodyJson, { asset: "BTC" });
  assert.equal(result.asset, "BTC");
});

test("fetchCryptoDerivsSnapshot defaults to BTC", async () => {
  const teardown = installMockFetch({
    asset: "BTC",
    timestamp: "x",
    funding: { binance: null, okx: null },
    open_interest: { binance: null, okx: null },
    errors: {},
  });
  await fetchCryptoDerivsSnapshot();
  const captured = teardown();
  assert.deepEqual(captured.bodyJson, { asset: "BTC" });
});

test("fetchFundingStats posts history + current + threshold", async () => {
  const teardown = installMockFetch({
    stats: { mean: 0, std: 0.0001, p5: 0, p25: 0, p50: 0, p75: 0, p95: 0 },
    n_samples: 100,
    signal: { z_score: 2.5, percentile: 99, signal: "contrarian_short" },
  });
  const result = await fetchFundingStats([0.0001, 0.0002, 0.0003], 0.0005, 2.0);
  const captured = teardown();
  assert.equal(captured.url, "/api/crypto-derivs/funding-stats");
  assert.deepEqual(captured.bodyJson, {
    history: [0.0001, 0.0002, 0.0003],
    current: 0.0005,
    z_threshold: 2.0,
  });
  assert.equal(result.signal?.signal, "contrarian_short");
});

test("fetchETFFlowStats omits current as null when not provided", async () => {
  const teardown = installMockFetch({
    stats: { n_samples: 100, mean: 0, std: 1, p5: 0, p25: 0, p50: 0, p75: 0, p95: 0 },
    signal: null,
  });
  await fetchETFFlowStats([1, 2, 3, 4, 5]);
  const captured = teardown();
  assert.deepEqual(captured.bodyJson, {
    history: [1, 2, 3, 4, 5],
    current: null,
    z_threshold: 2.0,
  });
});

test("postCryptoDerivs surfaces FastAPI 400 detail", async () => {
  const teardown = installMockFetch({ detail: "need at least 30 samples, got 3" }, 400);
  await assert.rejects(
    () => fetchFundingStats([0.0001, 0.0002, 0.0003]),
    (err: Error) => /need at least 30 samples/.test(err.message),
  );
  teardown();
});
