/** The list with its item at `from` moved to `to` (the default chat agents' reordering); the
 * list as it was for a move out of range. */
export function moved<T>(list: T[], from: number, to: number): T[] {
  if (from < 0 || to < 0 || from >= list.length || to >= list.length) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}
