import assert from "node:assert/strict";
import test from "node:test";

import { useTradingStore } from "./tradingStore.ts";

test("quickSell hydrates the shared ticket state for chart and trading page", () => {
  useTradingStore.setState({
    query: "",
    selectedSecurity: null,
    quote: null,
    draft: {
      side: "buy",
      orderType: "market",
      quantity: "100",
      submittedPrice: "",
    },
  });

  useTradingStore.getState().quickSell({
    security: {
      symbol: "AAPL.US",
      name: "Apple Inc.",
      market: "US",
      currency: "USD",
      asset_type: "stock",
      lot_size: 1,
      tradeable: true,
      shortable: true,
      restrictions: [],
    },
    quantity: 12,
  });

  const state = useTradingStore.getState();
  assert.equal(state.query, "AAPL.US");
  assert.equal(state.selectedSecurity?.symbol, "AAPL.US");
  assert.equal(state.draft.side, "sell");
  assert.equal(state.draft.quantity, "12");
});
