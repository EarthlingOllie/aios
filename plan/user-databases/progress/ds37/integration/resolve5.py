R = "/home/user/aios/.claude/worktrees/agent-a3737976f090b22c1/frontend/"


def block(s, start_marker, tag, replacement):
    i = s.index(start_marker)
    j = s.index("\n", s.index(">>>>>>> " + tag, i)) + 1
    return s[:i] + replacement + s[j:]


p = R + "src/components/Documents.tsx"
s = open(p).read()
s = block(s, "<<<<<<< HEAD\n  const { storeStatus, documents }", "32941df",
          "  const { storeStatus, documents } = useSharedDocuments();\n")
old_type = "type SharedDocuments = { storeStatus: DataStoreStatus | undefined; documents: DocumentsState };"
assert old_type in s
s = s.replace(old_type, """type SharedDocuments = {
  storeStatus: DataStoreStatus | undefined;
  /** Why the store's status couldn't be read, while it keeps trying. */
  storeError: string | undefined;
  documents: DocumentsState;
};""")
old_prov = """  const storeStatus = useDataStoreStatus();
  const documents = useDocuments(storeStatus);
  return <DocumentsContext value={{ storeStatus, documents }}>{children}</DocumentsContext>;"""
assert old_prov in s
s = s.replace(old_prov, """  const { status: storeStatus, error: storeError } = useDataStoreStatus();
  const documents = useDocuments(storeStatus);
  return (
    <DocumentsContext value={{ storeStatus, storeError, documents }}>{children}</DocumentsContext>
  );""")
open(p, "w").write(s)

p = R + "src/pages/Data.tsx"
s = open(p).read()
s = block(s, "<<<<<<< HEAD\n", "32941df", """  // Held above the routes, so switching tabs (or pages) doesn't ask again or drop an upload.
  const { storeStatus, storeError } = useSharedDocuments();
""")
open(p, "w").write(s)
