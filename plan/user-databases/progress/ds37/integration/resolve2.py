import re
R = "/home/user/aios/.claude/worktrees/agent-a3737976f090b22c1/frontend/"
p = R + "e2e/data-store.spec.ts"
s = open(p).read()
s = s.replace("<<<<<<< HEAD\ntest(\"a busy", "test(\"a busy")
s = re.sub(r"\n=======\n(test\(\"the chat's panel shows)", r"\n});\n\n\1", s)
s = re.sub(r"\n>>>>>>> 04dfc32[^\n]*\n", "\n", s)
open(p, "w").write(s)

p = R + "src/components/Documents.tsx"
s = open(p).read()
s = s.replace("""<<<<<<< HEAD
function useDocuments(storeStatus: DataStoreStatus | undefined) {
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
=======
export function useDocuments(storeStatus: DataStoreStatus | undefined) {
""", """function useDocuments(storeStatus: DataStoreStatus | undefined) {
""")
s = s.replace("""  const [docs, setDocs] = useState<DocumentInfo[]>();
>>>>>>> 04dfc32 (Show the documents list loading instead of saying it's empty)
""", """  const [docs, setDocs] = useState<DocumentInfo[]>();
""")
s = s.replace("""<<<<<<< HEAD
        {docs.length === 0 && documents.canUpload && !documents.listError && (
=======
        {docs?.length === 0 && documents.canUpload && (
>>>>>>> 04dfc32 (Show the documents list loading instead of saying it's empty)
""", """        {docs?.length === 0 && documents.canUpload && !documents.listError && (
""")
open(p, "w").write(s)
