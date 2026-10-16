# Data stores: build progress (paused)

Working notes and evidence from building the "Data stores N" issues (#21–#38) as a stack of
branches. Each ticket was built test-first, checked with Playwright, code-reviewed, and every
review finding went to its own agent, which investigated and fixed it test-first or argued why
not. Each `ledger.md` records the verdict for every finding and why it was accepted.

## Branches

| Ticket | Branch | State |
|---|---|---|
| #21 data-store seam | `claude/data-stores-21-seam` | Done, review fixes in |
| #22 least-privilege roles | `claude/data-stores-22-roles` | Done, review fixes in |
| #23 provisioning | `claude/data-stores-23-provision` | Done, review fixes in. Starts from a merge of #21 and #22 |
| #24 documents in the store | `claude/data-stores-24-documents` | Done, review fixes in |
| #26 describe/query tools | `claude/data-stores-26-query` | Done, review fixes in |
| #24 + #26 merged | `claude/data-stores-24-26` | Merge done; review findings M3, M5–M9 not yet investigated (see `ds2426/ledger.md`) |
| #37 Data page | `claude/data-stores-37-data-page` | Done, review fixes in |
| #34 run history export | `claude/data-stores-34-history-export` | Build only |
| #34 review fixes 2, 3, 4, 8, 10 | `claude/data-stores-34-fixes-untested` | Merged onto the #34 build; full suite not yet run |
| #34 review fix 1 | `claude/data-stores-34-f1-unmerged` | Committed by its agent; not yet assessed or merged |
| #34 review fix 5 | `claude/data-stores-34-f5-unmerged` | Committed by its agent; not yet assessed or merged |
| #25 S3 uploads | `claude/data-stores-25-s3` | Unfinished work in progress |
| #27 write_data | `claude/data-stores-27-write-data` | Unfinished work in progress, on the #24 + #26 merge |

## To resume

1. #34: assess fixes 1 and 5, merge them onto `claude/data-stores-34-fixes-untested`, run the
   full suite (with `DATABASE_ADMIN_URL` set), then move the result onto the #34 branch.
2. Investigate the merge's open review findings (`ds2426/ledger.md`).
3. Finish #25 and #27, then review each and handle its findings the same way.
4. Next on the frontier: #38 (needs #26 and #37) and #28 (needs #27 and #37).
5. Open the stacked PRs.

## Layout

- `ds23/`, `ds24/`, `ds26/`, `ds34/`, `ds37/`, `ds2426/`: each ticket's `ledger.md`, plus one
  folder per review finding (`fN/`) with red/green test output, full-suite runs and screenshots.
- `ds25/`, `ds27/`: notes from the unfinished builds.
- `builder-brief.md`, `*-finding-brief.md`: the instructions given to the agents, including how
  to set up a local Postgres 16 + pgvector, Redis and Playwright environment.
- `drive/` and `*/drive/`: the scripts that drove the real app with Playwright, and their screenshots.

Left out: downloaded tooling (the Terraform binary and providers, a uv image) and Terraform
working directories.
