import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { ArrowDown, ArrowLeft, ArrowUp, ArrowUpDown, Loader2, Table2, X } from "lucide-react";

import { useDataStore } from "@/components/Documents";
import { DataStoreNotice } from "@/components/DataStoreNotice";
import { Notice } from "@/components/Notice";
import { Button } from "@/components/ui/button";
import {
  api,
  ApiError,
  describeError,
  type DataRowsPage,
  type DataRowsQuery,
  type DataTable,
} from "@/lib/api";
import { nextSort, PAGE_SIZE, rowCountLabel, rowRangeLabel, type Sort } from "@/lib/data-tables";
import { cn } from "@/lib/utils";

/**
 * The Data page's Tables: the tables in the store's `data` schema, and, with `?table=` in the
 * URL, one table's rows (sorted by `sort` and `order`, from `offset`), so a reload or a link
 * opens the same page of rows. Read-only.
 */
export function TablesSection() {
  const { status, error: storeError } = useDataStore();
  const [params] = useSearchParams();
  const ready = status === "ready";
  const tables = useTables(ready);
  const open = params.get("table");

  return (
    <>
      <DataStoreNotice
        status={status}
        error={storeError}
        action="see your tables"
        className="px-3 py-2"
      />
      {tables.error && (
        <Notice label="Tables" error className="px-3 py-2">
          Couldn't load your tables ({describeError(tables.error)}).{" "}
          <button className="underline" onClick={tables.reload}>
            Try again
          </button>
        </Notice>
      )}
      {ready &&
        (open === null ? (
          <TableList tables={tables.list} truncated={tables.truncated} />
        ) : (
          <TableRows name={open} table={tables.list?.find((t) => t.name === open)} />
        ))}
    </>
  );
}

/** The store's tables, read once the store is ready. */
function useTables(ready: boolean) {
  const [list, setList] = useState<DataTable[]>();
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<unknown>();
  const [round, setRound] = useState(0);
  useEffect(() => {
    if (!ready) return;
    let stale = false;
    setError(undefined);
    api.listDataTables().then(
      (answer) => {
        if (stale) return;
        setList(answer.tables);
        setTruncated(answer.truncated);
      },
      (err) => {
        if (!stale) setError(err);
      },
    );
    return () => {
      stale = true;
    };
  }, [ready, round]);
  const reload = useCallback(() => setRound((n) => n + 1), []);
  return { list, truncated, error, reload };
}

/** A link to `table`'s rows (or, with null, back to the list), keeping the open tab. */
function tableLink(params: URLSearchParams, table: string | null) {
  const next = new URLSearchParams({ tab: params.get("tab") ?? "tables" });
  if (table !== null) next.set("table", table);
  return { search: `?${next}` };
}

function TableList({ tables, truncated }: { tables?: DataTable[]; truncated: boolean }) {
  const [params] = useSearchParams();
  if (!tables) {
    return (
      <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading tables" />
    );
  }
  if (tables.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        No tables yet. Your agents make tables when they have data to keep, once you approve
        them.
      </p>
    );
  }
  return (
    <>
      <p className="text-muted-foreground text-sm">
        Tables your agents keep data in. Open one to see its rows.
      </p>
      <ul className="flex flex-col gap-4" aria-label="Tables">
        {tables.map((table) => (
          <li key={table.name} className="flex flex-col gap-2 rounded-md border px-4 py-3">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <h3 className="text-sm font-semibold">
                <Link
                  to={tableLink(params, table.name)}
                  className="inline-flex items-center gap-1.5 hover:underline"
                >
                  <Table2 className="text-muted-foreground size-4" />
                  {table.name}
                </Link>
              </h3>
              <span className="text-muted-foreground text-xs">{rowCountLabel(table)}</span>
            </div>
            {table.description && (
              <p className="text-muted-foreground text-sm">{table.description}</p>
            )}
            <ColumnsTable table={table} />
          </li>
        ))}
      </ul>
      {truncated && (
        <p className="text-muted-foreground text-sm">
          There are more tables than fit here; only the first are shown.
        </p>
      )}
    </>
  );
}

