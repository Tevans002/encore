# Neo4j Guide for Encore (Graph Store Bonus)

The graph store bonus asks you to load artist membership and collaboration
relationships from MusicBrainz into Neo4j and expose them through a
`query_artist_graph` tool. This guide covers the concepts, the Python
driver, Cypher, loading MusicBrainz data, and designing the tool.

- **Running the server:** `NEO4J_SETUP.md` (Docker or local install)
- **Runnable example:** `ch07_04_neo4j_simple.py`
- **Official docs:** Cypher manual https://neo4j.com/docs/cypher-manual/current/ ·
  Python driver https://neo4j.com/docs/python-manual/current/

---

## 1. When a graph beats a vector store

| Question | Store | Why |
| --- | --- | --- |
| "What did we do last week?" | Chroma (episodic) | Similarity to past summaries |
| "How do I connect Spotify?" | Chroma (semantic) | Similarity to doc chunks |
| "Who's in this band?" | Neo4j | A direct relationship |
| "Who has this drummer played with?" | Neo4j | Two hops: person → band → other members |
| "How is artist A connected to artist B?" | Neo4j | A path search; embeddings can't do this |

The cost: another server to run, a data-loading job, and another place
user data could leak (keep the graph to catalog facts, not user history).
Your write-up has to justify that cost.

## 2. The data model

A **property graph** has **nodes** (with labels and properties) and
**relationships** (with a type, a direction, and properties).

```
(:Artist {mbid, name, type})-[:MEMBER_OF {instruments, begin, end}]->(:Artist {type:"Group"})
(:Artist)-[:COLLABORATED_WITH]->(:Artist)
(:Recording {mbid, title})-[:BY]->(:Artist)        # optional
```

Design rules:

- **Key every node by its MBID**, not its name. Names collide (several
  artists share a name); MBIDs don't.
- Keep relationship types few and verb-like (`MEMBER_OF`, not
  `IS_A_MEMBER_OF_BAND_AS`).
- Store only what your queries need. You can always load more later.

## 3. Python driver basics

```bash
pip install neo4j
```

```python
import os
from neo4j import GraphDatabase

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "password")),
)
driver.verify_connectivity()          # fails fast if the server isn't up

records, summary, keys = driver.execute_query(
    "MATCH (a:Artist {mbid: $mbid}) RETURN a.name AS name",
    mbid=artist_mbid,
)
for r in records:
    print(r["name"])

driver.close()                        # once, at shutdown
```

- **One driver per program.** Create it at startup and close it at exit.
  It manages a pool of connections for you.
- `execute_query` returns `(records, summary, keys)`. Each record behaves
  like a dict keyed by the names after `AS`.
- The URI scheme is `bolt://` (single server) or `neo4j://` (routing).
  `http://` is for the Browser UI on port 7474 and won't work here.
- `summary.counters` tells you what a write changed, e.g.
  `summary.counters.nodes_created`. Useful in traces.

### Always use parameters

```python
# ✅ parameter: safe, and Neo4j can cache the query plan
driver.execute_query("MATCH (a:Artist {name: $name}) RETURN a", name=user_text)

# ❌ never build Cypher from strings: a quote in a band name breaks it,
#    and model-produced text could inject Cypher
driver.execute_query(f"MATCH (a:Artist {{name: '{user_text}'}}) RETURN a")
```

Labels and relationship types can't be parameters. If you need a variable
one, choose it from a fixed allowlist in Python.

## 4. Cypher in ten minutes

