"""Tracker backends for mirroring sub-task tickets.

The local state file is always the source of truth; a backend mirrors tickets
to the repo's tracker so progress is visible there. The orchestrator calls
create() for any ticket without a tracker issue number (which also backfills
runs planned before a backend existed) and close() when a ticket lands.
"""

from . import github_io, trackers


class GithubTickets:
    def __init__(self, repo: str):
        self.repo = repo

    def create(self, parent_number: int, ticket) -> int:
        return github_io.create_subissue(self.repo, parent_number, ticket)

    def close(self, number: int, comment: str) -> None:
        github_io.close_subissue(self.repo, number, comment)


class GiteaTickets:
    def __init__(self, api_base: str, owner_repo: str):
        self.api_base = api_base
        self.owner_repo = owner_repo

    def create(self, parent_number: int, ticket) -> int:
        return trackers.create_gitea_subissue(self.api_base, self.owner_repo, parent_number, ticket)

    def close(self, number: int, comment: str) -> None:
        trackers.close_gitea_issue(self.api_base, self.owner_repo, number, comment)
