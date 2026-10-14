"""Workflows: graphs of agents, and of other workflows run as subgraphs.

A workflow version is a directed acyclic graph. Each node pins an agent version or a workflow
version; an edge says control passes from one node to the next when it finishes. Nodes with no
incoming edge are where the workflow starts, and it ends when every node has run.

Only the definition exists so far: workflows can be built, versioned and nested, not run yet.
Running one will compile it into a LangGraph `StateGraph`, with an agent node as a `build_agent`
graph (`aios.runner`) and a workflow node as its own compiled graph, added as a subgraph.
"""

import re
from collections.abc import Sequence

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class GraphError(ValueError):
    """A workflow graph that can't be saved; the message is shown to the user."""


def validate_graph(keys: Sequence[str], edges: Sequence[tuple[str, str]]) -> None:
    """Checks node keys are valid and unique, and edges join existing nodes without a cycle."""
    for key in keys:
        if not KEY_PATTERN.match(key):
            raise GraphError(f"Invalid node key {key!r}: use 1-64 letters, digits, '-' or '_'.")
    if len(set(keys)) != len(keys):
        raise GraphError("Each node key can be used only once")
    known = set(keys)
    if len(set(edges)) != len(edges):
        raise GraphError("Each edge can be added only once")
    successors: dict[str, list[str]] = {key: [] for key in keys}
    for source, target in edges:
        for end in (source, target):
            if end not in known:
                raise GraphError(f"An edge refers to an unknown node: {end!r}")
        if source == target:
            raise GraphError(f"Node {source!r} can't lead to itself")
        successors[source].append(target)
    # Kahn's algorithm: whatever never runs out of incoming edges is on a cycle.
    incoming = {key: 0 for key in keys}
    for _, target in edges:
        incoming[target] += 1
    ready = [key for key in keys if incoming[key] == 0]
    seen = 0
    while ready:
        key = ready.pop()
        seen += 1
        for target in successors[key]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if seen != len(keys):
        cycle = sorted(key for key, n in incoming.items() if n > 0)
        raise GraphError(f"The graph has a cycle through: {', '.join(cycle)}")
