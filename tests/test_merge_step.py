"""Phase 5: the reviewed PR merges, after the base is merged in when it must be."""

import subprocess

from issue_runner.cli import _exit_code
from tests.fake_github import Head
from tests.test_review_loop import (  # noqa: F401  (fixture reuse)
    built_through_acceptance,
    env,
    git_repo,
    log_subjects,
    run,
)


def push_upstream(fake, factory, files: dict[str, str], message="feat: upstream change"):
    """Land a commit on origin/main from another clone, as a teammate would."""
    clone = factory.mktemp("teammate") / "clone"
    subprocess.run(["git", "clone", "-q", str(fake.remote), str(clone)], check=True)
    for name, content in files.items():
        (clone / name).write_text(content)
    git = ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", message], check=True)
    subprocess.run([*git, "push", "-q", "origin", "main"], check=True)


def resolver(effect=None):
    return ('{"notes": "kept both sides"}', effect)


def remote_subject(fake, ref="main"):
    return fake._git("log", "-1", "--format=%s", ref).stdout.strip()


def test_a_clean_pr_squash_merges_pinned_to_the_reviewed_head(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    _, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    d = store.delivery
    assert d.gates["merge"] == "pass"
    [call] = fake.merge_calls
    assert call == {
        "number": d.pr_number,
        "merge_method": "squash",
        "sha": d.head_sha,
        "commit_title": f"feat: Add subtract (#{d.pr_number})",
    }
    assert d.merge_sha == fake._tip("main")
    assert remote_subject(fake) == f"feat: Add subtract (#{d.pr_number})"


def test_a_behind_pr_merges_main_in_and_is_reviewed_again(env, git_repo, tmp_path_factory):  # noqa: F811
    fake, _, cfg = env
    fake.require_up_to_date = True
    push_upstream(fake, tmp_path_factory, {"other.py": "x = 1\n"})
    _, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    d = store.delivery
    assert d.gates["merge"] == "pass" and d.merge_attempts == 1
    assert log_subjects(git_repo)[0] == f"chore: merge origin/main into {store.branch}"
    assert fake.review_requests == [(d.pr_number, "copilot-pull-request-reviewer[bot]")]
    assert len(fake.heads) == 2  # the merged head was reviewed before it merged
    assert (git_repo / "other.py").exists()


def test_a_conflict_is_resolved_checked_and_rereviewed(env, git_repo, tmp_path_factory):  # noqa: F811
    fake, _, cfg = env
    push_upstream(fake, tmp_path_factory, {"impl.py": "upstream code\n"})

    def resolve():
        (git_repo / "impl.py").write_text("code\n# and the upstream intent\n")

    script = built_through_acceptance(git_repo) + [resolver(resolve)]
    _, store, client = run(cfg, git_repo, script)
    d = store.delivery
    assert d.gates["merge"] == "pass" and d.merge_attempts == 1
    prompt = next(c for c in client.calls if c["role"] == "resolver")["prompt"]
    assert "These paths conflict:\n  - impl.py" in prompt
    assert "conflict markers remain" in prompt and "test_sub.py" in prompt
    assert "upstream intent" in fake._git("show", "main:impl.py").stdout


def test_a_resolver_that_leaves_markers_is_aborted_cleanly(env, git_repo, tmp_path_factory):  # noqa: F811
    fake, _, cfg = env
    cfg.coder_retries = 1
    push_upstream(fake, tmp_path_factory, {"impl.py": "upstream code\n"})
    script = built_through_acceptance(git_repo) + [resolver(), resolver()]
    report, store, _ = run(cfg, git_repo, script)
    assert report.dod_failed_gate == "merge" and _exit_code(report) == 7
    assert "conflict markers remain" in store.delivery.failed_reason
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout
    assert status == ""
    assert (
        subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=git_repo,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
    assert fake.merge_calls == []


def test_a_resolution_that_breaks_a_ticket_test_is_sent_back(env, git_repo, tmp_path_factory):  # noqa: F811
    fake, _, cfg = env
    push_upstream(fake, tmp_path_factory, {"impl.py": "upstream code\n"})

    def drop_impl():
        (git_repo / "impl.py").unlink()  # the RED test needs impl.py

    def keep_ours():
        (git_repo / "impl.py").write_text("code\n")

    script = built_through_acceptance(git_repo) + [resolver(drop_impl), resolver(keep_ours)]
    _, store, client = run(cfg, git_repo, script)
    assert store.delivery.gates["merge"] == "pass"
    second = [c for c in client.calls if c["role"] == "resolver"][1]["prompt"]
    assert "test_sub.py now fails" in second


def test_a_base_that_keeps_moving_exhausts_the_merge_attempts(env, git_repo, tmp_path_factory):  # noqa: F811
    fake, _, cfg = env
    fake.require_up_to_date = True
    cfg.deploy_settings.max_merge_attempts = 1
    push_upstream(fake, tmp_path_factory, {"one.py": "1\n"})
    fake.on_review_request = lambda: push_upstream(
        fake, tmp_path_factory, {"two.py": "2\n"}, "feat: another upstream change"
    )
    report, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "merge" and _exit_code(report) == 7
    assert "still behind after 1 update(s) from main" in store.delivery.failed_reason


def test_a_merge_github_refuses_once_is_retried(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.refuse_merges = 1
    _, store, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert store.delivery.gates["merge"] == "pass"
    assert len(fake.merge_calls) == 2 and store.delivery.merge_attempts == 1


def test_merging_needs_a_passed_review(env, git_repo):  # noqa: F811
    fake, _, cfg = env
    fake.scenarios = [Head(verdict=None)]
    report, _, _ = run(cfg, git_repo, built_through_acceptance(git_repo))
    assert report.dod_failed_gate == "review"
    assert fake.merge_calls == []


def test_a_resolver_crash_aborts_the_merge(env, git_repo, tmp_path_factory):  # noqa: F811
    from issue_runner.copilot import CopilotError

    fake, _, cfg = env
    push_upstream(fake, tmp_path_factory, {"impl.py": "upstream code\n"})

    def crash():
        raise CopilotError("boom")

    run(cfg, git_repo, built_through_acceptance(git_repo) + [resolver(crash)])
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout
    merge_head = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    )
    assert (status, merge_head.returncode != 0) == ("", True)
