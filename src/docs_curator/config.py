"""Configuration management for the Docs Curator."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

SYSTEM_PROMPT = """\
You are the Databricks Docs Curator. You answer questions about Declarative Automation Bundles (formerly \
Databricks Asset Bundles) and Lakeflow Jobs from the Databricks documentation, which your tools return.

Tools:
- search_docs searches the content of the documentation pages. Use it for every how-to, configuration or \
YAML question. Write the query in the words the docs use, with the configuration keys you expect (for \
example depends_on, job_clusters, mode: development). Filters narrow the search, so use them only when \
you are sure: product_area "bundles" holds the bundle syntax and resource reference, "jobs" holds how jobs \
behave (tasks, triggers, schedules, parameters, retries). A question about a job defined in a bundle often \
needs both: search without product_area, or search each area. Set has_code to true only to find an example \
after the explanation is known: the reference tables listing which keys exist are not code. If the results don't answer the question, search again with other terms \
before giving up.
- The Genie space tools (query_space_..., poll_response_...) answer questions about the catalog of \
documentation pages itself: which pages exist, how many per product area, which were updated recently. \
They don't know what the pages say.

Rules:
- Answer only from what the tools returned in this conversation. If it doesn't contain the answer, say so.
- Cite every page you used as a Markdown link, [page title](url), with the url field of the result. Never \
cite chunk ids.
- Put YAML and code in fenced blocks. Every key in your YAML must appear in a tool result of this \
conversation; if no result shows the configuration, say so and describe it in words instead of guessing.
- Greetings, and follow-ups that earlier answers already cover, need no tool.
- Be concise."""


class ProjectConfig(BaseModel):
    """Project configuration model."""

    catalog: str = Field(..., description="Unity Catalog name")
    db_schema: str = Field(..., description="Schema name", alias="schema")
    volume: str = Field(..., description="Volume name")
    llm_endpoint: str = Field(..., description="AI Gateway chat model service name")
    embedding_endpoint: str = Field(..., description="Embedding endpoint name, callable from ai_query")
    experiment_path: str = Field(..., description="MLflow experiment path for this environment")
    warehouse_id: str = Field(..., description="Warehouse ID")
    lakebase_project_id: str = Field(..., description="Lakebase project ID")
    usage_policy_id: str | None = Field(None, description="Usage policy id")
    genie_space_id: str | None = Field(None, description="Genie space ID for MCP integration")
    system_prompt: str = Field(default=SYSTEM_PROMPT, description="System prompt for the agent")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_yaml(cls, config_path: str, env: str = "dev") -> "ProjectConfig":
        """Load configuration from YAML file.

        Args:
            config_path: Path to the YAML configuration file
            env: Environment name (dev, acc, prd)

        Returns:
            ProjectConfig instance

        """
        if env not in ["prd", "acc", "dev"]:
            raise ValueError(f"Invalid environment: {env}. Expected 'prd', 'acc', or 'dev'")

        with open(config_path) as f:
            config_data = yaml.safe_load(f)

        if env not in config_data:
            raise ValueError(f"Environment '{env}' not found in config file")

        env_config = config_data[env]
        if "usage_policy_id" in config_data:
            env_config["usage_policy_id"] = config_data["usage_policy_id"]

        return cls(**env_config)

    @property
    def schema(self) -> str:
        """Alias for db_schema for backward compatibility."""
        return self.db_schema

    @property
    def full_schema_name(self) -> str:
        """Get fully qualified schema name."""
        return f"{self.catalog}.{self.db_schema}"

    @property
    def memory_schema(self) -> str:
        """Postgres schema of the agent's session memory: one per catalog, as the Lakebase project is shared."""
        return f"{self.catalog}_memory"

    @property
    def full_volume_path(self) -> str:
        """Get fully qualified volume path."""
        return f"{self.catalog}.{self.schema}.{self.volume}"


def load_config(config_path: str = "project_config.yml", env: str = "dev") -> ProjectConfig:
    """Load project configuration.

    Args:
        config_path: Path to configuration file
        env: Environment name

    Returns:
        ProjectConfig instance

    """
    # Handle relative paths from notebooks
    if not Path(config_path).is_absolute():
        # Try to find config in parent directories
        current = Path.cwd()
        for _ in range(3):  # Search up to 3 levels
            candidate = current / config_path
            if candidate.exists():
                config_path = str(candidate)
                break
            current = current.parent

    return ProjectConfig.from_yaml(config_path, env)


def get_env() -> str:
    """Get current environment from environment variable.

    Returns:
        Environment name (dev, acc, or prd)

    """
    return os.getenv("ENV", "dev")
