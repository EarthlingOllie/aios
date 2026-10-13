import { useCallback, useState } from "react";
import { useSearchParams } from "react-router";
import { FileText, Search as SearchIcon } from "lucide-react";

import { ErrorBanner, Placeholder, useLoad } from "@/components/crud";
import { MarkdownView } from "@/components/Markdown";
import { MarkdownEditor } from "@/components/MarkdownEditor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, type SearchHit } from "@/lib/api";
import { stripMarkdown } from "@/lib/markdown";

// The result counts offered. GET /api/search takes any count from 1 to 50: the first here is its
// default (so it's left out of the URL), the last its maximum.
const limits = [8, 20, 50];

/**
 * Runs the hybrid (semantic + keyword) document search directly, with no agent in between. The
 * query and result count live in the URL, so a search can be linked to and Back returns to it.
 */
export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const query = params.get("q")?.trim() ?? "";
  const limit = limits.includes(Number(params.get("limit"))) ? Number(params.get("limit")) : limits[0];

  // The editor's text, reset to the URL's query when that changes (Back, or following a link).
  const [draft, setDraft] = useState(query);
  const [shownQuery, setShownQuery] = useState(query);
  if (shownQuery !== query) {
    setShownQuery(query);
    setDraft(query);
  }

  // Bumped to run the search already shown again. A new loader makes useLoad drop the old hits
  // and error, so a re-run shows as loading and a failed one doesn't leave stale results up.
  const [run, setRun] = useState(0);
  // The URL keeps the query as typed, so it round-trips to the editor; the search gets plain text.
  const { data: hits, error } = useLoad(
    useCallback(() => {
      const text = stripMarkdown(query);
      return text ? api.search(text, limit) : Promise.resolve(null);
    }, [query, limit, run]),
  );
  const loading = Boolean(query) && hits === undefined && !error;

  function search(next: { q?: string; limit?: number }) {
    const q = (next.q ?? draft).trim();
    const n = next.limit ?? limit;
    // Nothing to search once the formatting is gone (e.g. only a list marker).
    if (!stripMarkdown(q)) return;
    // Submitting the search already shown doesn't change the URL, so run it again explicitly.
    if (q === query && n === limit) setRun((r) => r + 1);
    else setParams({ q, ...(n !== limits[0] && { limit: String(n) }) });
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6 md:px-8">
        <header>
          <h1 className="text-xl font-semibold">Search</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Search your documents with the same hybrid retrieval your agents use: semantic matching
            over the vector index, combined with keyword matching over the full-text index.
          </p>
        </header>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            search({});
          }}
          className="flex flex-col gap-2"
        >
          <MarkdownEditor
            id="search-query"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                // Like the disabled button: don't start another search while one is running.
                if (!loading) search({});
              }
            }}
            placeholder="Describe what you're looking for, or use keywords, quoted phrases and -exclusions…"
            aria-label="Search query"
            className="max-h-48 resize-none"
            previewClassName="max-h-48"
            rows={3}
            autoFocus={!query}
          />
          <div className="flex items-center gap-2">
            <label className="text-muted-foreground flex items-center gap-2 text-sm">
              Results
              <select
                value={limit}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  // Search for what's in the box, so the results match it; if it's been cleared,
                  // re-run the shown query, and with neither just remember the count.
                  const q = stripMarkdown(draft) ? draft.trim() : query;
                  if (q) search({ q, limit: n });
                  else setParams({ limit: String(n) });
                }}
                className="border-input dark:bg-input/30 h-8 rounded-md border bg-transparent px-2 text-sm"
              >
                {limits.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
            <span className="text-muted-foreground ml-auto text-xs max-sm:hidden">
              Enter to search, Shift+Enter for a new line
            </span>
            <Button type="submit" disabled={!stripMarkdown(draft) || loading} className="max-sm:ml-auto">
              <SearchIcon />
              {loading ? "Searching…" : "Search"}
            </Button>
          </div>
        </form>
        <ErrorBanner message={error && `Search failed: ${error}`} />
        <Results query={query} hits={hits} />
      </div>
    </div>
  );
}

function Results({ query, hits }: { query: string; hits: SearchHit[] | null | undefined }) {
  if (!query || !hits) return null;
  if (hits.length === 0) {
    return (
      <Placeholder>
        No passages matched. Try different wording, or upload documents from the Chat page.
      </Placeholder>
    );
  }
  return (
    <section className="flex flex-col gap-3" aria-label="Results">
      <p className="text-muted-foreground text-sm">
        {hits.length} {hits.length === 1 ? "passage" : "passages"}, best match first
      </p>
      <ol className="flex flex-col gap-3">
        {hits.map((hit) => (
          <li key={hit.chunk_id}>
            <Hit hit={hit} />
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * "p. 3", "pp. 3–5" or "pp. 3–4, 17": consecutive pages collapse into a range, gaps stay visible.
 * Ingest stores a chunk's pages sorted and deduplicated.
 */
function formatPages(pages: number[]) {
  const runs: string[] = [];
  for (let i = 0; i < pages.length; i++) {
    const start = pages[i];
    while (pages[i + 1] === pages[i] + 1) i++;
    runs.push(start === pages[i] ? `${start}` : `${start}–${pages[i]}`);
  }
  return `${pages.length === 1 ? "p." : "pp."} ${runs.join(", ")}`;
}

function Hit({ hit }: { hit: SearchHit }) {
  const headings = hit.meta.headings ?? [];
  const pages = hit.meta.pages ?? [];
  return (
    <article className="rounded-md border">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2">
        {/* min-w-40: the badges wrap below the filename rather than squeezing it out on phones. */}
        <div className="flex min-w-40 flex-1 items-center gap-2">
          <FileText className="text-muted-foreground size-4 shrink-0" />
          <span className="truncate text-sm font-medium">{hit.filename}</span>
          {pages.length > 0 && (
            <span className="text-muted-foreground shrink-0 text-xs">
              {formatPages(pages)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {/* Which retrieval branches found the passage, and how each ranked it. */}
          {hit.semantic_rank !== null && (
            <Badge variant="secondary" title="Rank among the semantic (vector) matches">
              Semantic #{hit.semantic_rank}
            </Badge>
          )}
          {hit.lexical_rank !== null && (
            <Badge variant="secondary" title="Rank among the keyword (full-text) matches">
              Keyword #{hit.lexical_rank}
            </Badge>
          )}
          <Badge variant="outline" title="Reciprocal Rank Fusion score">
            {hit.score.toFixed(4)}
          </Badge>
        </div>
      </header>
      <div className="px-4 py-3">
        {headings.length > 0 && (
          <p className="text-muted-foreground mb-2 truncate text-xs">{headings.join(" › ")}</p>
        )}
        <MarkdownView className="text-sm">{hit.text}</MarkdownView>
      </div>
    </article>
  );
}
