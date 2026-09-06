"""Vector store behind the knowledge base: Chroma (default) or Milvus.

``open_collection(config)`` returns an object with the small subset of the
Chroma collection API the knowledge base uses - ``add``, ``upsert``, ``get``,
``query``, ``delete``, ``count``, ``metadata`` and ``modify`` - so
``knowledge_base.py`` is byte-for-byte unchanged in behaviour when
``VECTOR_STORE=chroma`` and needs no edits to run on Milvus.

Embeddings are computed LOCALLY in both cases (Chroma's bundled ONNX
MiniLM, present in the offline bundle); Milvus stores vectors, it never
embeds. pymilvus is an optional dependency: selecting Milvus without it is
a configuration error at startup, never a silent fallback.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def open_collection(config, *, collection_metadata: Optional[Dict[str, Any]] = None):
    """The knowledge base's collection for the configured store."""
    store = str(getattr(config, "vector_store", "chroma") or "chroma").strip().lower()
    if store in ("", "chroma"):
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(
            path=config.chroma_persist_dir,
            settings=Settings(anonymized_telemetry=False))
        return client.get_or_create_collection(
            name=config.chroma_collection_name, metadata=collection_metadata or {})
    if store == "milvus":
        return MilvusCollection(
            uri=str(getattr(config, "milvus_uri", "") or ""),
            name=config.chroma_collection_name,
            metadata=collection_metadata or {})
    raise ValueError(f"VECTOR_STORE={store!r} is not supported (chroma | milvus)")


def local_embedding_function():
    """Chroma's default embedding function: a local ONNX MiniLM. Used by
    both stores so the vectors are comparable and nothing leaves the host."""
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    return DefaultEmbeddingFunction()


