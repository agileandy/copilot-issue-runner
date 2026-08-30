import json

import pytest

from issue_runner.trackers import RemoteInfo, TrackerError, fetch_gitea_issue, parse_remote


def test_parse_github_https():
    info = parse_remote("https://github.com/agileandy/copilot-issue-runner.git")
    assert info == RemoteInfo("github", "https://github.com", "agileandy/copilot-issue-runner")


def test_parse_github_scp_like():
    info = parse_remote("git@github.com:owner/repo.git")
    assert info.kind == "github"
    assert info.owner_repo == "owner/repo"


def test_parse_gitea_http():
    info = parse_remote("http://192.168.1.205:3000/Org/my-repo.git")
    assert info == RemoteInfo("gitea", "http://192.168.1.205:3000", "Org/my-repo")


def test_parse_gitea_ssh_has_no_api_base():
    info = parse_remote("ssh://git@studio-git:2222/Org/my-repo.git")
    assert info.kind == "gitea"
    assert info.api_base is None  # ssh port is not the API port; needs GITEA_URL env
    assert info.owner_repo == "Org/my-repo"


def test_parse_garbage_raises():
    with pytest.raises(TrackerError):
        parse_remote("not-a-remote-url")


def test_fetch_gitea_issue_maps_fields():
    def fake_get(url, token):
        assert url == "http://gitea.local:3000/api/v1/repos/Org/repo/issues/7"
        assert token == "sekrit"
        return json.dumps(
            {"number": 7, "title": "T", "body": "B", "html_url": "http://gitea.local/x/7"}
        )

    issue = fetch_gitea_issue(
        "http://gitea.local:3000", "Org/repo", "7", token="sekrit", getter=fake_get
    )
    assert issue == {"number": 7, "title": "T", "body": "B", "url": "http://gitea.local/x/7"}


def test_fetch_gitea_issue_requires_token():
    with pytest.raises(TrackerError, match="GITEA_TOKEN"):
        fetch_gitea_issue("http://g", "o/r", "1", token=None)


def test_fetch_gitea_issue_null_body_becomes_empty():
    def fake_get(url, token):
        return json.dumps({"number": 1, "title": "T", "body": None, "html_url": "u"})

    issue = fetch_gitea_issue("http://g", "o/r", "1", token="t", getter=fake_get)
    assert issue["body"] == ""
