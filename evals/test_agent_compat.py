"""
The skill outside Claude: the open Agent Skills format that Gemini CLI, GitHub Copilot and OpenAI
Codex (the ChatGPT desktop app's agent) all read.

SKILL.md is not a Claude-only file. agentskills.io publishes the format as an open spec, and on
2026-10-15 the package built from main was loaded by the real CLIs, each from a clean home:

  Gemini CLI    `gemini skills list`, from ~/.agents/skills/        listed, full description, enabled
  Copilot CLI   `copilot skill list`, from <project>/.github/skills/ listed under "Project skills"
  Codex CLI     `codex debug prompt-input`, from <project>/.agents/skills/
                                                                    in the model-visible skills list

The spec's reference validator (skills-ref, agentskills/agentskills) also passed it. This file keeps
that true offline: the frontmatter is parsed with strictyaml, the same parser the validator uses, which
is stricter than any of the three clients. `--agents` repeats the real-CLI load for any of the three on
PATH; none of them is in CI.

Discovery is all these can prove. Whether a session then works depends on the client giving the model a
shell that can reach the stack and write ~/.meridian, which Codex's default `workspace-write` sandbox
does not: it allows no network and no writes outside the workspace. ChatGPT's web chat runs no
standalone skills at all.

    uv run --locked --project scripts/tools pytest evals -k agent_compat             # offline
    uv run --locked --project scripts/tools pytest evals -k agent_compat --agents    # + real CLIs
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

import pytest
import pytest_check
import strictyaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL_MD = os.path.join(ROOT, "SKILL.md")
MAKE_PACKAGE = os.path.join(ROOT, "scripts", "tools", "make-package.py")

# agentskills.io/specification. Anything else in the frontmatter fails the reference validator.
SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
SPEC_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SPEC_NAME_MAX = 64
SPEC_DESCRIPTION_MAX = 1024
SPEC_COMPATIBILITY_MAX = 500

# Claude Code's own tools and variables. Another client has none of them, so an instruction that names
# one is an instruction the model there cannot follow -- and it fails quietly, as a skipped step.
CLAUDE_ONLY = ("AskUserQuestion", "TodoWrite", "TaskCreate", "Skill tool", "Task tool", "${CLAUDE",
               "CLAUDE_SKILL_DIR", "CLAUDE_PLUGIN_ROOT", "~/.claude", ".claude/")


def check(name, got, want):
    pytest_check.equal(got, want, name)


def _flat(text):
    return " ".join(text.split())


def _frontmatter():
    text = open(SKILL_MD, encoding="utf-8").read()
    parts = text.split("---", 2)
    return strictyaml.load(parts[1]).data


def _make_package():
    sys.path.insert(0, os.path.dirname(MAKE_PACKAGE))
    try:
        spec = importlib.util.spec_from_file_location("make_package", MAKE_PACKAGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


def test_skill_meets_agent_skills_spec():
    """The frontmatter passes the open spec's rules, not only Claude's.

    Claude enforces the 1024-char description and 64-char name (test_skill_frontmatter). The spec adds
    a closed field list, a name alphabet, and that the name matches the folder it is installed in. An
    install is the package unzipped, so the folder is make-package.py's PREFIX, not this checkout's
    name: CI clones it as MeridianCS-internal."""
    fm = _frontmatter()
    check("frontmatter is a strict-YAML mapping", isinstance(fm, dict), True)
    check("only spec fields (extra: %s)" % sorted(set(fm) - SPEC_FIELDS), set(fm) <= SPEC_FIELDS, True)
    name = fm.get("name", "")
    check("name uses the spec alphabet (%r)" % name, bool(SPEC_NAME_RE.match(name)), True)
    check("name is within %d chars" % SPEC_NAME_MAX, 0 < len(name) <= SPEC_NAME_MAX, True)
    check("the package unzips into a folder named after the skill", _make_package().PREFIX, name + "/")
    desc = fm.get("description", "")
    check("description is 1-%d chars (is %d)" % (SPEC_DESCRIPTION_MAX, len(desc)),
          0 < len(desc.strip()) <= SPEC_DESCRIPTION_MAX, True)
    if "compatibility" in fm:
        check("compatibility is within %d chars" % SPEC_COMPATIBILITY_MAX,
              len(fm["compatibility"]) <= SPEC_COMPATIBILITY_MAX, True)
    # test_skill_frontmatter's 90% headroom guard measures a hand-folded value. If that fold ever
    # disagreed with a real YAML parser, the guard would be measuring the wrong string.
    from test_connect import _skill_frontmatter
    check("the hand-folded description matches a real YAML parse",
          _skill_frontmatter()["description"], desc)


def test_skill_paths_resolve_in_an_install():
    """Every file SKILL.md points at ships in the package.

    The spec resolves paths from the skill root, which is the unzipped package. A link that resolves in
    this checkout can still dangle in an install, because make-package.py drops design/, evals/ and
    scripts/tools/. Prose like "assets/users" is not a path, so only names with an extension count."""
    mp = _make_package()
    shipped = set(mp.packaged_files()) | {mp.VERSION_MEMBER}
    text = open(SKILL_MD, encoding="utf-8").read()
    links = {l.split("#")[0] for l in re.findall(r"\]\(([^)\s]+)\)", text)
             if not re.match(r"[a-z]+:|#", l)}
    paths = set(re.findall(r"(?<![\w/.~-])((?:scripts|references|assets)/[\w./-]*\.\w+)", text))
    check("SKILL.md names at least the script and the references", len(links | paths) >= 5, True)
    for p in sorted(links | paths):
        check("SKILL.md's %s ships in the package" % p, p in shipped, True)


def test_skill_names_no_claude_only_tools():
    """SKILL.md never instructs the model to use something only Claude Code has.

    Today it relies only on a shell, file reads and Python, which every client provides. A later edit
    that reached for a Claude Code tool would read fine here and silently do nothing elsewhere."""
    text = open(SKILL_MD, encoding="utf-8").read()
    for term in CLAUDE_ONLY:
        check("SKILL.md does not name Claude-only %r" % term, term in text, False)


# --- --agents: the real CLIs -------------------------------------------------------------------------

def _build_and_unzip(tmp_path, dest):
    out = tmp_path / "pkg"
    out.mkdir()
    subprocess.run([sys.executable, MAKE_PACKAGE, "--label", "agents", "--out-dir", str(out)],
                   check=True, capture_output=True, cwd=ROOT)
    (zipped,) = list(out.glob("*.skill.zip"))
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zipped) as z:
        z.extractall(dest)
    return dest / "meridiancs"


def _run(cli, args, home, cwd):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex"))
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    r = subprocess.run([cli] + args, cwd=str(cwd), env=env, capture_output=True, timeout=180,
                       encoding="utf-8", errors="replace")
    return r.stdout


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


# Each client is pointed at the location it was verified loading from. Copilot also reads the real
# ~/.claude/skills whatever HOME says (it lists those as "Inherited"), so it is checked as a project
# skill, where only this package can be the one it found.
AGENT_CLIS = {
    "gemini": (".agents/skills", "home", ["skills", "list"]),
    "copilot": (".github/skills", "project", ["skill", "list"]),
    "codex": (".agents/skills", "project", ["debug", "prompt-input", "how many servers in AWS?"]),
}


@pytest.mark.agents
@pytest.mark.parametrize("client", sorted(AGENT_CLIS))
def test_agent_cli_loads_package(client, tmp_path):
    """The built package is discovered by another vendor's agent, with its description intact.

    A truncated or mis-parsed description would route nothing, so the whole description must come
    back, not only the name. Skips a client that is not on PATH."""
    cli = shutil.which(client)
    if not cli:
        pytest.skip("%s is not on PATH" % client)
    rel, where, args = AGENT_CLIS[client]
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(project), check=True)
    _build_and_unzip(tmp_path, (home if where == "home" else project) / rel)
    out = _run(cli, args, home, project)
    if client == "copilot":
        m = re.search(r"Project skills:\n(.*?)(?:\n\S|\Z)", out, re.S)
        out = m.group(1) if m else ""
    if client == "codex":
        # The rendered prompt is JSON, so the description's quotes arrive escaped until it is parsed.
        try:
            out = "\n".join(_strings(json.loads(out)))
        except ValueError:
            pass
    desc = _frontmatter()["description"]
    check("%s lists meridiancs" % client, "meridiancs" in out, True)
    check("...with its whole description", _flat(desc) in _flat(out), True)
