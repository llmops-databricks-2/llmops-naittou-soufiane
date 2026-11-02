"""Short-lived connections to the Lakebase project, shared by pgvector search and session memory."""

import os
import urllib.parse

import psycopg
from databricks.sdk import WorkspaceClient


def connection_string(project_id: str, workspace: WorkspaceClient | None = None) -> str:
    """Create a Lakebase connection string with a fresh database credential (valid for one hour).

    A Databricks app exposes its Lakebase resource as LAKEBASE_ENDPOINT, PGHOST and PGDATABASE; elsewhere
    the first endpoint of the project's default branch is used. A service principal (DATABRICKS_CLIENT_ID)
    logs in as its application id, a user as their user name.
    """
    workspace = workspace or WorkspaceClient()
    postgres_api = workspace.postgres
    endpoint_name = os.environ.get("LAKEBASE_ENDPOINT")
    if endpoint_name:
        host = os.environ.get("PGHOST")
        database = os.environ.get("PGDATABASE", "databricks_postgres")
    else:
        branch = next(iter(postgres_api.list_branches(parent=f"projects/{project_id}")))
        endpoint = next(iter(postgres_api.list_endpoints(parent=branch.name)))
        endpoint_name = endpoint.name
        host = endpoint.status.hosts.host
        database = "databricks_postgres"
    credential = postgres_api.generate_database_credential(endpoint=endpoint_name)
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    username = urllib.parse.quote_plus(client_id or workspace.current_user.me().user_name)
    return f"postgresql://{username}:{credential.token}@{host}:5432/{database}?sslmode=require"


def connect(project_id: str, workspace: WorkspaceClient | None = None) -> psycopg.Connection:
    """Open a Lakebase connection; use it as a context manager, which commits on exit."""
    return psycopg.connect(connection_string(project_id, workspace))
