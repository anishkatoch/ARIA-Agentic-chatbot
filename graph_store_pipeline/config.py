import os
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def _load_graph_yaml_section() -> dict:
    """Best-effort read of the host app's config.yaml `graph:` section
    (one directory up from this package). Keeps this package usable
    standalone - falls back to defaults if yaml isn't installed or no
    config.yaml is found, same as if it were copied into a bare project."""
    if yaml is None:
        return {}
    try:
        yaml_path = Path(__file__).parent.parent / "config.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        return (data or {}).get("graph", {})
    except Exception:
        return {}


_graph_yaml = _load_graph_yaml_section()


class GraphStoreConfig:
    """Standalone defaults for this package - no required dependency on
    any host app's config system, though it will pick up a `graph:`
    section from a sibling config.yaml if one exists."""

    def __init__(self):
        # Primary backend
        self.neo4j_uri: str = os.getenv("NEO4J_URI", "")
        self.neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")

        # Secondary/fallback backend - Azure Cosmos DB (Gremlin API).
        # Only used when Neo4j is unset or unreachable. Endpoint/key are
        # secrets (.env); database/graph names are not secrets and come
        # from config.yaml's `graph:` section.
        self.cosmos_gremlin_endpoint: str = os.getenv("COSMOS_GREMLIN_ENDPOINT", "")
        self.cosmos_gremlin_key: str = os.getenv("COSMOS_GREMLIN_KEY", "")
        self.cosmos_gremlin_database: str = _graph_yaml.get("cosmos_gremlin_database", "aria-graphdb")
        self.cosmos_gremlin_graph: str = _graph_yaml.get("cosmos_gremlin_graph", "aria-graph")


cfg = GraphStoreConfig()
