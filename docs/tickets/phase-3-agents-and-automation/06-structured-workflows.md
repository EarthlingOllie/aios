# 06: Structured workflows: typed I/O, templates, conditional edges

**What to build:** Workflows can pass structured data between nodes and branch on it.

- **Schemas.** Workflows and their nodes declare JSON Schemas for what they take and give. Agent nodes with an output schema return validated structured output, and runs are refused when their input doesn't fit.
- **Templates.** A node can build its input from named fields of its predecessors and the run's input.
- **Conditions.** Edges can carry JSONLogic conditions. Nodes no active path reaches are skipped, visibly.

The editor gets fields for schemas, templates and conditions (with a simple builder), and the run page shows skipped steps and structured outputs.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [Phase 1 · 13: Workflows node by node; approvals resume there; workflow worker removed](../phase-1-foundation/13-workflows-node-by-node.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md)

**Status:** done

- [x] Run input is validated against the version's input schema however the run starts, and a refused start returns the validation errors. The start-run form is generated from the schema.
- [x] An agent node's structured output is validated, with one retry that shows the model the error. A second failure fails the step with the path and message.
- [x] Run output follows the spec's rule for one, several or no final nodes, and is checked against the output schema when there is one.
- [x] Saving rejects templates that refer to nodes that aren't predecessors. A missing field at run time fails the step, naming the path.
- [x] Saving rejects conditions that use operators outside the allowed subset, exceed the size cap, or read fields missing from the declared schemas.
- [x] Diamond, all-skipped and only-final-node-skipped graphs behave as specified, and skips propagate.
- [x] A condition that errors fails the run, naming the edge.
- [x] A node output passed to a later agent node, templated or not, reaches its model wrapped as untrusted (verdict `derived`).
- [x] Resuming a paused run rebuilds its skips and condition results from recorded steps.
- [x] Workflows without schemas, templates or conditions behave exactly as today, and the existing workflow run tests pass unchanged.
