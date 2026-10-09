from aios.db import SessionLocal
from aios.models import Skill
from aios.versioning import save_skill_version


def test_identical_save_after_concurrent_save_does_not_duplicate(db, user):
    """A save that read a stale current version must not create a duplicate version."""
    skill = Skill(owner_id=user.id, name="Race")
    db.add(skill)
    save_skill_version(db, skill, description="", content="v1", note=None, user_id=user.id)
    db.commit()

    with SessionLocal() as a, SessionLocal() as b:
        stale = a.get(Skill, skill.id)
        assert stale.current_version.content == "v1"  # loaded into a's identity map

        other = b.get(Skill, skill.id)
        v2 = save_skill_version(b, other, description="", content="v2", note=None, user_id=user.id)
        b.commit()

        again = save_skill_version(
            a, stale, description="", content="v2", note=None, user_id=user.id
        )
        a.commit()
        assert (again.id, again.version) == (v2.id, 2)
        assert stale.current_version_id == v2.id
