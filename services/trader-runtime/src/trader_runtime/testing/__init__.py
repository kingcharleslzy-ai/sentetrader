"""Test-only doubles that the runtime can wire in when explicitly asked to.

Nothing here is reachable in a deployed configuration: each double refuses to
start unless the runtime is the local, token-authenticated, fake-data one that
the browser tests run against.
"""
