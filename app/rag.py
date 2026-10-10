"""RAG mínimo: BM25 sobre los documentos markdown de docs/.

Cada sección '## ...' de cada documento es un fragmento (chunk)
"""
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

STOPWORDS = set("""a al algo como con de del el en es la las lo los me mi mis o para por que qué se
sus su un una y ya hay cual cuál cuales cuáles cuando cuándo donde dónde tienen tiene son ser""".split())


@dataclass
class Chunk:
    source: str
    title: str
    text: str


def _normalize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))     # quita tildes
    return [t for t in re.findall(r"[a-z0-9]+", text) if t not in STOPWORDS]


def load_chunks(docs_dir: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(docs_dir).glob("*.md")):
        current_title, lines = path.stem, []
        for line in path.read_text(encoding="utf-8").splitlines() + ["## __fin__"]:
            if line.startswith("## "):
                if any(l.strip() for l in lines):
                    chunks.append(Chunk(path.name, current_title, "\n".join(lines).strip()))
                current_title, lines = line[3:].strip(), []
            elif not line.startswith("# "):
                lines.append(line)
    return chunks


class BusinessKB:
    def __init__(self, docs_dir: str):
        self.chunks = load_chunks(docs_dir)
        if not self.chunks:
            raise RuntimeError(f"No hay documentos en {docs_dir}")
        corpus = [_normalize(f"{c.title} {c.title} {c.text}") for c in self.chunks]   # el título pesa doble
        self.bm25 = BM25Okapi(corpus)

    def search(self, query: str, k: int = 3) -> list[Chunk]:
        scores = self.bm25.get_scores(_normalize(query))
        ranked = sorted(zip(scores, self.chunks), key=lambda x: x[0], reverse=True)
        return [chunk for score, chunk in ranked[:k] if score > 0]