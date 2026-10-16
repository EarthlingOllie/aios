import re
p='/home/user/aios/.claude/worktrees/agent-ab17abbcec02c8f74/backend/src/aios/datastores/provision.py'
s=open(p).read()
s=re.sub(r"<<<<<<< HEAD\n(.*?)=======\n.*?>>>>>>> [^\n]*\n", lambda m:m.group(1), s, flags=re.S)
s=s.replace("# Tries at dropping a store's database (see `drop`).\nDROP_TRIES = 10\n\n\n","")
s=s.replace("import sys\nimport time\n","import sys\n").replace("from psycopg import errors, sql\n","from psycopg import sql\n")
open(p,'w').write(s)