class MilvusCollection:
    """A Milvus collection wearing the Chroma collection interface.

    Documents are the questions; metadata is stored as one JSON field so the
    knowledge base's ``where`` filters (equality and ``$and``/``$in``) keep
    working. Collection-level metadata (the seed hash) lives in a reserved
    row so the re-seed logic is unchanged.
    """

    _META_ID = "__collection_metadata__"
    _DIM = 384          # all-MiniLM-L6-v2

    def __init__(self, uri: str, name: str, metadata: Optional[Dict[str, Any]] = None):
        try:
            from pymilvus import MilvusClient
        except ImportError as e:      # pragma: no cover - optional dependency
            raise RuntimeError(
                "VECTOR_STORE=milvus needs the pymilvus package in the image "
                "(offline bundle: wheels/pymilvus*.whl)") from e
        if not uri.strip():
            raise ValueError("MILVUS_URI is empty while VECTOR_STORE=milvus")
        self._client = MilvusClient(uri=uri)
        self.name = name
        self._embed = local_embedding_function()
        if not self._client.has_collection(name):
            self._client.create_collection(
                collection_name=name, dimension=self._DIM, primary_field_name="id",
                id_type="string", vector_field_name="vector", metric_type="COSINE",
                max_length=64, auto_id=False)
        self._metadata = dict(metadata or {})
        stored = self._client.get(collection_name=name, ids=[self._META_ID])
        if stored:
            try:
                self._metadata.update(json.loads(stored[0].get("meta") or "{}"))
            except Exception:
                pass

    # ----- metadata ---------------------------------------------------------
    @property
    def metadata(self) -> Dict[str, Any]:
        return dict(self._metadata)

    def modify(self, metadata: Optional[Dict[str, Any]] = None, **_: Any) -> None:
        if metadata:
            self._metadata.update(metadata)
        self._client.upsert(collection_name=self.name, data=[{
            "id": self._META_ID, "vector": [0.0] * self._DIM, "document": "",
            "meta": json.dumps(self._metadata, default=str)}])

    # ----- writes ------------------------------------------------------------
    def _rows(self, ids, documents, metadatas):
        vectors = self._embed(list(documents))
        return [{"id": i, "vector": [float(x) for x in v], "document": d,
                 "meta": json.dumps(m or {}, default=str)}
                for i, v, d, m in zip(ids, vectors, documents, metadatas or [{}] * len(ids))]

    def add(self, ids, documents, metadatas=None, **_: Any) -> None:
        self._client.insert(collection_name=self.name, data=self._rows(ids, documents, metadatas))

    def upsert(self, ids, documents, metadatas=None, **_: Any) -> None:
        self._client.upsert(collection_name=self.name, data=self._rows(ids, documents, metadatas))

    def delete(self, ids=None, where=None, **_: Any) -> None:
        if ids:
            self._client.delete(collection_name=self.name, ids=list(ids))
        elif where:
            hits = self.get(where=where)
            if hits["ids"]:
                self._client.delete(collection_name=self.name, ids=hits["ids"])

    # ----- reads -------------------------------------------------------------
    @staticmethod
    def _matches(meta: Dict[str, Any], where: Optional[Dict[str, Any]]) -> bool:
        if not where:
            return True
        for key, expected in where.items():
            if key == "$and":
                if not all(MilvusCollection._matches(meta, clause) for clause in expected):
                    return False
            elif key == "$or":
                if not any(MilvusCollection._matches(meta, clause) for clause in expected):
                    return False
            elif isinstance(expected, dict):
                if "$in" in expected and meta.get(key) not in expected["$in"]:
                    return False
                if "$eq" in expected and meta.get(key) != expected["$eq"]:
                    return False
                if "$ne" in expected and meta.get(key) == expected["$ne"]:
                    return False
            elif meta.get(key) != expected:
                return False
        return True

    def _all(self, limit: int = 16384) -> List[Dict[str, Any]]:
        rows = self._client.query(collection_name=self.name, filter='id != "%s"' % self._META_ID,
                                  output_fields=["id", "document", "meta"], limit=limit)
        for row in rows:
            try:
                row["meta"] = json.loads(row.get("meta") or "{}")
            except Exception:
                row["meta"] = {}
        return rows

    def get(self, ids=None, where=None, include=None, limit=None, **_: Any) -> Dict[str, Any]:
        if ids:
            rows = self._client.get(collection_name=self.name, ids=list(ids),
                                    output_fields=["id", "document", "meta"])
            for row in rows:
                try:
                    row["meta"] = json.loads(row.get("meta") or "{}")
                except Exception:
                    row["meta"] = {}
        else:
            rows = self._all()
        rows = [r for r in rows if self._matches(r["meta"], where)]
        if limit:
            rows = rows[:int(limit)]
        return {"ids": [r["id"] for r in rows], "documents": [r["document"] for r in rows],
                "metadatas": [r["meta"] for r in rows]}

    def query(self, query_texts=None, n_results: int = 5, where=None, include=None, **_: Any):
        vectors = self._embed(list(query_texts or []))
        out = {"ids": [], "documents": [], "metadatas": [], "distances": []}
        for vector in vectors:
            hits = self._client.search(collection_name=self.name, data=[[float(x) for x in vector]],
                                       limit=max(int(n_results) * 4, int(n_results)),
                                       output_fields=["id", "document", "meta"],
                                       filter='id != "%s"' % self._META_ID)[0]
            kept = []
            for hit in hits:
                entity = hit.get("entity", hit)
                try:
                    meta = json.loads(entity.get("meta") or "{}")
                except Exception:
                    meta = {}
                if self._matches(meta, where):
                    # cosine similarity -> the L2-style distance the caller expects
                    kept.append((entity.get("id"), entity.get("document"), meta,
                                 max(0.0, 1.0 - float(hit.get("distance", 0.0)))))
                if len(kept) >= int(n_results):
                    break
            out["ids"].append([k[0] for k in kept])
            out["documents"].append([k[1] for k in kept])
            out["metadatas"].append([k[2] for k in kept])
            out["distances"].append([k[3] for k in kept])
        return out

    def count(self) -> int:
        return len(self._all())
