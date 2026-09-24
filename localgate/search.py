"""Hybrid search: BM25 full-text + local vector cosine, weighted fusion.

BM25 scores are unbounded, so they are max-normalized within a result set.
Cosine similarities are already bounded [0, 1] and are used raw - normalizing
them would inflate weak accidental matches to full weight. Results below a
small fused-score floor are dropped (pure-noise matches).
"""

from __future__ import annotations

from typing import Any

# fused scores below this are considered noise and not returned
MIN_FUSED_SCORE = 0.04
# vector-only hits (no full-text evidence) need at least this cosine to count,
# otherwise hashed-feature collisions surface as phantom matches
MIN_VECTOR_ONLY_COSINE = 0.15


def _normalize(scored: list[tuple[str, float]]) -> dict[str, float]:
    if not scored:
        return {}
    top = max(s for _, s in scored)
    if top <= 0:
        return {cid: 0.0 for cid, _ in scored}
    return {cid: s / top for cid, s in scored}


class HybridSearcher:
    def __init__(self, store, embedder, cfg: dict):
        self.store = store
        self.embedder = embedder
        self.cfg = cfg

    def search(self, query: str, top_k: int | None = None,
               fulltext_weight: float | None = None,
               vector_weight: float | None = None) -> dict[str, Any]:
        scfg = self.cfg["search"]
        top_k = int(top_k or scfg["top_k"])
        w_ft = scfg["fulltext_weight"] if fulltext_weight is None else float(fulltext_weight)
        w_vec = scfg["vector_weight"] if vector_weight is None else float(vector_weight)
        top_k = max(1, min(top_k, 100))
        degraded = False
        notes: list[str] = []

        ft = self.store.search_fulltext(query, top_n=max(50, top_k))
        ft_scores = _normalize(ft)

        vec_scores: dict[str, float] = {}
        if w_vec > 0:
            try:
                qvec = self.embedder.embed([query])[0]
                vec = self.store.search_vector(qvec, top_n=max(50, top_k))
                # cosine is bounded already; keep it raw, clamp negatives
                vec_scores = {cid: max(0.0, s) for cid, s in vec}
            except Exception as e:  # embedder down -> degrade to FT only
                degraded = True
                notes.append(f"vector search degraded: {type(e).__name__}")

        candidates: set[str] = set(ft_scores) | set(vec_scores)
        fused: list[tuple[str, float]] = []
        for cid in candidates:
            ft_s = ft_scores.get(cid, 0.0)
            vec_s = vec_scores.get(cid, 0.0)
            if ft_s == 0.0 and vec_s < MIN_VECTOR_ONLY_COSINE:
                continue  # vector-only noise, no textual evidence
            s = w_ft * ft_s + w_vec * vec_s
            if s >= MIN_FUSED_SCORE:
                fused.append((cid, s))
        fused.sort(key=lambda kv: kv[1], reverse=True)
        fused = fused[:top_k]

        score_by_cid = dict(fused)
        results = []
        for chunk, doc in self.store.materialize_results([cid for cid, _ in fused]):
            cid = chunk["chunk_id"]
            text = chunk.get("text", "")
            snippet = text[:240] + ("…" if len(text) > 240 else "")
            results.append({
                "chunk_id": cid,
                "doc_id": chunk.get("doc_id"),
                "path": doc.get("path"),
                "kind": doc.get("kind"),
                "ext": doc.get("ext"),
                "ordinal": chunk.get("ordinal"),
                "score": round(score_by_cid.get(cid, 0.0), 4),
                "fulltext_score": round(ft_scores.get(cid, 0.0), 4),
                "vector_score": round(vec_scores.get(cid, 0.0), 4),
                "snippet": snippet,
            })

        return {
            "query": query,
            "top_k": top_k,
            "results": results,
            "degraded": degraded,
            "notes": notes,
        }
