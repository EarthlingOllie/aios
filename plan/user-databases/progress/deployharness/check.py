"""Checks the order of a deploy.sh run's calls (the harness log): the db-roles task must run
before any untargeted `terraform apply`, which would switch the app's secret to aios_app, on a
stack with running services; and on every stack, before the migration."""

import sys

stack, log = sys.argv[1], [line.rstrip("\n") for line in open(sys.argv[2])]


def first(pred):
    return next((i for i, line in enumerate(log) if pred(line)), None)


db_roles = first(lambda l: l.startswith("aws ecs run-task") and "aios-db-roles" in l)
migrate = first(lambda l: l.startswith("aws ecs run-task") and "aios-migrate" in l)
full_apply = first(
    lambda l: l.startswith("terraform") and " apply " in f" {l} " and "-target=" not in l
)
ok = True
for name, cond in [
    ("db-roles task runs", db_roles is not None),
    ("db-roles runs before the migration", None not in (db_roles, migrate) and db_roles < migrate),
]:
    print(("PASS " if cond else "FAIL ") + name)
    ok &= bool(cond)
if stack == "running":
    cond = None not in (db_roles, full_apply) and db_roles < full_apply
    print(("PASS " if cond else "FAIL ") + "db-roles runs before the first untargeted apply")
    ok &= bool(cond)
    # A targeted apply only writes an output whose resources are all targeted (see
    # tftarget.sh), and db_roles_task is new on stacks deployed before it existed.
    targeted = [l for l in log[: db_roles or 0] if "-target=aws_ecs_task_definition.db_roles" in l]
    needed = ["aws_ecs_task_definition.db_roles", "aws_subnet.private", "aws_security_group.app",
              "aws_secretsmanager_secret_version.db_master"]
    cond = bool(targeted) and all(f"-target={r}" in targeted[0].split() for r in needed)
    print(("PASS " if cond else "FAIL ") + "the targeted apply covers db_roles_task's resources")
    ok &= cond
    cond = not any("secret_version.app" in l for l in targeted)
    print(("PASS " if cond else "FAIL ") + "the targeted apply leaves the app's secret alone")
    ok &= cond
print("\n".join(f"  {i:2} {l}" for i, l in enumerate(log) if l.startswith(("terraform", "aws ecs run-task"))))
sys.exit(0 if ok else 1)
