import multiprocessing
import sys

# Rule backtests run in spawned worker processes (trader_runtime.strategy_backtest.run_in_worker):
# in the frozen desktop Runtime a worker starts this same executable, and this call turns it into
# the worker before anything reads the Runtime's startup configuration. A spawned worker of an
# unfrozen run imports this file as __mp_main__: nothing below runs there.
if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--backtest-worker" in sys.argv:
        from trader_runtime.backtest_worker import main
    else:
        from trader_runtime.sidecar_entry import main
    raise SystemExit(main())
