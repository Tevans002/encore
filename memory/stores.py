"""Vector memory: episodic (per-user) and semantic (shared) ChromaDB collections.

Adapted from memory_examples/ch07_05_chroma_simple.py.
Episodic rows are scoped by user_id; semantic rows carry a source.
"""

import os

import chromadb

# Keep the DB next to this file, independent of cwd.
DB_PATH = os.path.join(os.path.dirname(__file__), "chroma")


def open_client(path: str = DB_PATH) -> chromadb.ClientAPI:
    os.makedirs(path, exist_ok=True)
    return chromadb.PersistentClient(path=path)


def open_collections(client: chromadb.ClientAPI):
    """Return (episodic, semantic) collections, creating them if needed."""
    episodic = client.get_or_create_collection(
        name="episodic", metadata={"hnsw:space": "cosine"}
    )
    semantic = client.get_or_create_collection(
        name="semantic", metadata={"hnsw:space": "cosine"}
    )
    return episodic, semantic