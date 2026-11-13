"""A removed connector's message items kept as Markdown documents (`Provider.keep_groups`): one
document per channel per month, its messages in time order, each reply under its thread's first
message. Shared by the providers whose items are channels' messages (Slack, the test provider)."""

from collections import defaultdict
from collections.abc import Callable, Iterable

from aios.datastores.models import SyncedItem
from aios.source_providers import KeptDocument


def _when(row: SyncedItem):
    return row.source_updated_at or row.created_at


def by_channel_and_month(
    rows: Iterable[SyncedItem], channel_of: Callable[[SyncedItem], str]
) -> list[KeptDocument]:
    rows = list(rows)
    replies: dict[int, list[SyncedItem]] = defaultdict(list)
    for r in rows:
        if r.parent_id is not None:
            replies[r.parent_id].append(r)
    ids = {r.id for r in rows}
    groups: dict[tuple[str, str], list[SyncedItem]] = defaultdict(list)
    for r in rows:
        if r.parent_id is None or r.parent_id not in ids:
            groups[(channel_of(r), _when(r).strftime("%Y-%m"))].append(r)
    kept = []
    for (channel, month), tops in sorted(groups.items()):
        lines = [f"# {channel}, {month}", ""]
        for top in sorted(tops, key=_when):
            lines += _message_lines(top, "")
            for reply in sorted(replies.get(top.id, []), key=_when):
                lines += _message_lines(reply, "> ")
        kept.append(KeptDocument(f"{channel} {month}.md", "\n".join(lines).rstrip() + "\n"))
    return kept


def _message_lines(row: SyncedItem, quote: str) -> list[str]:
    who = row.author_name or "Someone"
    head = f"**{who}**, {_when(row):%Y-%m-%d %H:%M} UTC"
    if row.url:
        head += f" ([link]({row.url}))"
    body = [quote + line if line else quote.rstrip() for line in (row.text or "").splitlines()]
    return [quote + head, quote.rstrip(), *body, ""]
