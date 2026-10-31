#!/bin/sh
# Sidecar image entrypoint (tqkq-sidecar, tqkq-pool and its children, paper-engine).
#
# AI_TRADER_JEMALLOC=1 runs the process — and every child it starts, which inherit the environment —
# on jemalloc instead of glibc malloc (2026-10-13 内存调查, ai-trader-release-artifacts/memory-20261012):
# TqSdk sessions grow the heap in many threads and glibc keeps what they free, so a process that
# once held a few chart downloads stays at that size. With jemalloc preloaded, Python's own small-
# object allocator off (PYTHONMALLOC=malloc, so the frees reach jemalloc) and jemalloc's background
# purge thread on, the same open/close cycles return to the baseline; without background_thread
# nothing is returned. Any other value (the default 0) leaves the process exactly as before, so a
# service is switched back by setting its compose switch to 0 and recreating it.
set -eu
if [ "${AI_TRADER_JEMALLOC:-0}" = 1 ]; then
  if [ -r /usr/local/lib/libjemalloc.so.2 ]; then
    export LD_PRELOAD="/usr/local/lib/libjemalloc.so.2${LD_PRELOAD:+:$LD_PRELOAD}"
    export PYTHONMALLOC=malloc
    export MALLOC_CONF="${AI_TRADER_JEMALLOC_CONF:-background_thread:true,narenas:2}"
  else
    echo "ai-trader: AI_TRADER_JEMALLOC=1 but libjemalloc.so.2 is missing; running on glibc malloc" >&2
  fi
fi
exec "$@"
