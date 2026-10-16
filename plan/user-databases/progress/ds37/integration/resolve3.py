import re
R = "/home/user/aios/.claude/worktrees/agent-a3737976f090b22c1/frontend/"
p = R + "e2e/data-page.spec.ts"
s = open(p).read()
s = s.replace("<<<<<<< HEAD\ntest(\"uploads still in flight", "test(\"uploads still in flight")
s = re.sub(r"\n=======\n(test\(\"a list refused for good)", r"\n});\n\n\1", s)
s = re.sub(r"\n>>>>>>> 51cf805[^\n]*\n", "\n", s)
open(p, "w").write(s)

p = R + "src/components/Documents.tsx"
s = open(p).read()
old = s[s.index("<<<<<<< HEAD\n  // Poll while"):s.index(">>>>>>> 51cf805")]
end = s.index("\n", s.index(">>>>>>> 51cf805")) + 1
s = s.replace(s[s.index("<<<<<<< HEAD\n  // Poll while"):end], """  // Poll while anything is still being ingested by the worker, or the last read failed for a
  // reason that may pass.
  const ingesting = Boolean(docs?.some((d) => d.status === "pending" || d.status === "processing"));
  const retry = ready && listRetrying;
""")
open(p, "w").write(s)
