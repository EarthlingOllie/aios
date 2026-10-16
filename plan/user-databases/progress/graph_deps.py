"""From `terraform graph` (edges "A" -> "B" meaning A depends on B), everything deploy.sh's
targeted apply pulls in: the targets and all they depend on. Fails if the app's secret version is
among them, since applying it is what switches the app's DATABASE_URL."""

import re
import sys
from collections import defaultdict

edges = defaultdict(set)
for a, b in re.findall(r'"([^"]+)"\s*->\s*"([^"]+)"', open(sys.argv[1]).read()):
    edges[a].add(b)

def norm(n):
    return re.sub(r"^\[root\] | \(expand\)$", "", n)

graph = defaultdict(set)
for a, bs in edges.items():
    graph[norm(a)] |= {norm(b) for b in bs}

targets = sys.argv[2:]
seen, todo = set(), list(targets)
while todo:
    n = todo.pop()
    if n in seen:
        continue
    seen.add(n)
    todo.extend(graph.get(n, ()))
resources = sorted(n for n in seen if re.match(r"^(aws_|random_|data\.)", n))
print("pulled in by the targeted apply:")
print("\n".join("  " + r for r in resources))
bad = [r for r in resources if r.startswith("aws_secretsmanager_secret_version.app")]
print("FAIL: app secret version is pulled in" if bad else "PASS: app secret version is not pulled in")
sys.exit(1 if bad else 0)
