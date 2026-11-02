r"""
Chapter 7 — Memory — Example 4
The Agentic AI Bible (Revised & Expanded Edition 2026)

A straight-shot Neo4j example: graph memory for
relationship questions.

    "Who is in this band?"
    "Who has this drummer played with?"

Those are relationship questions, not similarity
questions, so a graph answers them better than a
vector store.

This script:
    1. Connects to Neo4j
    2. Clears the demo graph
    3. Creates a few artists and bands (MERGE)
    4. Runs three Cypher queries
    5. Closes the connection

No LLM, no agent loop. Just the database.
(The artists below are made up for the demo.)

Setup:

    pip install neo4j

    Start a Neo4j server first. See NEO4J_SETUP.md for
    Docker and local-install instructions. Quickest:

    macOS / Linux:
    mkdir -p "$HOME/neo4j/data"
    docker run -d --name neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/password -v "$HOME/neo4j/data:/data" neo4j:2026.07.1

    Windows (PowerShell):
    mkdir $HOME\neo4j\data -Force
    docker run -d --name neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/password -v "$HOME\neo4j\data:/data" neo4j:2026.07.1

Environment (optional):

    NEO4J_URI=bolt://localhost:7687
    NEO4J_USERNAME=neo4j
    NEO4J_PASSWORD=password

Run:

    python ch07_04_neo4j_simple.py
"""

import os

from neo4j import GraphDatabase


URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
PASSWORD = os.getenv("NEO4J_PASSWORD", "password")


# Every node this demo creates gets this label, so we
# can delete just the demo data without touching
# anything else in your database.
DEMO = "Ch07Demo"


#
# 1. Connect
#

driver = GraphDatabase.driver(URI, auth=(USERNAME, PASSWORD))
driver.verify_connectivity()
print(f"Connected to {URI}\n")


#
# 2. Start clean
#

driver.execute_query(f"MATCH (n:{DEMO}) DETACH DELETE n")


#
# 3. Build a small artist graph
#
#   (Person)-[:MEMBER_OF]->(Band)
#   (Person)-[:PLAYED_ON]->(Recording)<-[:BY]-(Band)
#
# MERGE = "create if it doesn't exist", so running
# this twice does not make duplicates.
#

members = [
    # (person, instrument, band)
    ("Ana Ruiz",   "vocals", "Static Bloom"),
    ("Ben Okafor", "guitar", "Static Bloom"),
    ("Cal Moreno", "drums",  "Static Bloom"),
    ("Dee Park",   "vocals", "Low Orbit"),
    ("Cal Moreno", "drums",  "Low Orbit"),   # Cal is in two bands
    ("Eli Novak",  "bass",   "Low Orbit"),
]

for person, instrument, band in members:
    driver.execute_query(
        f"""
        MERGE (p:Person:{DEMO} {{name: $person}})
        MERGE (b:Band:{DEMO}   {{name: $band}})
        MERGE (p)-[:MEMBER_OF {{instrument: $instrument}}]->(b)
        """,
        person=person,
        band=band,
        instrument=instrument,
    )

# A guest appearance: Ana sings on a Low Orbit song.
driver.execute_query(
    f"""
    MATCH (ana:Person {{name: "Ana Ruiz"}}),
          (lo:Band    {{name: "Low Orbit"}})
    MERGE (r:Recording:{DEMO} {{title: "Night Bus"}})
    MERGE (lo)-[:BY]->(r)
    MERGE (ana)-[:PLAYED_ON {{role: "guest vocals"}}]->(r)
    """
)

print("Graph loaded.\n")


#
# 4. Query the graph
#

# Q1: Who is in a band?
records, _, _ = driver.execute_query(
    """
    MATCH (p:Person)-[m:MEMBER_OF]->(b:Band {name: $band})
    RETURN p.name AS name, m.instrument AS instrument
    ORDER BY name
    """,
    band="Static Bloom",
)

print("Q1. Members of Static Bloom")
for r in records:
    print(f"    {r['name']:<12} {r['instrument']}")


# Q2: Who has this person played with?
# Walk from the person to each band, then back out
# to the other members of that band.
records, _, _ = driver.execute_query(
    """
    MATCH (me:Person {name: $name})-[:MEMBER_OF]->(b:Band)
          <-[:MEMBER_OF]-(other:Person)
    WHERE other <> me
    RETURN other.name AS name, b.name AS band
    ORDER BY band, name
    """,
    name="Cal Moreno",
)

print("\nQ2. Cal Moreno has played with")
for r in records:
    print(f"    {r['name']:<12} (in {r['band']})")


# Q3: A two-hop question a vector store can't answer:
# which artists outside a band appear on its songs?
records, _, _ = driver.execute_query(
    """
    MATCH (b:Band {name: $band})-[:BY]->(rec:Recording)
          <-[p:PLAYED_ON]-(guest:Person)
    WHERE NOT (guest)-[:MEMBER_OF]->(b)
    RETURN guest.name AS guest, p.role AS role, rec.title AS song
    """,
    band="Low Orbit",
)

print("\nQ3. Guests on Low Orbit recordings")
for r in records:
    print(f"    {r['guest']} — {r['role']} on \"{r['song']}\"")


#
# 5. Clean up
#

driver.close()
