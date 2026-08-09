"""Registration drift: a SKILL.md that nobody loads is a skill that doesn't exist.

Every skill in the tap has to appear in BOTH manifests — `skills.sh.json` (the hub's tap-root
category list) and `adapters/hermes/sotto.bundle.yaml` (what `/sotto` actually loads). Adding a
skill and forgetting one of them has happened; the failure is silent at runtime. Also pins the
`sotto-routines` fence, because the whole personal-routines feature rests on that one prefix.
"""
import json
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))            # sotto-chief-of-staff/
HERMES = os.path.dirname(ROOT)                              # sotto-hermes/
BUNDLE = os.path.join(HERMES, "adapters", "hermes", "sotto.bundle.yaml")
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


def test_every_skill_is_registered_in_both_manifests():
    names = set(_skill_names())
    with open(os.path.join(ROOT, "skills.sh.json"), encoding="utf-8") as f:
        listed = {s for g in json.load(f)["groupings"] for s in g["skills"]}
    with open(BUNDLE, encoding="utf-8") as f:
        bundled = set(yaml.safe_load(f)["skills"])
    assert names == listed, f"skills.sh.json drift: {names ^ listed}"
    assert names == bundled, f"sotto.bundle.yaml drift: {names ^ bundled}"


def test_routines_skill_exists_and_is_registered():
    assert "sotto-routines" in _skill_names()


def test_routines_skill_states_the_fence():
    """The three load-bearing promises of the personal-routines feature, in the skill the model
    actually reads: only `user-` names are created, only `user-` jobs are removed, and the cap."""
    with open(ROUTINES, encoding="utf-8") as f:
        text = f.read()
    assert "--name user-<slug>" in text
    assert 'hermes cron create "<cron>" "<prompt>" --name user-<slug> --deliver' in text
    assert "hermes cron remove" in text and "Remove only `user-` jobs." in text
    assert "Cap: 10 routines" in text
    assert '"${SOTTO_CRON_DELIVER:-whatsapp}"' in text          # never the silent `local` sink
    # drafts-never-send and no-recursion are inherited guards, stated here too
    assert "No routine schedules a routine." in text
    assert "Drafts, never sends" in text
    # the honest v1 timezone limitation
    assert "Timezone changes don't move existing routines" in text


def test_no_user_prefixed_job_in_the_system_cron_spec():
    """crons.json is the SYSTEM schedule. A `user-` name there would be re-registered and removed by
    the boot cleanup on the owner's behalf — the fence assumes the two namespaces never overlap."""
    with open(os.path.join(HERMES, "adapters", "hermes", "crons.json"), encoding="utf-8") as f:
        spec = json.load(f)
    assert spec and not [j for j in spec if j["name"].startswith("user-")]
    assert all(j["name"].startswith("sotto-") for j in spec)
