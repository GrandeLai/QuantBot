import assert from "node:assert/strict";
import test from "node:test";
import { researchRequest, runResearch } from "./research.ts";

test("research creation only freezes a plan; model calls require a separate explicit run", async () => {
  const requests: { url: string; body?: string }[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    requests.push({ url: String(input), body: init?.body as string | undefined });
    return new Response(JSON.stringify({ id: "study", status: "registered" }), { status: 200 });
  }) as typeof fetch;
  try {
    const plan = { job_ids: ["candidate"], card: { title: "hypothesis" }, protocol: { model_request_budget: 20 } };
    await researchRequest("", plan);
    await runResearch("study");
    await runResearch("study", true);
    assert.deepEqual(JSON.parse(requests[0].body!), plan);
    assert.deepEqual(JSON.parse(requests[1].body!), { allow_model_calls: false });
    assert.deepEqual(JSON.parse(requests[2].body!), { allow_model_calls: true });
    assert.equal(requests[1].url, "/api/quantbot/runtime/research/study/run");
  } finally { globalThis.fetch = original; }
});

test("research validation errors are shown instead of treating the response as a completed study", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: "冻结行情指纹不一致" }), { status: 400 })) as typeof fetch;
  try { await assert.rejects(runResearch("study"), /冻结行情指纹不一致/); }
  finally { globalThis.fetch = original; }
});
