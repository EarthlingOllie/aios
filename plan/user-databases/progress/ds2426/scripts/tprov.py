import re
p='/home/user/aios/.claude/worktrees/agent-ab17abbcec02c8f74/backend/tests/test_provisioning.py'
s=open(p).read()
pat=re.compile(r"<<<<<<< HEAD\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n", re.S)
hunks=pat.findall(s)
print(len(hunks))
# 0 imports: theirs; 1 models import: theirs; 2 tests: both (ours then theirs); 3..: theirs
def choose(i, ours, theirs):
    if i == 2:
        return ours + "\n\n" + theirs
    return theirs
i=[0]
def rep(m):
    r=choose(i[0], m.group(1), m.group(2)); i[0]+=1; return r
s=pat.sub(rep, s)
open(p,'w').write(s)
