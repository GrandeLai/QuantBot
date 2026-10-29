import assert from "node:assert/strict";
import test from "node:test";

import {
  fetchRiskKellyBinary,
  fetchRiskKellyFromReturns,
  fetchRiskSummary,
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

test("fetchRiskSummary posts returns array to /api/risk/summary", async () => {
  const teardown = installMockFetch({
    n_samples: 3,
    vol_target: {
      realized_vol: 0.18,
      target_vol: 0.15,
      scale_factor: 0.83,
      regime: "normal",
    },
    sharpe_decay: null,
    var: { n_samples: 3, worst_loss: 0.05, method: "historical", var_95: 0.04, var_99: 0.05 },
  });
  const result = await fetchRiskSummary([0.01, -0.02, 0.005]);
  const captured = teardown();
  assert.equal(captured.url, "/api/risk/summary");
  assert.equal(captured.init?.method, "POST");
  assert.deepEqual(captured.bodyJson, { returns: [0.01, -0.02, 0.005] });
  assert.equal(result.vol_target.regime, "normal");
  assert.equal(result.sharpe_decay, null);
});

test("fetchRiskKellyBinary builds correct binary-mode body", async () => {
  const teardown = installMockFetch({
    full_kelly: 0.2,
    fractional_kelly: 0.05,
    capped_kelly: 0.05,
    mode: "binary",
    fraction: 0.25,
    cap: 0.25,
  });
  const result = await fetchRiskKellyBinary(0.6, 1.0);
  const captured = teardown();
  assert.equal(captured.url, "/api/risk/kelly");
  assert.deepEqual(captured.bodyJson, {
    mode: "binary",
    win_rate: 0.6,
    payoff_ratio: 1.0,
    fraction: 0.25,
    cap: 0.25,
  });
  assert.equal(result.full_kelly, 0.2);
  assert.equal(result.mode, "binary");
});

test("fetchRiskKellyBinary respects custom fraction and cap", async () => {
  const teardown = installMockFetch({
    full_kelly: 0.4,
    fractional_kelly: 0.4,
    capped_kelly: 0.5,
    mode: "binary",
    fraction: 1.0,
    cap: 0.5,
  });
  await fetchRiskKellyBinary(0.7, 2.0, 1.0, 0.5);
  const captured = teardown();
  assert.deepEqual(captured.bodyJson, {
    mode: "binary",
    win_rate: 0.7,
    payoff_ratio: 2.0,
    fraction: 1.0,
    cap: 0.5,
  });
});

test("fetchRiskKellyFromReturns sends mode=returns body", async () => {
  const teardown = installMockFetch({
    full_kelly: 0.0,
    fractional_kelly: 0.0,
    capped_kelly: 0.0,
    mode: "returns",
    fraction: 0.25,
    cap: 0.25,
  });
  await fetchRiskKellyFromReturns([0.01, -0.005, 0.02]);
  const captured = teardown();
  assert.equal(captured.url, "/api/risk/kelly");
  assert.deepEqual(captured.bodyJson, {
    mode: "returns",
    returns: [0.01, -0.005, 0.02],
    fraction: 0.25,
    cap: 0.25,
  });
});

test("postRisk surfaces error detail from FastAPI 400 response", async () => {
  const teardown = installMockFetch(
    { detail: "need at least 30 samples, got 3" },
    400,
  );
  await assert.rejects(
    () => fetchRiskSummary([0.01, -0.02, 0.005]),
    (err: Error) => /need at least 30 samples/.test(err.message),
  );
  teardown();
});

test("postRisk surfaces non-JSON error body as plain text", async () => {
  const teardown = installMockFetch("Internal Server Error", 500);
  await assert.rejects(
    () => fetchRiskSummary([0.01, -0.02]),
    (err: Error) => /Internal Server Error/.test(err.message),
  );
  teardown();
});