function ColumnsTable({ table }: { table: DataTable }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs" aria-label={`Columns of ${table.name}`}>
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 font-medium">Column</th>
            <th className="py-1 pr-3 font-medium">Type</th>
            <th className="py-1 font-medium">Description</th>
          </tr>
        </thead>
        <tbody>
          {table.columns.map((column) => (
            <tr key={column.name} className="border-t">
              <td className="py-1 pr-3 font-mono">{column.name}</td>
              <td className="text-muted-foreground py-1 pr-3 font-mono">
                {column.type}
                {!column.nullable && " not null"}
              </td>
              <td className="text-muted-foreground py-1">{column.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The open table's sort and first row, from the URL. */
function useRowsQuery() {
  const [params, setParams] = useSearchParams();
  const column = params.get("sort");
  const order = params.get("order") === "desc" ? "desc" : "asc";
  const sort: Sort = column ? { column, order } : null;
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  const update = (changes: Record<string, string | null>) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(changes)) {
          if (value === null) next.delete(key);
          else next.set(key, value);
        }
        return next;
      },
      { replace: true },
    );
  return {
    sort,
    offset,
    // A new order starts from the first row.
    setSort: (s: Sort) =>
      update({ sort: s?.column ?? null, order: s?.order === "desc" ? "desc" : null, offset: null }),
    setOffset: (n: number) => update({ offset: n > 0 ? String(n) : null }),
  };
}

/** A page of the table's rows; the last page stays up while the next loads. */
function useRows(name: string, query: DataRowsQuery) {
  const key = JSON.stringify([name, query]);
  const [answer, setAnswer] = useState<{ key: string; page?: DataRowsPage; error?: unknown }>();
  const [round, setRound] = useState(0);
  useEffect(() => {
    let stale = false;
    const [table, q] = JSON.parse(key) as [string, DataRowsQuery];
    api.getDataRows(table, q).then(
      (page) => !stale && setAnswer({ key, page }),
      (error) => !stale && setAnswer({ key, error }),
    );
    return () => {
      stale = true;
    };
  }, [key, round]);
  const current = answer?.key === key ? answer : undefined;
  return {
    page: current?.page ?? answer?.page,
    error: current?.error,
    loading: current === undefined,
    reload: () => setRound((n) => n + 1),
  };
}

type Expanded = { row: string; rowNumber: number; column: string };

function TableRows({ name, table }: { name: string; table?: DataTable }) {
  const [params] = useSearchParams();
  const { sort, offset, setSort, setOffset } = useRowsQuery();
  const rows = useRows(name, {
    offset,
    limit: PAGE_SIZE,
    ...(sort ? { sort: sort.column, order: sort.order } : {}),
  });
  const [expanded, setExpanded] = useState<Expanded>();
  const missing = rows.error instanceof ApiError && rows.error.status === 404;
  const page = missing ? undefined : rows.page;
  const columns = table?.columns ?? page?.columns.map((c) => ({ name: c, embedding: false }));

  return (
    <div className="flex flex-col gap-3">
      <div>
        <Link
          to={tableLink(params, null)}
          className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-sm"
        >
          <ArrowLeft className="size-4" />
          All tables
        </Link>
      </div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="font-mono text-base font-semibold">{name}</h2>
        {table && <span className="text-muted-foreground text-xs">{rowCountLabel(table)}</span>}
      </div>
      {table?.description && <p className="text-muted-foreground text-sm">{table.description}</p>}
      {missing && (
        <Notice label="Table" error className="px-3 py-2">
          There's no table called “{name}”.
        </Notice>
      )}
      {rows.error !== undefined && !missing && (
        <Notice label="Rows" error className="px-3 py-2">
          Couldn't load the rows ({describeError(rows.error)}).{" "}
          <button className="underline" onClick={rows.reload}>
            Try again
          </button>
        </Notice>
      )}
      {!page && !rows.error && (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading rows" />
      )}
      {page && columns && (
        <>
          {page.rows.length === 0 && offset === 0 ? (
            <p className="text-muted-foreground text-sm">This table has no rows yet.</p>
          ) : (
            <div className="overflow-x-auto rounded-md border">
              <table
                className={cn("w-full text-left text-sm", rows.loading && "opacity-60")}
                aria-label={`Rows of ${name}`}
                aria-busy={rows.loading}
              >
                <thead className="bg-muted/50">
                  <tr>
                    {columns.map((column) => (
                      <SortHeader
                        key={column.name}
                        column={column.name}
                        sortable={!column.embedding}
                        sort={sort}
                        onSort={() => {
                          setExpanded(undefined);
                          setSort(nextSort(sort, column.name));
                        }}
                      />
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {page.rows.map((row, r) => (
                    <tr key={row.key} className="border-t align-top">
                      {row.values.map((value, i) => (
                        <Cell
                          key={i}
                          value={value}
                          truncated={row.truncated.includes(i)}
                          onExpand={() =>
                            setExpanded({
                              row: row.key,
                              rowNumber: page.offset + r + 1,
                              column: page.columns[i],
                            })
                          }
                          label={`${page.columns[i]} in row ${page.offset + r + 1}`}
                        />
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground text-xs" aria-live="polite">
              {rowRangeLabel(page.offset, page.rows.length)}
            </span>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={offset === 0 || rows.loading}
                onClick={() => {
                  setExpanded(undefined);
                  setOffset(Math.max(0, offset - PAGE_SIZE));
                }}
              >
                Previous
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={!page.has_more || rows.loading}
                onClick={() => {
                  setExpanded(undefined);
                  setOffset(page.offset + page.rows.length);
                }}
              >
                Next
              </Button>
            </div>
          </div>
          {expanded && (
            <FullValue table={name} expanded={expanded} onClose={() => setExpanded(undefined)} />
          )}
        </>
      )}
    </div>
  );
}

function SortHeader({
  column,
  sortable,
  sort,
  onSort,
}: {
  column: string;
  sortable: boolean;
  sort: Sort;
  onSort: () => void;
}) {
  const order = sort?.column === column ? sort.order : undefined;
  const Icon = order === "asc" ? ArrowUp : order === "desc" ? ArrowDown : ArrowUpDown;
  return (
    <th
      className="px-3 py-2 font-mono text-xs font-medium whitespace-nowrap"
      aria-sort={order === "asc" ? "ascending" : order === "desc" ? "descending" : undefined}
    >
      {sortable ? (
        <button className="hover:text-foreground inline-flex items-center gap-1" onClick={onSort}>
          {column}
          <Icon className={cn("size-3", !order && "text-muted-foreground")} aria-hidden />
        </button>
      ) : (
        column
      )}
    </th>
  );
}

function Cell({
  value,
  truncated,
  onExpand,
  label,
}: {
  value: string | null;
  truncated: boolean;
  onExpand: () => void;
  label: string;
}) {
  return (
    <td className="max-w-xs min-w-24 px-3 py-2">
      {value === null ? (
        <span className="text-muted-foreground text-xs italic">NULL</span>
      ) : (
        <div className="max-h-32 overflow-hidden font-mono text-xs break-words whitespace-pre-wrap">
          {value}
          {truncated && "…"}
        </div>
      )}
      {truncated && (
        <button
          className="text-primary mt-1 text-xs underline-offset-2 hover:underline"
          onClick={onExpand}
          aria-label={`Show all of ${label}`}
        >
          Show all
        </button>
      )}
    </td>
  );
}

/** One value in full (as far as the server's byte cap), read when asked for. */
function FullValue({
  table,
  expanded,
  onClose,
}: {
  table: string;
  expanded: Expanded;
  onClose: () => void;
}) {
  const { row, column } = expanded;
  const [answer, setAnswer] = useState<{
    for: string;
    value?: string | null;
    truncated?: boolean;
    error?: unknown;
  }>();
  const key = JSON.stringify([table, row, column]);
  useEffect(() => {
    let stale = false;
    api.getDataValue(table, row, column).then(
      ({ value, truncated }) => !stale && setAnswer({ for: key, value, truncated }),
      (error) => !stale && setAnswer({ for: key, error }),
    );
    return () => {
      stale = true;
    };
  }, [table, row, column, key]);
  const current = answer?.for === key ? answer : undefined;
  const label = `${column} in row ${expanded.rowNumber}`;

  return (
    <section aria-label="Full value" className="flex flex-col gap-2 rounded-md border px-4 py-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">
          <span className="font-mono">{column}</span>, row {expanded.rowNumber}
        </h3>
        <button
          className="text-muted-foreground hover:text-foreground"
          onClick={onClose}
          aria-label={`Close ${label}`}
        >
          <X className="size-4" />
        </button>
      </div>
      {!current && (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading value" />
      )}
      {current?.error !== undefined && (
        <p className="text-destructive text-sm">
          Couldn't load the value ({describeError(current.error)}).
        </p>
      )}
      {current && current.error === undefined && (
        <>
          <pre className="max-h-96 overflow-auto font-mono text-xs break-words whitespace-pre-wrap">
            {current.value ?? "NULL"}
          </pre>
          {current.truncated && (
            <p className="text-muted-foreground text-xs">
              This value is too big to show whole; only its beginning is shown.
            </p>
          )}
        </>
      )}
    </section>
  );
}