```cypher
// Create or reuse (MERGE = match, or create if missing). Idempotent.
MERGE (b:Artist {mbid: $band_mbid})
  ON CREATE SET b.name = $band_name, b.type = "Group"

// Relationship between two existing nodes
MATCH (p:Artist {mbid: $person}), (b:Artist {mbid: $band})
MERGE (p)-[r:MEMBER_OF]->(b)
SET r.instruments = $instruments

// Read: members of a band
MATCH (p:Artist)-[r:MEMBER_OF]->(b:Artist {mbid: $mbid})
RETURN p.name AS member, r.instruments AS instruments
ORDER BY member

// Two hops: who has this person played with (through any band)?
MATCH (me:Artist {mbid: $mbid})-[:MEMBER_OF]->(b)<-[:MEMBER_OF]-(other)
WHERE other <> me
RETURN DISTINCT other.name AS name, collect(DISTINCT b.name) AS bands
LIMIT 25

// Shortest connection between two artists, at most 4 hops, any direction
MATCH p = shortestPath((a:Artist {mbid: $a})-[:MEMBER_OF|COLLABORATED_WITH*..4]-(b:Artist {mbid: $b}))
RETURN [n IN nodes(p) | n.name] AS chain

// Clean up
MATCH (n:Artist) DETACH DELETE n          // DETACH removes relationships too
```

Things that trip people up:

- `MERGE` matches the **whole pattern**. `MERGE (a)-[:X]->(b)` where `a`
  and `b` don't exist creates both. MERGE nodes first, then the relationship.
- Patterns with no direction arrow (`-[:X]-`) match both directions. Use
  that for symmetric facts like collaboration.
- Variable-length paths (`*..4`) can explode on a big graph. Always set an
  upper bound and a `LIMIT`.
- `RETURN` names (`AS member`) are how you read values in Python.

## 5. Constraints and indexes (do this first)

```cypher
CREATE CONSTRAINT artist_mbid IF NOT EXISTS
FOR (a:Artist) REQUIRE a.mbid IS UNIQUE
```

A uniqueness constraint also creates an index, so `MERGE` and `MATCH` by
MBID stay fast and duplicates become impossible. Run it once at startup;
`IF NOT EXISTS` makes it safe to repeat.

## 6. Loading from MusicBrainz

The artist lookup with relationships is the same endpoint your
`lookup_artist` tool uses (see `tests/live/test_live_endpoints.py`):

```
GET https://musicbrainz.org/ws/2/artist/{mbid}?inc=artist-rels&fmt=json
```

The response has the artist's `id`, `name` and `type` (`"Person"`,
`"Group"`, …) and a `relations` list. Each relation has a `type` (for
example `"member of band"` or `"collaboration"`), a `direction`, the other
`artist` (`id`, `name`), and often `attributes` (instruments, "original")
and `begin`/`end` dates. **Print one real response before writing the
loader**: which side is the band depends on `direction` and on whether you
looked up the person or the group.

```python
def load_artist(driver, mbid):
    status, body, _ = http("GET", f"{MB_API}/artist/{mbid}", params={"inc": "artist-rels"})
    if status != 200:
        return fail(classify_status(status), f"MusicBrainz lookup failed (HTTP {status}).")

    driver.execute_query(
        "MERGE (a:Artist {mbid: $id}) SET a.name = $name, a.type = $type",
        id=body["id"], name=body["name"], type=body.get("type"),
    )
    for rel in body.get("relations", []):
        other = rel.get("artist")
        if not other:
            continue
        if rel["type"] == "member of band":
            # Looking up a band: members come back with direction "backward".
            person, band = (other, body) if rel["direction"] == "backward" else (body, other)
            driver.execute_query(
                """
                MERGE (p:Artist {mbid: $pid}) ON CREATE SET p.name = $pname
                MERGE (b:Artist {mbid: $bid}) ON CREATE SET b.name = $bname
                MERGE (p)-[r:MEMBER_OF]->(b)
                SET r.instruments = $attrs, r.begin = $begin, r.end = $end
                """,
                pid=person["id"], pname=person["name"],
                bid=band["id"], bname=band["name"],
                attrs=rel.get("attributes", []), begin=rel.get("begin"), end=rel.get("end"),
            )
        elif rel["type"] == "collaboration":
            driver.execute_query(
                """
                MERGE (a:Artist {mbid: $aid})
                MERGE (b:Artist {mbid: $bid}) ON CREATE SET b.name = $bname
                MERGE (a)-[:COLLABORATED_WITH]-(b)
                """,
                aid=body["id"], bid=other["id"], bname=other["name"],
            )
```

