// MA crossover strategy (Rhai)
//
// 输入：fast (移动平均快线值), slow (移动平均慢线值) — Rust 端预计算
// 输出："long" | "flat" | "hold"
//
// 与 Rust src/lib.rs::run_ma_crossover_backtest 内嵌的决策逻辑等价：
//   if fast > slow { → buy if no position }
//   if fast < slow { → sell if has position }
//   else hold

fn signal(fast, slow) {
    if fast > slow {
        return "long";
    } else if fast < slow {
        return "flat";
    }
    return "hold";
}
