import re
R = "/home/user/aios/.claude/worktrees/agent-a3737976f090b22c1/frontend/"


def block(s, start_marker, tag, replacement):
    i = s.index(start_marker)
    j = s.index("\n", s.index(">>>>>>> " + tag, i)) + 1
    return s[:i] + replacement + s[j:]


# Specs: keep both sides, HEAD's tests first.
p = R + "e2e/data-page.spec.ts"
s = open(p).read()
s = s.replace("<<<<<<< HEAD\ntest(\"uploads still in flight", "test(\"uploads still in flight")
s = re.sub(r"\n=======\n(test\(\"a slow poll that answers)", r"\n});\n\n\1", s)
s = re.sub(r"\n>>>>>>> f3cb6c6[^\n]*\n", "\n", s)
open(p, "w").write(s)

# Fake API: keep upload gate and list hold helpers; delay, then snapshot, then hold.
p = R + "e2e/fake-api.ts"
s = open(p).read()
s = block(s, "<<<<<<< HEAD\n  /** Uploads wait", "f3cb6c6", """  /** Uploads wait, as if still on the wire, until `releaseUploads`. */
  holdUploads() {
    let release = () => {};
    const wait = new Promise<void>((resolve) => (release = resolve));
    this.uploadGate = { wait, release };
  }

  releaseUploads() {
    this.uploadGate?.release();
    this.uploadGate = undefined;
  }

  /**
   * Until called again with false, each GET /api/documents reads the list when it arrives but
   * waits to answer until the test calls its entry in `heldDocumentLists`: a slow response.
   */
  holdDocumentLists(hold = true) {
    this.holdingLists = hold;
""")
s = block(s, "<<<<<<< HEAD\n        await new Promise((resolve) => setTimeout(resolve, this.documentsDelay));", "f3cb6c6", """        await new Promise((resolve) => setTimeout(resolve, this.documentsDelay));
        const list = [...this.documents];
        if (this.holdingLists) {
          await new Promise<void>((release) => this.heldDocumentLists.push(release));
        }
        return reply(200, list);
""")
open(p, "w").write(s)

# Documents: sequence-numbered refresh with finding 3's transient classification; usePoll.
p = R + "src/components/Documents.tsx"
s = open(p).read()
s = block(s, "<<<<<<< HEAD\n  // Never rejects", "f3cb6c6", """  // Never rejects: a failed read keeps the last list, and the poll below tries again if the
  // failure is transient. Only the latest read may update state: a slower, older answer (a poll
  // sent before a delete, say) is dropped.
  const latest = useRef(0);
  const refresh = useCallback(() => {
    const request = ++latest.current;
    return api.listDocuments().then(
      (list) => {
        if (request !== latest.current) return;
        setDocs(list);
        setListError(undefined);
        setListRetrying(false);
      },
      (err) => {
        if (request !== latest.current) return;
        // A network error, or the API restarting (502, 504) or busy (503: every connection the
        // store allows is in use), which a moment's wait frees.
        const transient = !(err instanceof ApiError) || [502, 503, 504].includes(err.status);
        setListRetrying(transient);
        setListError(
          err instanceof ApiError && err.status === 503
            ? "Your data store is busy. Trying again…"
            : `Couldn't load your documents (${describeError(err)}).${transient ? " Trying again…" : ""}`,
        );
      },
    );
  }, []);
""")
s = block(s, "<<<<<<< HEAD\n  // Poll while", "f3cb6c6", """  // Poll while anything is still being ingested by the worker, or the last read failed for a
  // reason that may pass; each read waits for the last to answer, so a slow store doesn't pile
  // them up.
  const ingesting = Boolean(docs?.some((d) => d.status === "pending" || d.status === "processing"));
  const retry = ready && listRetrying;
  usePoll(ingesting || retry, refresh, 2000);
""")
open(p, "w").write(s)
