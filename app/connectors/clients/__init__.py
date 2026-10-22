"""Source-system connector clients, ported from CONNECTOR_POC/app/connectors/.

Re-exports the same factory functions / classes the PoC exposed, so callers
(app/connectors/router.py) can import from one place.
"""
from app.connectors.clients.github import get_github_connector
from app.connectors.clients.gmail import get_gmail_connector
from app.connectors.clients.google_drive import get_google_drive_connector
from app.connectors.clients.google_chat import get_google_chat_connector
from app.connectors.clients.outlook import get_outlook_connector
from app.connectors.clients.outlook_chat import get_outlook_chat_connector
from app.connectors.clients.microsoft_graph import MicrosoftGraphError
from app.connectors.clients.sharepoint import SharePointConnector
from app.connectors.clients.teams import TeamsConnector
from app.connectors.clients.azure_devops import AzureDevOpsConnector, AzureDevOpsError

__all__ = [
    "get_github_connector",
    "get_gmail_connector",
    "get_google_drive_connector",
    "get_google_chat_connector",
    "get_outlook_connector",
    "get_outlook_chat_connector",
    "MicrosoftGraphError",
    "SharePointConnector",
    "TeamsConnector",
    "AzureDevOpsConnector",
    "AzureDevOpsError",
]
