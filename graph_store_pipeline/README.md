# graph_store_pipeline

Self-contained knowledge-graph layer with two backends: extracts
entity/relationship triples from text chunks via an LLM you supply, and
lets you query them back as context for a question.

- **Primary: Neo4j.** Used whenever `NEO4J_URI`/`NEO4J_PASSWORD` are set
  and reachable.
- **Secondary/fallback: Azure Cosmos DB (Gremlin API).** Used automatically
  when Neo4j is unset or the connection fails - no code changes needed to
  switch, it's tried on every call.

This folder has no dependency on the rest of this repo - copy it into any
other Python project as-is. If neither backend is configured, every
function is a safe no-op (`build_graph` returns `False`, `query_graph`
returns `""`), so it's fine to call unconditionally from a host app's
ingest/chat paths.

## Usage

```python
from graph_store_pipeline import build_graph, query_graph, copy_graph_session

build_graph(chunks=["Alice works at Acme Corp.", "..."], session_id="s1", llm=my_llm)
context = query_graph("Who works at Acme?", session_id="s1")
copy_graph_session("s1", "s2")   # fork a session's graph (e.g. cloning a KB)
```

`llm` is any object with a langchain-style `.invoke(messages) -> response`
where `response.content` is a string - bring your own provider, this
package has no opinion on which one.

Install deps: `pip install -r graph_store_pipeline/requirements.txt`

## Configuration (env vars)

| Var | Default |
|---|---|
| `NEO4J_URI` | unset (falls through to Cosmos Gremlin until set) |
| `NEO4J_USER` | `neo4j` |
| `NEO4J_PASSWORD` | unset |
| `COSMOS_GREMLIN_ENDPOINT` | unset (fallback disabled until set; e.g. `wss://<account>.gremlin.cosmos.azure.com:443/`) |
| `COSMOS_GREMLIN_KEY` | unset (primary key from the Azure portal's "Keys" blade) |

Database/graph names aren't secrets, so they're not env vars - they come
from a `graph:` section in a sibling `config.yaml` (if the host app has
one), e.g.:

```yaml
graph:
  cosmos_gremlin_database: aria-graphdb
  cosmos_gremlin_graph: aria-graph
```

Falls back to `aria-graphdb` / `aria-graph` if no config.yaml is found
(e.g. when this package is copied into a project with no YAML config at all).
