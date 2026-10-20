"""Source-system connector clients, ported from CONNECTOR_POC/app/connectors/.

Re-exports the same factory functions / classes the PoC exposed, so callers
(app/connectors/router.py) can import from one place.
"""
from app.connectors.clients.file_share import get_connector as get_file_share_connector
from app.connectors.clients.lms import get_lms_connector
from app.connectors.clients.github import get_github_connector
from app.connectors.clients.gmail import get_gmail_connector
from app.connectors.clients.google_drive import get_google_drive_connector
from app.connectors.clients.salesforce import get_salesforce_connector
from app.connectors.clients.servicenow import get_servicenow_connector
from app.connectors.clients.jira import get_jira_connector
from app.connectors.clients.microsoft_graph import MicrosoftGraphError
from app.connectors.clients.sharepoint import SharePointConnector
from app.connectors.clients.teams import TeamsConnector
from app.connectors.clients.azure_devops import AzureDevOpsConnector, AzureDevOpsError

__all__ = [
    "get_file_share_connector",
    "get_lms_connector",
    "get_github_connector",
    "get_gmail_connector",
    "get_google_drive_connector",
    "get_salesforce_connector",
    "get_servicenow_connector",
    "get_jira_connector",
    "MicrosoftGraphError",
    "SharePointConnector",
    "TeamsConnector",
    "AzureDevOpsConnector",
    "AzureDevOpsError",
]
