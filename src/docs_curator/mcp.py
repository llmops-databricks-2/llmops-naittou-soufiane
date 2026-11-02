"""Tools from Databricks managed MCP servers, in the same ToolInfo shape as the custom tools."""

import json
from collections.abc import Callable
from typing import Any

import nest_asyncio
from databricks.sdk import WorkspaceClient
from databricks_mcp import DatabricksMCPClient
from pydantic import BaseModel, ConfigDict

# Rows of a Genie result passed to the model; docs_metadata has 50 pages
MAX_GENIE_ROWS = 50


class ToolInfo(BaseModel):
    """A tool the agent can call.

    Attributes:
        name: Tool name, as the model calls it
        spec: OpenAI function-calling specification
        exec_fn: Function running the tool on the model's arguments, returning text

    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    spec: dict
    exec_fn: Callable[..., str]


def compact_genie_response(text: str) -> str:
    """Reduce a Genie MCP response to what the model needs: status, ids, answer, SQL and rows.

    The raw response carries the full statement manifest (chunks, byte counts, typed values), about three
    times the size of the data. Anything that isn't a Genie message is returned unchanged.
    """
    try:
        response = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(response, dict) or "content" not in response:
        return text

    content = response.get("content") or {}
    compact: dict[str, Any] = {
        "status": response.get("status"),
        "conversation_id": response.get("conversationId"),
        "message_id": response.get("messageId"),
        "answer": "\n".join(content.get("textAttachments") or []),
    }
    for attachment in content.get("queryAttachments") or []:
        statement = attachment.get("statement_response") or {}
        columns = [column["name"] for column in statement.get("manifest", {}).get("schema", {}).get("columns", [])]
        rows = [
            [next(iter(value.values()), None) for value in row.get("values", [])]
            for row in statement.get("result", {}).get("data_array", [])
        ]
        compact["sql"] = attachment.get("query")
        compact["columns"] = columns
        compact["rows"] = rows[:MAX_GENIE_ROWS]
        compact["total_rows"] = statement.get("manifest", {}).get("total_row_count", len(rows))
    return json.dumps(compact)


def _exec_fn(server_url: str, tool_name: str, workspace: WorkspaceClient) -> Callable[..., str]:
    def exec_fn(**kwargs: Any) -> str:  # noqa: ANN401
        client = DatabricksMCPClient(server_url=server_url, workspace_client=workspace)
        response = client.call_tool(tool_name, kwargs)
        return compact_genie_response("".join(content.text for content in response.content))

    return exec_fn


def create_mcp_tools(workspace: WorkspaceClient, server_urls: list[str]) -> list[ToolInfo]:
    """List the tools of managed MCP servers as ToolInfo objects.

    Args:
        workspace: Workspace client whose identity calls the servers
        server_urls: MCP server URLs, such as {host}/api/2.0/mcp/genie/{space_id}

    Returns:
        One ToolInfo per tool, keeping the server's name, description and input schema

    """
    # The MCP client runs asyncio, which needs nesting inside a notebook's running event loop
    nest_asyncio.apply()
    tools = []
    for server_url in server_urls:
        client = DatabricksMCPClient(server_url=server_url, workspace_client=workspace)
        for tool in client.list_tools():
            spec = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or f"Tool: {tool.name}",
                    "parameters": dict(tool.inputSchema or {"type": "object", "properties": {}}),
                },
            }
            tools.append(ToolInfo(name=tool.name, spec=spec, exec_fn=_exec_fn(server_url, tool.name, workspace)))
    return tools


def genie_server_url(workspace: WorkspaceClient, space_id: str) -> str:
    """URL of the managed MCP server of a Genie space."""
    return f"{workspace.config.host.rstrip('/')}/api/2.0/mcp/genie/{space_id}"
