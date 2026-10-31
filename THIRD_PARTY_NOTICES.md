# Third-party notices

AI Trader includes the following third-party software:

- TradingView Lightweight Charts 5.2.1 — Apache License 2.0 —
  <https://github.com/tradingview/lightweight-charts>

The in-product market chart retains the required TradingView attribution link.


## TqSdk skills package

`services/tqkq-sidecar/skills/tqsdk-trading-and-data/` is the official TqSdk AI skills package from
[shinnytech/tqsdk-python](https://github.com/shinnytech/tqsdk-python) (`doc/ai_editor/skills/`),
Apache License 2.0, vendored unmodified except for removing `agents/`. The `tqsdk` Python package
itself is a dependency under the same license.


## MiRemoteVoice ATVV / ADPCM adaptation

The Xiaomi Remote 2 Pro TypeScript protocol and ADPCM implementation adapts MIT-licensed VincentKingHsu/MiRemoteVoice at commit `2c374d9d65ed6c8b1af6a4f9aa1b6c0f8a039aaf`. Original source attribution: Copyright (c) 2026 FanXeon@Poemcoder with Codex. No GPL driver or BlackHole is included.

MIT License

Copyright (c) 2026 Sima Qingfeng

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


## Vibe-Trading adaptations

Parts of the Runtime adapt MIT-licensed [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) at commit `41f333b6c4de9190fb57e5bb02cb570890018570`, rewritten to the Python standard library (the packaged desktop Runtime ships without numpy or pandas):

- `services/trader-runtime/src/trader_runtime/text_relevance.py` — CJK unigram/bigram tokens from `agent/src/memory/search_index.py` and BM25 (`compute_idf`, `compute_bm25_score`) from `agent/src/memory/semantic_links.py`.
- `services/trader-runtime/src/trader_runtime/memory_stats.py` (`_busy_days_entry`) — the busy-day / quiet-day comparison of `_overtrading` in `agent/src/tools/trade_journal_tool.py`.
- `services/trader-runtime/src/trader_runtime/linkage.py` — edge density and the hysteresis regime state machine of `agent/backtest/regime.py`.
- `services/trader-runtime/src/trader_runtime/strategy_validation.py` (`luck_check`) — the bootstrap confidence interval and the trade-order shuffle's drawdown distribution of `agent/backtest/validation.py`.
- `services/trader-runtime/src/trader_runtime/habit_compare.py` — the real-vs-shadow attribution of `_compute_attribution` in `agent/src/shadow_account/backtester.py`.

MIT License

Copyright (c) 2026 Vibe-Trading Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
