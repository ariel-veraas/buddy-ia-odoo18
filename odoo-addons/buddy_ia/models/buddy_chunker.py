"""Splits a document's text into chunks that fit a prompt. Pure Python: no Odoo imports, so it is easy to test."""
import re
from itertools import islice

CHUNK_SIZE = 1000
OVERLAP = 150
MAX_CHUNKS_PER_DOCUMENT = 400


def _pieces(text, size):
    """Paragraphs, with the ones longer than `size` cut on a space when there is one.

    Cuts walk an index instead of re-slicing the remainder, so one huge paragraph costs linear time.
    """
    for paragraph in text.replace("\r", "").split("\n\n"):
        paragraph = " ".join(paragraph.split())
        start, length = 0, len(paragraph)
        while length - start > size:
            cut = paragraph.rfind(" ", start + size // 2, start + size)
            cut = cut if cut > start else start + size
            piece = paragraph[start:cut].strip()
            if piece:
                yield piece
            start = cut
            while start < length and paragraph[start] == " ":
                start += 1
        rest = paragraph[start:].strip()
        if rest:
            yield rest


def _overlap_tail(chunk, overlap):
    """The last `overlap` characters of a chunk, starting on a word boundary."""
    tail = chunk[-overlap:] if overlap else ""
    if " " in tail and chunk[-overlap - 1:-overlap] not in ("", " "):
        tail = tail.split(" ", 1)[1]  # the first word is cut in half: drop it
    return tail.strip()


def _iter_chunks(text, size, overlap):
    current = ""
    for piece in _pieces(text, size):
        if current and len(current) + 1 + len(piece) > size:
            yield current
            candidate = (_overlap_tail(current, overlap) + " " + piece).strip()
            current = candidate if len(candidate) <= size else piece
        else:
            current = (current + " " + piece) if current else piece
    if current:
        yield current


def split_text_ex(text, size=CHUNK_SIZE, overlap=OVERLAP, limit=MAX_CHUNKS_PER_DOCUMENT):
    """`(chunks, truncated)`: the chunks of `split_text` and whether the text had more than `limit` of them."""
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size or overlap.")
    chunks = list(islice(_iter_chunks(text, size, overlap), limit + 1))
    return chunks[:limit], len(chunks) > limit


def split_text(text, size=CHUNK_SIZE, overlap=OVERLAP, limit=MAX_CHUNKS_PER_DOCUMENT):
    """Paragraph-aware chunks of at most `size` characters; each one repeats the end of the previous one.

    Never returns empty chunks and never more than `limit` chunks. Stops working as soon as it has them.
    """
    return split_text_ex(text, size, overlap, limit)[0]


# --- table of contents ----------------------------------------------------------------------------------------------
# A table of contents is noise for search: every title matches the question, none of them answers it. It is removed
# from the text before chunking, and only when it is unmistakable:
#   * a heading ("Índice", "Contenido", "Tabla de contenidos") in the first lines followed by at least 3 entries that
#     end in a page number, with page numbers that never go back (a product list with prices does not qualify), or
#   * a run of at least 4 lines with dot leaders ("Objetivo ........ 3").
# Anything else, including numbered lists and tables of numbers, is kept.
#
# Documents are untrusted input: every pattern runs only on short lines (MAX_TOC_LINE) and none of them can backtrack
# more than linearly on such a line, so a hostile line ("....." repeated 200k times) cannot stall a worker.
_FOLD = str.maketrans("áéíóúü", "aeiouu")
_TOC_HEADINGS = {"indice", "indice de contenido", "indice de contenidos", "tabla de contenido", "tabla de contenidos",
                 "contenido", "contenidos", "table of contents", "contents"}
_TOC_ENTRY = re.compile(r"\s*(?P<title>\S.{0,100}?)(?:\s*\.{2,}\s*|\s+)(?P<page>\d{1,4})\s*\Z")
# Dot leaders, matched on the REVERSED line (page number first, then the dots) so there is a single attempt from
# the start: "\S.*(?:\.\s?){3,}\s*\d{1,4}" tried at every position backtracks quadratically on a line of dots.
_TOC_DOTTED_REVERSED = re.compile(r"\d{1,4}\s*(?:\s?\.){3,}(?=.)", re.DOTALL)
_FLAT_HEADING = re.compile(r"\s*(?:índice|indice|tabla de contenidos?|contenidos?)\b[:.]?\s*", re.IGNORECASE)
_FLAT_ENTRY = re.compile(r"\s*(?:\d{1,2}[.)]\s*)?[^\W\d_][^\d.,;:]{0,80}?\s+(?P<page>\d{1,4})(?=\s|\Z)")
MIN_TOC_ENTRIES = 3
HEADING_WINDOW = 40  # lines
MAX_TOC_LINE = 200  # characters: a longer line is prose, never a table-of-contents entry or heading
MAX_FLAT_ENTRIES = 300  # a flattened table of contents with more entries than this is not one we try to peel


def _monotonic(pages):
    """Page numbers of a real index only go forward (equal numbers are fine; a stray one is tolerated)."""
    backwards = sum(1 for before, after in zip(pages, pages[1:]) if after < before)
    return backwards <= len(pages) // 8


def _is_entry(line):
    match = _TOC_ENTRY.fullmatch(line.rstrip()) if len(line) <= 120 else None
    return int(match.group("page")) if match else None


def _is_dotted(line):
    """A line of dot leaders ("Objetivo ........ 3"): at least three dots (optionally spaced) before a page number."""
    stripped = line.rstrip()
    if not stripped or len(stripped) > MAX_TOC_LINE or stripped[0].isspace() or not stripped[-1].isdecimal():
        return False
    return _TOC_DOTTED_REVERSED.match(stripped[::-1]) is not None


def _strip_flat(text):
    """Same idea for text with no line breaks (already flattened chunks): only a heading at the very start counts."""
    heading = _FLAT_HEADING.match(text)
    if not heading:
        return text
    position, pages = heading.end(), []
    while True:
        entry = _FLAT_ENTRY.match(text, position)
        if not entry:
            break
        pages.append(int(entry.group("page")))
        position = entry.end()
        if len(pages) >= MAX_FLAT_ENTRIES:
            break
    if len(pages) < MIN_TOC_ENTRIES + 1 or not _monotonic(pages):
        return text
    return text[position:].lstrip()


def strip_toc(text):
    """The text without its table of contents (unchanged when there is none)."""
    if not text:
        return text
    lines = text.replace("\r", "").split("\n")
    if len([line for line in lines if line.strip()]) <= 3:
        return _strip_flat(text.strip())
    kept, index, drop = [], 0, set()
    for number, line in enumerate(lines[:HEADING_WINDOW]):
        if number in drop or len(line) > MAX_TOC_LINE                 or " ".join(line.split()).lower().translate(_FOLD).rstrip(":.") not in _TOC_HEADINGS:
            continue
        end, pages = number + 1, []
        while end < len(lines):
            if not lines[end].strip():
                end += 1
                continue
            page = _is_entry(lines[end])
            if page is None:
                break
            pages.append(page)
            end += 1
        if len(pages) >= MIN_TOC_ENTRIES and _monotonic(pages):
            drop.update(range(number, end))
    # Dot leaders are unmistakable by themselves, wherever they are.
    run = []
    for number, line in enumerate(lines + [""]):
        if number < len(lines) and _is_dotted(line):
            run.append(number)
            continue
        if len(run) >= 4:
            drop.update(run)
        run = []
    while index < len(lines):
        if index not in drop:
            kept.append(lines[index])
        index += 1
    return "\n".join(kept).strip() if drop else text
