"""Build a knowledge base from local documents.

    uv run python src/ingest.py <name> <file-or-folder> [...]

Extracts text from PDF / DOCX / TXT / MD, then writes:
    knowledge/<name>.json        the combined text + source list
    knowledge/<name>.index.npz   the vector index the agent retrieves from

Pass `<name>` as `knowledgeId` in the dispatch metadata to use it.

This replaces the browser app's /api/knowledge route, which did the same job in
Node (unpdf + mammoth). Doing it in Python keeps the whole knowledge pipeline in
one runtime and one dependency set.
"""

from __future__ import annotations

import json
import pathlib
import sys

import rag

KNOWLEDGE_DIR = pathlib.Path(__file__).resolve().parents[1] / "knowledge"
SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".markdown"}


def _extract(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)

    if suffix == ".docx":
        import docx

        return "\n\n".join(p.text for p in docx.Document(str(path)).paragraphs)

    return path.read_text(encoding="utf-8", errors="replace")


def _collect(targets: list[str]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for target in targets:
        p = pathlib.Path(target)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED))
        elif p.is_file():
            if p.suffix.lower() not in SUPPORTED:
                print(f"  skipping unsupported file: {p.name}")
                continue
            files.append(p)
        else:
            raise SystemExit(f"not found: {target}")
    return files


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: ingest.py <name> <file-or-folder> [...]")

    name = sys.argv[1]
    files = _collect(sys.argv[2:])
    if not files:
        raise SystemExit(f"no ingestable documents found (supported: {', '.join(sorted(SUPPORTED))})")

    parts: list[str] = []
    sources: list[dict] = []
    for f in files:
        text = _extract(f).strip()
        if not text:
            print(f"  no text extracted from {f.name} (scanned image? needs OCR)")
            continue
        print(f"  {f.name}: {len(text):,} chars")
        parts.append(f"# {f.stem}\n\n{text}")
        sources.append({"name": f.name, "chars": len(text)})

    combined = "\n\n".join(parts).strip()
    if not combined:
        raise SystemExit("no text extracted from any document")

    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    index = rag.build_index(combined)
    rag.save_index(str(KNOWLEDGE_DIR / f"{name}.index.npz"), index)
    (KNOWLEDGE_DIR / f"{name}.json").write_text(
        json.dumps(
            {"id": name, "text": combined, "docs": sources, "chars": len(combined),
             "chunks": len(index["chunks"])},
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"\nknowledge base '{name}': {len(sources)} doc(s), "
        f"{len(combined):,} chars, {len(index['chunks'])} chunks"
    )
    print(f"use it with:  --knowledge {name}")


if __name__ == "__main__":
    main()
