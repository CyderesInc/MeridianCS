import importlib.util
import os
import re
import sys

import pytest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The scripts import toolkit as a sibling module.
sys.path.insert(0, TOOLS)


@pytest.fixture(scope="module")
def pii_gate():
    spec = importlib.util.spec_from_file_location("check_docs_pii", os.path.join(TOOLS, "check-docs-pii.py"))
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def test_internal_only_exemption_stays_out_of_the_publication_audit(pii_gate):
    """design/oss-release.md must name the identifiers it removed, so the repo sweep exempts it --
    safe only because design/ never ships. make-public.py's audit reads SKIP_FILES, not the wider
    REPO_SWEEP_SKIP, so the two sets must stay disjoint or that exemption reaches a public tree."""
    assert not pii_gate.INTERNAL_ONLY_FILES & pii_gate.SKIP_FILES
    assert pii_gate.REPO_SWEEP_SKIP == pii_gate.SKIP_FILES | pii_gate.INTERNAL_ONLY_FILES


@pytest.mark.parametrize("ref", ["pre-commit/action@v3.0.1", "actions/checkout@v4", "ruff-pre-commit@v0.16.4"])
def test_version_ref_is_not_an_email_address(pii_gate, ref):
    assert re.findall(pii_gate.PATTERNS["email address"], ref) == []


@pytest.mark.parametrize("address", ["someone@example.com", "first.last+tag@mail.example.org",
                                     "svc_account@corp.example"])
def test_real_email_address_is_still_caught(pii_gate, address):
    """Both directions: silencing the version-ref noise must not silence a real address, which is
    the failure that leaves no trace."""
    assert re.findall(pii_gate.PATTERNS["email address"], address)


def test_internal_record_is_never_published():
    """Decided 2026-09-23 on counsel's advice (design/oss-release.md): CLAUDE.md and design/ hold
    privileged material. Pinned here so publishing it takes a visible change to a test."""
    spec = importlib.util.spec_from_file_location("make_public", os.path.join(TOOLS, "make-public.py"))
    make_public = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_public)
    assert make_public.PUBLISH_INTERNAL_DOCS is False


def test_only_release_workflow_can_sign():
    """Vault binds the signing roles to release.yml, so a Vault login, an OIDC token or a signing
    step anywhere else is a signing capability added, and a pull_request trigger on release.yml
    would hand one to a PR (design/release-signing.md, decision 6)."""
    workflows = os.path.join(os.path.dirname(os.path.dirname(TOOLS)), ".github", "workflows")
    for name in os.listdir(workflows):
        with open(os.path.join(workflows, name), encoding="utf-8") as f:
            code = "\n".join(line.split("#", 1)[0] for line in f.read().splitlines())
        if name == "release.yml":
            assert "pull_request" not in code.split("\njobs:", 1)[0]
        else:
            assert not [w for w in ("vault-action", "id-token", "sign-release.py") if w in code], name


def _release_workflow_code():
    workflow = os.path.join(os.path.dirname(os.path.dirname(TOOLS)), ".github", "workflows", "release.yml")
    with open(workflow, encoding="utf-8") as f:
        return "\n".join(line.split("#", 1)[0].rstrip() for line in f.read().splitlines())


def test_public_release_follows_the_internal_one_after_approval():
    """The environment is the public release's only approval, since Vault doesn't bind it. `needs:
    release` is the job's only proof that the tag is on main, the suite passed and the internal
    release exists."""
    jobs = _release_workflow_code().split("\njobs:", 1)[1]
    public = next(job for job in re.split(r"\n  (?=\S)", jobs) if "publish-public.py" in job)
    assert "needs: release" in public
    assert "environment: public-release" in public


@pytest.fixture(scope="module")
def publish_public():
    spec = importlib.util.spec_from_file_location("publish_public", os.path.join(TOOLS, "publish-public.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("head, released, refused", [
    ("abc1234 Release v2.28.1", False, True),
    ("abc1234 Release v2.28.1", True, False),
    ("abc1234 Release v2.28.0", False, False),
])
def test_rerun_refuses_a_pushed_but_unreleased_commit(publish_public, monkeypatch, head, released, refused):
    """A re-run after a pushed but unpublished release commit used to go green with nothing
    released. An older release commit is an internal-only change."""
    monkeypatch.setattr(publish_public.toolkit, "git", lambda *args, cwd: head)
    monkeypatch.setattr(publish_public, "release_exists", lambda repo, tag: released)
    if refused:
        with pytest.raises(SystemExit, match="v2.28.1 has no release"):
            publish_public.refuse_unpublished_release_commit("clone", "owner/public", "2.28.1")
    else:
        publish_public.refuse_unpublished_release_commit("clone", "owner/public", "2.28.1")
