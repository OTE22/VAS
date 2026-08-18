"""The legacy FAISS index service is retired — these tests keep it retired.

This file used to test `backend/core/identity_index.py`: a name-keyed, two-index
(KNOWN + UNKNOWN) service that wrote four artifacts side by side, tombstoned
deletions instead of removing vectors, and — under `VECTOR_BACKEND=faiss` — held
the ONLY copy of each embedding, because that write path never populated
`identity_embeddings.embedding`. Its "rebuild from database" reconstructed
vectors out of the in-memory index rather than reading the database.

That module is deleted. PostgreSQL is authoritative, and the index is a
disposable acceleration layer behind the `VectorIndex` contract.

The behaviours the old tests protected are now covered where they belong:

  atomic save, torn writes, versioning  -> tests/test_vector_index_recovery.py
  checksum / ntotal / dim verification  -> tests/test_vector_index_recovery.py
  rebuild-from-database                 -> tests/test_vector_index_recovery.py
                                           tests/test_vector_index_integration.py
  add / search / remove / key stability -> tests/test_vector_index_contract.py
  drift detection and repair            -> tests/test_vector_index_reconcile.py
  save/rebuild lock discipline          -> tests/test_vector_index_concurrency.py

Three of the ten original tests asserted on the SOURCE TEXT of the module
(greps for `os.replace`, for lock usage). Those are not reproduced: the new
suites assert on observable behaviour — kill a save midway and check what
loads — which is what the greps were standing in for.
"""

import importlib
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_legacy_index_module_is_gone():
    """The file itself must not come back."""
    legacy = REPO_ROOT / "backend" / "core" / "identity_index.py"
    assert not legacy.exists(), (
        f"{legacy} was reintroduced. It is retired: PostgreSQL is authoritative "
        "and index implementations live in backend/core/vector_index/.")


def test_the_legacy_module_cannot_be_imported():
    with pytest.raises(ImportError):
        importlib.import_module("backend.core.identity_index")


def test_no_source_file_imports_the_legacy_module():
    """No production or script file may reference it again.

    Scanned rather than mocked: an import added anywhere would resurrect a
    second, unreconciled copy of the vectors, which is the exact failure this
    work removed.
    """
    pattern = re.compile(r"from\s+backend\.core\.identity_index\s+import"
                         r"|import\s+backend\.core\.identity_index"
                         r"|backend\.core\.identity_index\b")
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.parts)
        if parts & {".venv", "venv", "node_modules", "__pycache__", ".git"}:
            continue
        if path.resolve() == pathlib.Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if not pattern.search(line):
                continue
            stripped = line.strip()
            # Prose in a docstring or comment explaining the retirement is fine;
            # an executable reference is not.
            if stripped.startswith("#"):
                continue
            if "identity_index_pgvector" in line:
                continue
            if pattern.search(line) and (
                    "import" in stripped or "identity_index." in stripped):
                if stripped.startswith(("'", '"')) or "`" in line:
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, (
        "Legacy identity_index references reintroduced:\n  " + "\n  ".join(offenders))


def test_the_replacement_contract_is_importable():
    """What replaced it must actually be there."""
    from backend.core.vector_index import (FlatFaissIndex, PgVectorIndex,
                                           VectorIndex, build_index,
                                           select_backend)

    for name in ("add", "add_many", "search", "remove", "contains", "keys",
                 "rebuild_from_db", "save", "load", "stats", "health"):
        assert hasattr(VectorIndex, name), f"contract lost {name}()"
