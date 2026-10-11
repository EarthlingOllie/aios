"""Sandboxes for agent-written Python (the `run_python` tool).

Each agent run gets one sandbox, started on its first call (or when a turn starts, to hide the
start-up time) and reused by its later calls, so files in /work carry over between them. A
backend starts, reaches and stops sandboxes (`base.SandboxBackend`); `sessions` records which
sandbox belongs to which run. Inside every sandbox, `runner` executes the code, and the egress
proxy (`egress_proxy`) is its only way out, limited to the domains in the tool's settings.
"""
