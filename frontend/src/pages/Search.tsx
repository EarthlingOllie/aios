import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Bot, FileText, Inbox, MessageSquare, NotebookPen, Search as SearchIcon } from "lucide-react";

import { ErrorBanner, Placeholder, useLoad } from "@/components/crud";
import { DataStoreNotice } from "@/components/DataStoreNotice";
import { useDataStore } from "@/components/Documents";
import { MarkdownView } from "@/components/Markdown";
import { MarkdownEditor } from "@/components/MarkdownEditor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, type SearchHit, type SearchKind } from "@/lib/api";
import { stripMarkdown } from "@/lib/markdown";

// The result counts offered. GET /api/search takes any count from 1 to 50: the first here is its
// default (so it's left out of the URL), the last its maximum.
const limits = [8, 20, 50];

/** What each kind of item is called, as a result's label and as its filter. */
const kinds: { key: SearchKind; label: string; filter: string }[] = [
  { key: "document_chunk", label: "Document", filter: "Documents" },
  { key: "chat_message", label: "Chat message", filter: "Chat messages" },
  { key: "run_message", label: "Run", filter: "Runs" },
  { key: "brief", label: "Brief", filter: "Briefs" },
  { key: "source_item", label: "Synced item", filter: "Synced items" },
];
const kindLabel = Object.fromEntries(kinds.map((k) => [k.key, k.label])) as Record<SearchKind, string>;

/**
 * Runs the organisation's unified hybrid (semantic + keyword) search directly, with no agent in
 * between: documents, chat messages, past runs and briefs, from the whole organisation. The
 * query, the kind it's narrowed to, the result count and whether the ranking shows live in the
 * URL, so a search can be linked to and Back returns to it.
 */
export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const query = params.get("q")?.trim() ?? "";
  const limit = limits.includes(Number(params.get("limit"))) ? Number(params.get("limit")) : limits[0];
  const kind = kinds.find((k) => k.key === params.get("kind"))?.key;
  const showRanking = params.get("ranking") === "1";

  // The editor's text, reset to the URL's query when that changes (Back, or following a link).
  const [draft, setDraft] = useState(query);
  const [shownQuery, setShownQuery] = useState(query);
  if (shownQuery !== query) {
    setShownQuery(query);
    setDraft(query);
  }

  // The API refuses to search a data store that isn't ready, so wait for it (and say why) rather
  // than show that refusal as a failed search.
  const { status: storeStatus } = useDataStore();
  const storeReady = storeStatus === "ready";

  // Bumped to run the search already shown again. A new loader makes useLoad drop the old hits
  // and error, so a re-run shows as loading and a failed one doesn't leave stale results up.
  const [run, setRun] = useState(0);
  // The URL keeps the query as typed, so it round-trips to the editor; the search gets plain text.
  const { data: hits, error } = useLoad(
    useCallback(() => {
      const text = stripMarkdown(query);
      return text && storeReady ? api.search(text, limit, kind ? [kind] : []) : Promise.resolve(null);
    }, [query, limit, kind, run, storeReady]),
  );
  const loading = Boolean(query) && storeReady && hits === undefined && !error;

  /** The URL's parameters for this search: those left at their defaults are left out. */
  function url(q: string, n: number, k: SearchKind | undefined, ranking: boolean) {
    return {
      ...(q && { q }),
      ...(n !== limits[0] && { limit: String(n) }),
      ...(k && { kind: k }),
      ...(ranking && { ranking: "1" }),
    };
  }

  function search(next: { q?: string; limit?: number; kind?: SearchKind | null }) {
    const q = (next.q ?? draft).trim();
    const n = next.limit ?? limit;
    const k = next.kind === undefined ? kind : (next.kind ?? undefined);
    // Nothing to search once the formatting is gone (e.g. only a list marker).
    if (!stripMarkdown(q)) {
      setParams(url(query, n, k, showRanking));
      return;
    }
    // Submitting the search already shown doesn't change the URL, so run it again explicitly.
    if (q === query && n === limit && k === kind) setRun((r) => r + 1);
    else setParams(url(q, n, k, showRanking));
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6 md:px-8">
        <header>
          <h1 className="text-xl font-semibold">Search</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Search everything in the organisation (documents, chat messages, past runs and the
            briefs agents were given) with the same hybrid retrieval your agents use: semantic
            matching over the vector index, combined with keyword matching over the full-text
            index.
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
                // Like the disabled button: not mid-search, nor before the store is ready.
                if (!loading && storeReady) search({});
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
                  else setParams(url("", n, kind, showRanking));
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
            <Button type="submit" disabled={!stripMarkdown(draft) || loading || !storeReady} className="max-sm:ml-auto">
              <SearchIcon />
              {loading ? "Searching…" : "Search"}
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div role="group" aria-label="Kinds" className="flex flex-wrap gap-1">
              {[{ key: undefined, filter: "All" }, ...kinds].map((option) => (
                <Button
                  key={option.key ?? ""}
                  type="button"
                  size="sm"
                  variant={kind === option.key ? "secondary" : "ghost"}
                  aria-pressed={kind === option.key}
                  onClick={() => {
                    const q = stripMarkdown(draft) ? draft.trim() : query;
                    search({ q, kind: option.key ?? null });
                  }}
                  className="h-7 px-2 text-xs"
                >
                  {option.filter}
                </Button>
              ))}
            </div>
            <label className="text-muted-foreground ml-auto flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={showRanking}
                onChange={(e) => setParams(url(query, limit, kind, e.target.checked), { replace: true })}
              />
              Show ranking
            </label>
          </div>
        </form>
        <DataStoreNotice status={storeStatus} action="search" className="px-3 py-2" />
        <ErrorBanner message={error && `Search failed: ${error}`} />
        <Results query={query} hits={hits} showRanking={showRanking} />
      </div>
    </div>
  );
}

