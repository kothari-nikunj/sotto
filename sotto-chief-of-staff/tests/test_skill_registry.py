"""Registration drift: a SKILL.md that nobody loads is a skill that doesn't exist.

Every skill in the pack has to appear in `skills.sh.json` — the index the Sotto runtime loads
skills from (runtime/sotto-chat/src/tools/skills.ts). Adding a skill and forgetting it has happened. Also pins the
`sotto-routines` fence, because the whole personal-routines feature rests on that one prefix.
"""
import json
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))            # sotto-chief-of-staff/
AGENT_ROOT = os.path.dirname(ROOT)                              # sotto-agent/
ROUTINES = os.path.join(ROOT, "routines", "SKILL.md")


def _skill_names() -> dict:
    """{skill name from frontmatter: directory} for every SKILL.md in the tap."""
    names = {}
    for entry in sorted(os.listdir(ROOT)):
        path = os.path.join(ROOT, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            fm = yaml.safe_load(re.match(r"^---\n(.*?)\n---\n", f.read(), re.S).group(1))
        names[fm["name"]] = entry
    return names


def test_every_skill_is_registered_in_the_tap_manifest():
    names = set(_skill_names())
    with open(os.path.join(ROOT, "skills.sh.json"), encoding="utf-8") as f:
        listed = {s for g in json.load(f)["groupings"] for s in g["skills"]}
    assert names == listed, f"skills.sh.json drift: {names ^ listed}"


def test_routines_skill_exists_and_is_registered():
    assert "sotto-routines" in _skill_names()


def test_routines_skill_states_the_fence():
    """The three load-bearing promises of the personal-routines feature, in the skill the model
    actually reads: only `user-` names are created, only `user-` jobs are removed, and the cap —
    each through the Sotto runtime's own routine tools."""
    with open(ROUTINES, encoding="utf-8") as f:
        text = f.read()
    assert "The proposal's `name` is always `user-<slug>`" in text
    assert "`owner_routine_propose`" in text and "`owner_routine_create`" in text
    assert "`owner_routine_cancel` refuses anything else" in text and "Remove only `user-` jobs." in text
    assert "Cap: 10 routines" in text
    # the destination is the receiver's, never a channel name the model picks
    assert "The destination is fixed by the receiver" in text and "Never name or guess a channel" in text
    assert ":-whatsapp" not in text and ":-telegram" not in text
    # drafts-never-send and no-recursion are inherited guards, stated here too
    assert "No routine schedules a routine." in text
    assert "Drafts, never sends" in text
    # the honest v1 timezone limitation
    assert "Timezone changes don't move existing routines" in text


def test_no_user_prefixed_job_in_the_system_cron_spec():
    """crons.json is the SYSTEM schedule. A `user-` name there would be re-registered and removed by
    the boot cleanup on the owner's behalf — the fence assumes the two namespaces never overlap."""
    with open(os.path.join(AGENT_ROOT, "adapters", "sotto", "crons.json"), encoding="utf-8") as f:
        spec = json.load(f)
    assert spec and not [j for j in spec if j["name"].startswith("user-")]
    assert all(j["name"].startswith("sotto-") for j in spec)
