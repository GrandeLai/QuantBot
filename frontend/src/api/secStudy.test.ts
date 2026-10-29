import assert from "node:assert/strict";
import test from "node:test";
import { parseEventHorizons, secStudyRequest } from "./secStudy.ts";

test("event horizons keep declared order and reject ambiguous or duplicate windows", () => {
  assert.deepEqual(parseEventHorizons(" 1, 5, 20 "), [1, 5, 20]);
  for (const input of ["", "1,,5", "5,1", "1,1", "0,5", "1.5", "1,61", "1,2,3,4,5,6"]) {
    assert.throws(() => parseEventHorizons(input));
  }
});

test("event study preserves explicit availability assumptions and does not add model authorization", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    assert.equal(String(input), "/api/quantbot/sec-study");
    assert.deepEqual(JSON.parse(init?.body as string), { policy: { availability_mode: "observed" } });
    return new Response(JSON.stringify({ execution_completed: true }), { status: 200 });
  }) as typeof fetch;
  try {
    assert.deepEqual(await secStudyRequest("", { policy: { availability_mode: "observed" } }), { execution_completed: true });
  } finally { globalThis.fetch = original; }
});
