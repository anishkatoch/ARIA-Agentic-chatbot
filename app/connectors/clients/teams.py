# app/connectors/clients/teams.py
from typing import Any
from urllib.parse import quote
from app.connectors.clients.microsoft_graph import MicrosoftGraphClient


class TeamsConnector:
    """Read-only Microsoft Teams operations using Microsoft Graph."""

    def __init__(self, user_id: str) -> None:
        self.graph = MicrosoftGraphClient(user_id, "teams")

    @staticmethod
    def _segment(value: str) -> str:
        if not value or not value.strip():
            raise ValueError("A non-empty ID is required.")
        return quote(value, safe="")

    def list_teams(self) -> list[dict[str, Any]]:
        # Delegated access (Team.ReadBasic.All) only sees teams the signed-in
        # user has joined - "/me/joinedTeams", not a tenant-wide "/teams"
        # listing (which needs app-only/directory-level permissions this
        # connector no longer has - see microsoft_graph.py).
        return self.graph.get_all(
            "/me/joinedTeams",
            params={"$select": "id,displayName,description"},
        )

    def list_channels(self, team_id: str) -> list[dict[str, Any]]:
        team = self._segment(team_id)
        return self.graph.get_all(
            f"/teams/{team}/channels",
            params={
                "$select": "id,displayName,description,membershipType"
            },
        )

    def list_channel_messages(
        self,
        team_id: str,
        channel_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        team = self._segment(team_id)
        channel = self._segment(channel_id)
        # Bound the amount returned to avoid unexpectedly large responses.
        limit = max(1, min(limit, 100))
        messages = self.graph.get_all(
            f"/teams/{team}/channels/{channel}/messages",
            params={
                "$top": str(limit),
                "$orderby": "createdDateTime desc",
            },
        )
        return messages[:limit]
