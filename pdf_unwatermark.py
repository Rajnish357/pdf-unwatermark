#!/usr/bin/env python3
"""
Split a PDF, remove watermarks, then merge it back. Uses pypdf only.

Pipeline
    1. Split the input into split-pages/<pdf name>/page_1.pdf, page_2.pdf, ...
       Each PDF gets its own subfolder. If that subfolder already has
       page_*.pdf, those files are reused and the PDF is not split again.
       Pass --resplit to split anyway.
    2. Remove watermarks into watermark-removed/<pdf name>/ (folder is kept).
    3. Merge those pages, in numeric order, into <pdf name>_merged.pdf.

With no file argument, the script lists every PDF next to itself and
asks you to pick one by number.

Watermarks are detected automatically. You do not name them.
    - The same stamp text on most pages (phrases, ALL-CAPS stamps, tiled text)
    - Text drawn at a diagonal
    - Faded text and faded images (opacity / ExtGState)
    - Content marked as a watermark, including optional-content watermarks
    - Watermark annotations

A logo painted as a normal opaque image is not removed.
Running headers that are the same phrase on every page may be removed too;
the script prints every repeated string it decided was a watermark.

Install
    pip install pypdf

Usage
    python pdf_split_unwatermark_merge.py
    python pdf_split_unwatermark_merge.py "polity 1.pdf"
    python pdf_split_unwatermark_merge.py "polity 1.pdf" --resplit
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        ContentStream,
        DictionaryObject,
        NameObject,
        TextStringObject,
    )
except ImportError:
    sys.exit("pypdf is not installed. Run:  pip install pypdf")


PAGE_RE = re.compile(r"^page_(\d+)\.pdf$", re.IGNORECASE)
# Text more than this many degrees off horizontal/vertical is a diagonal stamp.
DIAGONAL_DEGREES = 15.0
OPAQUE = 0.95


def page_sort_key(path: Path) -> int:
    """Sort page_2.pdf before page_10.pdf."""
    match = PAGE_RE.match(path.name)
    if match:
        return int(match.group(1))
    return 0


def open_reader(path: Path) -> PdfReader:
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        if reader.decrypt("") == 0:
            raise SystemExit(f"PDF is encrypted and could not be opened: {path}")
    return reader


def operand_text(operand: object) -> str | None:
    """Decoded text for a Tj/TJ operand, or None if it is not text."""
    if isinstance(operand, ByteStringObject):
        raw = bytes(operand)
        for encoding in ("utf-8", "utf-16-be", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return None
    if isinstance(operand, str):
        return operand
    return None


def normalize(text: str) -> str:
    return " ".join(text.split())


def as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _off_axis(x: float, y: float) -> bool:
    if abs(x) < 1e-4 and abs(y) < 1e-4:
        return False
    angle = abs(math.degrees(math.atan2(y, x))) % 180.0
    distance = min(angle, abs(angle - 90.0), abs(angle - 180.0))
    return distance > DIAGONAL_DEGREES


def is_diagonal(a: float, b: float, c: float, d: float) -> bool:
    """True for a rotated (not just italic-skewed or 90-degree) matrix."""
    return _off_axis(a, b) or _off_axis(c, d)


def matmul(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    """Multiply two PDF matrices stored as (a, b, c, d, e, f)."""
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


class DrawState:
    def __init__(self) -> None:
        self.ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        self.alpha_transparent = False
        self.text_rotated = False

    def rotated(self) -> bool:
        a, b, c, d, _e, _f = self.ctm
        return self.text_rotated or is_diagonal(a, b, c, d)


def _resource_dict(page_or_form, key: str):
    resources = page_or_form.get("/Resources") if hasattr(page_or_form, "get") else None
    if resources is None:
        return None
    resources = resources.get_object()
    value = resources.get(key)
    if value is None:
        return None
    return value.get_object()


def load_ext_states(container) -> dict[str, bool | None]:
    """
    Map a graphics-state name to an alpha change.

    True  -> this `gs` makes painting transparent (watermark fade)
    False -> this `gs` forces opaque painting
    None  -> this `gs` does not change alpha
    """
    result: dict[str, bool | None] = {}
    ext = _resource_dict(container, "/ExtGState")
    if ext is None:
        return result
    for key, value in ext.items():
        obj = value.get_object()
        change: bool | None = None
        for field in ("/ca", "/CA"):
            if field not in obj:
                continue
            try:
                faded = float(obj[field]) < OPAQUE
            except (TypeError, ValueError):
                continue
            change = True if faded else change
            if faded:
                break
            change = False
        for alias in (str(key), str(key).lstrip("/")):
            result[alias] = change
    return result


def load_property_labels(container) -> dict[str, str]:
    """Map a marked-content property name to its OCG / dict label."""
    result: dict[str, str] = {}
    props = _resource_dict(container, "/Properties")
    if props is None:
        return result
    for key, value in props.items():
        obj = value.get_object()
        parts = [
            str(obj.get("/Name", "")),
            str(obj.get("/Type", "")),
            str(obj.get("/Subtype", "")),
        ]
        label = " ".join(parts)
        for alias in (str(key), str(key).lstrip("/")):
            result[alias] = label
    return result


def _watermark_in_dict(obj) -> bool:
    getter = getattr(obj, "items", None)
    if getter is None:
        return "watermark" in str(obj).lower()
    try:
        pairs = list(getter())
    except Exception:
        return "watermark" in str(obj).lower()
    for key, value in pairs:
        if "watermark" in str(key).lower() or "watermark" in str(value).lower():
            return True
    return False


def is_watermark_mark(tag, props, properties: dict[str, str]) -> bool:
    if "watermark" in str(tag).lower():
        return True
    if props is None:
        return False
    if isinstance(props, (NameObject, str)) or str(props).startswith("/"):
        label = properties.get(str(props), properties.get(str(props).lstrip("/"), ""))
        return "watermark" in label.lower()
    resolved = props.get_object() if hasattr(props, "get_object") else props
    return _watermark_in_dict(resolved)


def pdf_folder_name(pdf_path: Path) -> str:
    """Subfolder name taken from the PDF file name, without the extension."""
    name = pdf_path.stem.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    return name or "document"


def list_source_pdfs(directory: Path) -> list[Path]:
    """PDFs sitting next to the script. Merged outputs are skipped."""
    found: list[Path] = []
    for path in sorted(directory.glob("*.pdf"), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        lowered = path.name.lower()
        if lowered.endswith("_merged.pdf") or lowered == "final_merged_document.pdf":
            continue
        found.append(path)
    return found


def prompt_pdf(pdfs: list[Path]) -> Path:
    print(f"PDFs in {pdfs[0].parent}:")
    for index, path in enumerate(pdfs, start=1):
        print(f"  {index}) {path.name}")
    while True:
        raw = input(f"Choose a PDF (1-{len(pdfs)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(pdfs):
            return pdfs[int(raw) - 1]
        print(f"Enter a number from 1 to {len(pdfs)}.")


def resolve_user_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    cwd_path = (Path.cwd() / path).resolve()
    if cwd_path.is_file():
        return cwd_path
    script_path = (SCRIPT_DIR / path).resolve()
    if script_path.is_file():
        return script_path
    return cwd_path


def under_script(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return path.resolve()


def existing_split_pages(split_dir: Path) -> list[Path]:
    if not split_dir.is_dir():
        return []
    pages = [path for path in split_dir.glob("page_*.pdf") if page_sort_key(path) > 0]
    return sorted(pages, key=page_sort_key)


def clear_page_pdfs(directory: Path) -> None:
    """Remove old page_N.pdf files inside a folder. The folder itself stays."""
    if not directory.is_dir():
        return
    for path in directory.glob("page_*.pdf"):
        if PAGE_RE.match(path.name):
            path.unlink()


def split_pdf(source: Path, split_dir: Path) -> list[Path]:
    split_dir.mkdir(parents=True, exist_ok=True)
    clear_page_pdfs(split_dir)
    reader = open_reader(source)
    if len(reader.pages) == 0:
        raise SystemExit(f"No pages found in {source}")

    paths: list[Path] = []
    for index, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        output = split_dir / f"page_{index}.pdf"
        with output.open("wb") as handle:
            writer.write(handle)
        paths.append(output)
    return paths


def _walk_xobjects(container, seen: set[int]):
    xobjects = _resource_dict(container, "/XObject")
    if xobjects is None:
        return
    for _name, value in xobjects.items():
        try:
            form = value.get_object()
        except Exception:
            continue
        if form.get("/Subtype") != "/Form":
            continue
        identity = id(form)
        if identity in seen:
            continue
        seen.add(identity)
        yield form


def extract_strings(container, pdf, seen: set[int] | None = None) -> list[str]:
    """Every text-showing string on a page, including text inside form XObjects."""
    if seen is None:
        seen = set()
    found: list[str] = []
    contents = None
    if hasattr(container, "get_contents"):
        try:
            contents = container.get_contents()
        except Exception:
            contents = None
    if contents is None and hasattr(container, "get_data"):
        try:
            contents = ContentStream(container, pdf)
        except Exception:
            contents = None
    if contents is not None:
        for operands, operator in contents.operations:
            found.extend(_strings_from_op(operands, operator))
    for form in _walk_xobjects(container, seen):
        found.extend(extract_strings(form, pdf, seen))
    return found


def _strings_from_op(operands, operator: bytes) -> list[str]:
    if not operands:
        return []
    if operator in (b"Tj", b"'"):
        text = operand_text(operands[0])
        return [text] if text and text.strip() else []
    if operator == b'"' and len(operands) >= 3:
        text = operand_text(operands[2])
        return [text] if text and text.strip() else []
    if operator == b"TJ":
        array = operands[0]
        if not isinstance(array, (list, ArrayObject)):
            return []
        parts = [operand_text(item) or "" for item in array]
        joined = "".join(parts).strip()
        return [joined] if joined else []
    return []


def looks_like_stamp(text: str, page_ratio: float, max_on_one_page: int) -> bool:
    """
    Decide whether a repeated string is watermark text rather than body text.

    Common words that happen to occur on every page ("that", "with") are kept.
    Phrases, ALL-CAPS stamps, and text tiled several times on a page are not.
    """
    if len(text) > 180:
        return False
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 3 or text.isdigit():
        return False
    if max_on_one_page >= 3 and len(text) >= 4:
        return True
    if max_on_one_page >= 2 and len(text) >= 8:
        return True
    if page_ratio < 0.6:
        return False
    words = text.split()
    if len(words) >= 2 and len(text) >= 6:
        return True
    upper = text.upper() == text
    if upper and len(text) >= 4 and page_ratio >= 0.8:
        return True
    if len(text) >= 8 and page_ratio >= 0.8:
        return True
    return False


def detect_watermark_strings(pages: list[list[str]]) -> list[str]:
    """Strings that show up like a stamp across the document."""
    page_count = len(pages)
    if page_count == 0:
        return []

    presence: Counter[str] = Counter()
    max_on_page: Counter[str] = Counter()
    per_page_norms: list[list[str]] = []
    for strings in pages:
        norms = [normalize(text) for text in strings if normalize(text)]
        per_page_norms.append(norms)
        counts = Counter(norms)
        for text, count in counts.items():
            presence[text] += 1
            if count > max_on_page[text]:
                max_on_page[text] = count

    found: list[str] = []
    for text, count in presence.most_common():
        ratio = count / page_count
        if not looks_like_stamp(text, ratio, max_on_page[text]):
            continue
        # Keep a repeated string that is the whole page (stamp-only page),
        # but keep real content that is most of a page which also has other text.
        dominated = 0
        for norms in per_page_norms:
            if text not in norms:
                continue
            total = sum(len(item) for item in norms) or 1
            others = total - len(text)
            if others >= 40 and len(text) / total > 0.5:
                dominated += 1
        if dominated >= max(1, count // 2):
            continue
        found.append(text)
    return found


def matches_watermark(text: str, needles: set[str]) -> bool:
    norm = normalize(text)
    if not norm:
        return False
    if norm in needles:
        return True
    for needle in needles:
        if len(needle) >= 4 and needle in norm and len(norm) <= len(needle) + 12:
            return True
        if len(norm) >= 4 and norm in needle and len(needle) <= 180:
            return True
    return False


def _blank(container, index: int) -> None:
    container[index] = TextStringObject("")


def clean_tj(array, needles: set[str], force: bool) -> int:
    texts = [operand_text(item) for item in array]
    if force:
        removed = 0
        for index, text in enumerate(texts):
            if text:
                _blank(array, index)
                removed += 1
        return removed

    removed = 0
    for index, text in enumerate(texts):
        if text and matches_watermark(text, needles):
            _blank(array, index)
            removed += 1
    if removed:
        return removed
    joined = "".join(text or "" for text in texts)
    if joined.strip() and matches_watermark(joined, needles):
        for index, text in enumerate(texts):
            if text:
                _blank(array, index)
                removed += 1
    return removed


def clean_operations(content: ContentStream, needles: set[str], ext_states, properties) -> int:
    """Blank watermark text. Drop faded / marked-watermark drawings."""
    state = DrawState()
    graphics_stack: list[tuple[tuple[float, ...], bool]] = []
    mark_stack: list[bool] = []
    kept: list[tuple] = []
    removed = 0

    for operands, operator in list(content.operations):
        if operator == b"q":
            graphics_stack.append((state.ctm, state.alpha_transparent))
            kept.append((operands, operator))
            continue
        if operator == b"Q":
            if graphics_stack:
                state.ctm, state.alpha_transparent = graphics_stack.pop()
            kept.append((operands, operator))
            continue
        if operator == b"cm" and len(operands) >= 6:
            numbers = [as_float(value) for value in operands[:6]]
            if all(number is not None for number in numbers):
                state.ctm = matmul(state.ctm, tuple(numbers))  # type: ignore[arg-type]
            kept.append((operands, operator))
            continue
        if operator == b"gs" and operands:
            key = str(operands[0])
            change = ext_states.get(key, ext_states.get(key.lstrip("/")))
            if change is not None:
                state.alpha_transparent = change
            kept.append((operands, operator))
            continue
        if operator == b"BT":
            state.text_rotated = False
            kept.append((operands, operator))
            continue
        if operator == b"Tm" and len(operands) >= 6:
            numbers = [as_float(value) for value in operands[:6]]
            if all(number is not None for number in numbers):
                a, b, c, d, _e, _f = numbers  # type: ignore[misc]
                state.text_rotated = is_diagonal(a, b, c, d)
            kept.append((operands, operator))
            continue
        if operator in (b"BMC", b"BDC"):
            tag = operands[0] if operands else ""
            props = operands[1] if len(operands) > 1 else None
            parent = mark_stack[-1] if mark_stack else False
            mark_stack.append(parent or is_watermark_mark(tag, props, properties))
            kept.append((operands, operator))
            continue
        if operator == b"EMC":
            if mark_stack:
                mark_stack.pop()
            kept.append((operands, operator))
            continue

        force = bool(mark_stack and mark_stack[-1]) or state.alpha_transparent or state.rotated()

        if operator == b"Do" and force:
            removed += 1
            continue

        if operator in (b"Tj", b"'") and operands:
            text = operand_text(operands[0])
            if text and (force or matches_watermark(text, needles)):
                _blank(operands, 0)
                removed += 1
        elif operator == b'"' and len(operands) >= 3:
            text = operand_text(operands[2])
            if text and (force or matches_watermark(text, needles)):
                _blank(operands, 2)
                removed += 1
        elif operator == b"TJ" and operands and isinstance(operands[0], (list, ArrayObject)):
            removed += clean_tj(operands[0], needles, force)

        kept.append((operands, operator))

    content.operations = kept
    return removed


def clean_container(container, pdf, needles: set[str], seen: set[int] | None = None) -> int:
    if seen is None:
        seen = set()
    removed = 0
    ext_states = load_ext_states(container)
    properties = load_property_labels(container)

    content = None
    if hasattr(container, "get_contents"):
        try:
            content = container.get_contents()
        except Exception:
            content = None
    if content is not None:
        removed += clean_operations(content, needles, ext_states, properties)
        replace = getattr(container, "replace_contents", None)
        if replace is not None:
            replace(content)
        elif hasattr(container, "set_data"):
            container.set_data(content.get_data())

    for form in _walk_xobjects(container, seen):
        form_ext = load_ext_states(form)
        form_props = load_property_labels(form)
        try:
            form_content = ContentStream(form, pdf)
        except Exception:
            continue
        count = clean_operations(form_content, needles, form_ext, form_props)
        if count:
            form.set_data(form_content.get_data())
            removed += count
    return removed


def remove_watermark_annotations(page) -> int:
    annots = page.get("/Annots")
    if not annots:
        return 0
    annots = annots.get_object()
    kept = []
    removed = 0
    for annot in list(annots):
        try:
            obj = annot.get_object()
        except Exception:
            kept.append(annot)
            continue
        if "Watermark" in str(obj.get("/Subtype", "")):
            removed += 1
            continue
        kept.append(annot)
    if removed:
        if kept:
            page[NameObject("/Annots")] = ArrayObject(kept)
        else:
            del page["/Annots"]
    return removed


def remove_watermarks(source: Path, dest: Path, needles: set[str]) -> int:
    reader = open_reader(source)
    writer = PdfWriter()
    writer.append(reader)
    removed = 0
    for page in writer.pages:
        removed += remove_watermark_annotations(page)
        removed += clean_container(page, writer, needles)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        writer.write(handle)
    return removed


def merge_pdfs(page_paths: list[Path], output_path: Path) -> None:
    ordered = sorted(page_paths, key=page_sort_key)
    merger = PdfWriter()
    for path in ordered:
        merger.append(str(path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        merger.write(handle)
    merger.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pick a PDF, split it into its own folder (or reuse that folder), "
        "remove every watermark, then merge. Output folders are kept."
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        help="Input PDF. If omitted, a numbered menu of PDFs next to this script is shown",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Merged PDF path (default: <pdf name>_merged.pdf next to this script)",
    )
    parser.add_argument(
        "--split-dir",
        default="split-pages",
        help="Parent folder for split pages. A subfolder named after the PDF is created inside it",
    )
    parser.add_argument(
        "--clean-dir",
        default="watermark-removed",
        help="Parent folder for cleaned pages (kept). A subfolder named after the PDF is created inside it",
    )
    parser.add_argument(
        "--resplit",
        action="store_true",
        help="Split the chosen PDF again even if its split-pages subfolder already has pages",
    )
    return parser.parse_args(argv)


def choose_source(args: argparse.Namespace) -> Path:
    if args.pdf:
        source = resolve_user_path(args.pdf)
        if not source.is_file():
            raise SystemExit(f"File not found: {source}")
        return source

    pdfs = list_source_pdfs(SCRIPT_DIR)
    if not pdfs:
        raise SystemExit(f"No PDFs found in {SCRIPT_DIR}")
    return prompt_pdf(pdfs)


def resolve_pages(source: Path, split_dir: Path, resplit: bool) -> list[Path]:
    reuse = existing_split_pages(split_dir)
    if reuse and not resplit:
        print(f"\n[1/3] Reusing {len(reuse)} existing page(s) in {split_dir}")
        print("      Pass --resplit to split this PDF again.")
        return reuse

    if not source.is_file():
        raise SystemExit(f"File not found: {source}")

    print(f"\n[1/3] Splitting {source.name}")
    paths = split_pdf(source, split_dir)
    print(f"      {len(paths)} page(s) written to {split_dir}")
    return paths


def main(argv: list[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    source = choose_source(args)
    folder = pdf_folder_name(source)
    split_dir = under_script(args.split_dir) / folder
    clean_dir = under_script(args.clean_dir) / folder
    if args.output:
        output_path = Path(args.output).expanduser()
        if not output_path.is_absolute():
            output_path = (Path.cwd() / output_path).resolve()
    else:
        output_path = SCRIPT_DIR / f"{folder}_merged.pdf"

    print("-" * 60)
    print("PDF split  ->  remove all watermarks  ->  merge   (pypdf)")
    print("-" * 60)
    print(f"PDF:       {source.name}")
    print(f"Folder:    {folder}")

    split_paths = resolve_pages(source, split_dir, args.resplit)

    print("\n[2/3] Finding watermarks...")
    per_page: list[list[str]] = []
    for path in split_paths:
        reader = open_reader(path)
        if not reader.pages:
            per_page.append([])
            continue
        per_page.append(extract_strings(reader.pages[0], reader))

    needles = detect_watermark_strings(per_page)
    needle_set = set(needles)
    if needles:
        print("      Repeated / tiled stamp text:")
        for needle in needles:
            print(f"        - {needle!r}")
    else:
        print("      No repeated stamp text found.")
    print("      Also removing diagonal text, faded text/images, and marked watermarks.")

    clean_dir.mkdir(parents=True, exist_ok=True)
    stale = {path.name for path in existing_split_pages(clean_dir)}
    clean_paths: list[Path] = []
    total_removed = 0
    for index, split_path in enumerate(split_paths, start=1):
        clean_path = clean_dir / split_path.name
        removed = remove_watermarks(split_path, clean_path, needle_set)
        total_removed += removed
        clean_paths.append(clean_path)
        stale.discard(split_path.name)
        print(f"      [ok] {index:>4}  {split_path.name}  ({removed} watermark op(s) cleared)")

    for name in stale:
        leftover = clean_dir / name
        if leftover.is_file():
            leftover.unlink()

    if total_removed == 0:
        print(
            "      WARNING: nothing was removed. "
            "An opaque image watermark cannot be cleared by this script."
        )

    print("\n[3/3] Merging...")
    merge_pdfs(clean_paths, output_path)

    print("-" * 60)
    print(f"Merged {len(clean_paths)} page(s) into:\n  {output_path}")
    print("Folders kept (not deleted):")
    print(f"  split pages:           {split_dir}")
    print(f"  watermark-removed:     {clean_dir}")
    print("-" * 60)


if __name__ == "__main__":
    main()