function Results({
  query,
  hits,
  showRanking,
}: {
  query: string;
  hits: SearchHit[] | null | undefined;
  showRanking: boolean;
}) {
  if (!query || !hits) return null;
  if (hits.length === 0) {
    return (
      <Placeholder>
        <span>
          Nothing matched. Try different wording, or upload documents on the{" "}
          <Link to="/data/documents" className="underline underline-offset-2">
            Data page
          </Link>
          .
        </span>
      </Placeholder>
    );
  }
  return (
    <section className="flex flex-col gap-3" aria-label="Results">
      <p className="text-muted-foreground text-sm">
        {hits.length} {hits.length === 1 ? "result" : "results"}, best match first
      </p>
      <ol className="flex flex-col gap-3">
        {hits.map((hit) => (
          <li key={hit.id}>
            <Hit hit={hit} showRanking={showRanking} />
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

/** Where an item is from, and the page that shows it. */
function source(hit: SearchHit): { to: string; label: string; icon: typeof FileText } {
  switch (hit.kind) {
    case "document_chunk":
      return { to: "/data/documents", label: hit.filename ?? "A document", icon: FileText };
    case "chat_message":
      return { to: `/chats/${hit.chat_id}`, label: `#${hit.chat_name ?? hit.chat_id}`, icon: MessageSquare };
    case "run_message": {
      const title = hit.run_title ? ` · ${hit.run_title}` : "";
      return {
        to: `/agents/${hit.agent_id}/runs/${hit.agent_run_id}`,
        label: `${hit.agent_name ?? "An agent"}${title}`,
        icon: Bot,
      };
    }
    case "brief":
      return {
        to: hit.chat_id !== null ? `/chats/${hit.chat_id}` : `/agents/${hit.agent_id}`,
        label: `${hit.agent_name ?? "An agent"}${hit.chat_name ? ` in #${hit.chat_name}` : ""}`,
        icon: NotebookPen,
      };
    case "source_item":
      return {
        to: "/data/sources",
        label: `${hit.item_title ?? "An item"}${hit.source_name ? ` · ${hit.source_name}` : ""}`,
        icon: Inbox,
      };
  }
}

function Hit({ hit, showRanking }: { hit: SearchHit; showRanking: boolean }) {
  const headings = hit.ref.headings ?? [];
  const pages = hit.ref.pages ?? [];
  const from = source(hit);
  const Icon = from.icon;
  return (
    <article className="rounded-md border" aria-label={kindLabel[hit.kind]}>
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2">
        {/* min-w-40: the badges wrap below the source rather than squeezing it out on phones. */}
        <div className="flex min-w-40 flex-1 items-center gap-2">
          <Badge variant="outline" className="shrink-0">
            {kindLabel[hit.kind]}
          </Badge>
          <Icon className="text-muted-foreground size-4 shrink-0" />
          <Link to={from.to} className="truncate text-sm font-medium underline-offset-2 hover:underline">
            {from.label}
          </Link>
          {pages.length > 0 && (
            <span className="text-muted-foreground shrink-0 text-xs">{formatPages(pages)}</span>
          )}
          {hit.ref.role && (
            <span className="text-muted-foreground shrink-0 text-xs">
              {hit.ref.role === "user" ? "Asked" : "Replied"}
            </span>
          )}
        </div>
        {showRanking && (
          <div className="flex items-center gap-1" aria-label="Ranking">
            {/* Which retrieval branches found the item, and how each ranked it. */}
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
        )}
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
