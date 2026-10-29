// SMA Crossover strategy — Rhai 版本.
//
// 与 ma_crossover.rhai 不同，这个策略**在 Rhai 中自己调用 sma()**，
// 而不是接收 Rust 预先算好的 fast_ma/slow_ma。
//
// 函数签名：
//   fn signal_from_closes(closes, fast_period, slow_period) -> string
//
// 这展示了 Rhai 策略可以完全控制指标计算（C.2 里程碑）。
// fn signal(fast, slow) 保留兼容性（供 ma_crossover_backtest 调用）。

/// 从 closes 数组计算 SMA 交叉信号.
///
/// 返回 "long" / "flat" / "hold"：
///   - long: 快线在慢线之上（买入信号）
///   - flat: 快线在慢线之下（卖出信号）
///   - hold: 两线相等（维持现状）
fn signal_from_closes(closes, fast_period, slow_period) {
    let fast_arr = sma(closes, fast_period);
    let slow_arr = sma(closes, slow_period);

    let last = closes.len() - 1;
    let fast = fast_arr[last];
    let slow = slow_arr[last];

    // NaN 发生在 warmup 期 — 不够数据时 hold
    if fast != fast || slow != slow {
        return "hold";
    }

    if fast > slow {
        return "long";
    } else if fast < slow {
        return "flat";
    }
    return "hold";
}

/// 兼容 ma_crossover.rhai 的接口，接收预算好的 fast/slow MA.
fn signal(fast, slow) {
    if fast > slow { return "long"; }
    if fast < slow { return "flat"; }
    return "hold";
}
