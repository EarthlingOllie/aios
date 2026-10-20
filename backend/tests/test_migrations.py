"""The migrations form one unbroken line.

Alembic only warns when two files share a revision id and keeps one of them, so two branches
that each add the next number merge cleanly and one migration silently never runs.
"""

import importlib.util
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def migrations() -> dict[str, object]:
    """Each migration module, by its file's stem."""
    modules = {}
    for path in sorted(VERSIONS.glob("*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[path.stem] = module
    return modules


def test_revision_ids_are_unique_and_named_by_their_files():
    modules = migrations()
    ids = [m.revision for m in modules.values()]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)
    assert [stem for stem, m in modules.items() if not stem.startswith(f"{m.revision}_")] == []


def test_the_migrations_run_in_one_line():
    # Followed by their links, not their numbers: 0043 runs before 0035.
    after = {}
    for m in migrations().values():
        assert m.down_revision not in after, (
            f"{after[m.down_revision]} and {m.revision} both follow {m.down_revision}"
        )
        after[m.down_revision] = m.revision
    line, revision = [], None
    while revision in after:
        revision = after[revision]
        line.append(revision)
    assert sorted(line) == sorted(after.values())
