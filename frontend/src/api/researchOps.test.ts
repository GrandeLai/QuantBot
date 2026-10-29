import assert from "node:assert/strict";
import test from "node:test";
import { operationsRequest, queueResearch } from "./researchOps.ts";

test("queue carries an explicit timezone and never silently authorizes model calls", async () => {
  const requests: { url: string; body?: string }[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    requests.push({ url: String(input), body: init?.body as string | undefined });
    return new Response(JSON.stringify({ status: "queued" }), { status: 200 });
  }) as typeof fetch;
  try {
    await queueResearch("study", "2026-10-18T06:00:00+08:00");
    assert.deepEqual(JSON.parse(requests[0].body!), { planned_at: "2026-10-17T22:00:00.000Z", allow_model_calls: false });
    await operationsRequest();
    assert.equal(requests[1].url, "/api/quantbot/runtime/operations");
    assert.equal(requests[1].body, undefined);
    assert.throws(() => queueResearch("study", "invalid"), RangeError);
  } finally { globalThis.fetch = original; }
});
