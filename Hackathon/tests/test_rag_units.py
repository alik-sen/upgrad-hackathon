from unreliable.rag.bm25 import BM25Index, tokenize
from unreliable.rag.chunker import chunk_notes, load_vault
from unreliable.rag.fusion import rrf
from unreliable.config import settings


def test_rrf_scale_free_and_dedup():
    fused = rrf([[1, 2, 3], [3, 1, 4]], k=60, hashes=["a", "b", "c", "d", "b2"], pool=3)
    assert [i for i, _ in fused][0] in (1, 3) and len(fused) == 3
    d = rrf([[0, 1], [1, 0]], k=60, hashes=["same", "same"], pool=5)
    assert len(d) == 1          # dedup by chunk_hash


def test_rrf_formula():
    s = dict(rrf([[7], [7]], k=60, pool=1))
    assert abs(s[7] - 2 / 61) < 1e-12


def test_tokenize_keeps_ids_and_stems():
    t = tokenize("What is the storage of SKU-017 suppliers?")
    assert "sku-017" in t and "17" in t and "supplier" in t and "the" not in t


def test_bm25_ranks_exact_id_first():
    notes = load_vault(settings.vault_dir)
    ch = chunk_notes(notes)
    idx = BM25Index([f"{c.title}. {c.text}" for c in ch])
    sc = idx.scores("SKU-017 storage temperature")
    assert ch[int(sc.argmax())].note_id == "sku-017"


def test_chunker_respects_size_and_tenant():
    notes = load_vault(settings.vault_dir)
    ch = chunk_notes(notes, 512, 64)
    assert all(len(c.text.split()) <= 512 + 64 for c in ch)
    assert any(c.tenant == "tenant_a" for c in ch) and any(c.tenant == "redteam" for c in ch)
