p = "tests/test_provisioning.py"
s = open(p).read()


def rep(a, b):
    global s
    assert a in s, a
    s = s.replace(a, b)


rep(
    '''# A second revision of the store tree, for the migration tests.
NEXT_REVISION = """
from alembic import op
import sqlalchemy as sa

revision = "t0002"
down_revision = "0001"
''',
    '''# A revision after the store tree's head, for the migration tests.
NEXT_REVISION = """
from alembic import op
import sqlalchemy as sa

revision = "tnext"
down_revision = "{head}"
''',
)
rep(
    '''    """Adds `t0002` to the store tree when called; returns its id."""

    def add() -> str:
        (tmp_path / "t0002_marker.py").write_text(NEXT_REVISION)''',
    '''    """Adds `tnext` to the store tree, after its head, when called; returns its id."""

    def add() -> str:
        (tmp_path / "tnext_marker.py").write_text(NEXT_REVISION.replace("{head}", head_revision()))''',
)
rep('        return "t0002"', '        return "tnext"')
rep(
    '''    broken, behind, ahead = ids
    head = next_revision()''',
    '''    broken, behind, ahead = ids
    before = head_revision()
    head = next_revision()''',
)
rep(
    '''    assert [store_row(db, s).schema_revision for s in ids] == ["0001", "0001", head]''',
    '''    assert [store_row(db, s).schema_revision for s in ids] == [before, before, head]''',
)
rep(
    '''    assert [rows[s].schema_revision for s in ids] == ["0001", head, head]''',
    '''    assert [rows[s].schema_revision for s in ids] == [before, head, head]''',
)
rep('    assert f"{ok}: t0002" in out', '    assert f"{ok}: tnext" in out')
open(p, "w").write(s)