Loading rules:

- **Respect the rate limit.** Go through the starter's `http()` helper
  (one request per 1.5 s to MusicBrainz) and cache responses. Load once,
  outside class, and reuse the graph.
- **Bound the crawl.** Start from the artists in your listening history
  (top 20–50) and go one hop out. Following every relation recursively
  never finishes.
- `MERGE` makes the loader safe to re-run.
- `None` is fine for a property in Neo4j (it just isn't stored). Lists
  must hold one type (all strings, say).

## 7. Designing `query_artist_graph`

Don't let the model write Cypher. Offer a small set of **fixed,
parameterized queries** chosen by an enum, the same way your router uses
enums:

```python
GRAPH_QUERIES = {
    "members": """
        MATCH (p:Artist)-[r:MEMBER_OF]->(b:Artist {mbid: $mbid})
        RETURN p.name AS name, p.mbid AS mbid, r.instruments AS instruments
        ORDER BY name LIMIT 25""",
    "bands_of": """
        MATCH (p:Artist {mbid: $mbid})-[:MEMBER_OF]->(b:Artist)
        RETURN b.name AS name, b.mbid AS mbid ORDER BY name LIMIT 25""",
    "played_with": """
        MATCH (me:Artist {mbid: $mbid})-[:MEMBER_OF]->(b)<-[:MEMBER_OF]-(o:Artist)
        WHERE o <> me
        RETURN DISTINCT o.name AS name, o.mbid AS mbid, collect(DISTINCT b.name) AS via
        LIMIT 25""",
}

def query_artist_graph(question_type: str, artist_mbid: str) -> str:
    if question_type not in GRAPH_QUERIES:
        return fail("input", f"question_type must be one of {sorted(GRAPH_QUERIES)}.").to_json()
    if not UUID_RE.match(artist_mbid):
        return fail("input", "artist_mbid must be a MusicBrainz MBID; call search_recordings or lookup_artist first.").to_json()
    try:
        records, _, _ = driver.execute_query(GRAPH_QUERIES[question_type], mbid=artist_mbid)
    except ServiceUnavailable:
        return fail("transient", "The graph database is not reachable.").to_json()
    if not records:
        return fail("resource", f"No {question_type} found for {artist_mbid} in the graph; "
                                "it may not be loaded. Try lookup_artist instead.").to_json()
    return ok({"results": [dict(r) for r in records]}).to_json()
```

The tool's description is what makes the agent choose it. State that it
answers **relationship** questions (band members, who played with whom),
when to prefer it over `lookup_artist` (multi-hop questions, or when
MusicBrainz calls should be saved), and a "do not use this when…" sentence
(not for similarity, docs, or past sessions).

Imports and helpers this assumes: `from neo4j.exceptions import
ServiceUnavailable, AuthError` (treat `AuthError` as `fatal`), the starter's
`ok`/`fail`, and an MBID pattern such as
`UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")`.

## 8. Cleanup and privacy

- The graph holds public catalog facts keyed by MBID, so `--forget` doesn't
  need to touch it, **as long as you store no user data there**. Don't
  attach listening history or preferences to graph nodes.
- To reset during development: `MATCH (n:Artist) DETACH DELETE n`, or stop
  the container and delete its data folder.

## 9. Common problems

| Symptom | Fix |
| --- | --- |
| `ServiceUnavailable` | Server not running or still starting (`docker logs -f neo4j`), or wrong port. |
| `AuthError` | Wrong password. `NEO4J_AUTH` only applies when the data folder is empty. |
| `ConfigurationError: URI scheme 'http' is not supported` | Use `bolt://localhost:7687`, not `http://`. |
| Duplicate artists | MERGE by `mbid`, add the uniqueness constraint. |
| Query never returns | Unbounded variable-length path. Add `*..4` and `LIMIT`. |
| Empty result but data looks loaded | Direction is reversed in your pattern; try `-[:MEMBER_OF]-` to check. |
| MusicBrainz 503s while loading | You're over 1 request/second. Slow down, cache, and don't load on campus. |
