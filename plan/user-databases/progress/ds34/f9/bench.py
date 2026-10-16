import timeit

from alembic.script import ScriptDirectory

from aios.datastores import migrations

migrations._line(True)  # warm imports
n = 200
t = timeit.timeit(lambda: migrations._line(True), number=n) / n
print(f"_line: {t * 1e3:.3f} ms/call")
t2 = (
    timeit.timeit(
        lambda: ScriptDirectory.from_config(migrations.config()).get_revision("0002"), number=n
    )
    / n
)
print(f"ScriptDirectory build+lookup (as command.upgrade does): {t2 * 1e3:.3f} ms")
