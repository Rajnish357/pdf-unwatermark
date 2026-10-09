# PDF Split, Unwatermark, and Merge

Remove text watermarks from a PDF without naming them. The script splits the file into one PDF per page, clears the watermark, then merges the pages back in order. Each source PDF keeps its own folders, so you can process more than one file without mixing pages.

It uses [pypdf](https://github.com/py-pdf/pypdf) only. Nothing is deleted when the run finishes: split pages and cleaned pages stay on disk, and an existing split is reused the next time you pick the same PDF.

## Introduction

Watermarks in study notes and downloaded PDFs are often the same phrase on every page, a diagonal stamp, or faded text laid over the page. This script looks for those patterns itself. You do not type the watermark text.

Put the PDFs next to `pdf_unwatermark.py`, run it, and choose a file from a numbered menu. You can also pass a path on the command line.

What it writes for a file named `polity 1.pdf`:

| Result | Path |
|---|---|
| Original pages | `split-pages/polity 1/page_1.pdf`, `page_2.pdf`, … |
| Pages with watermarks removed | `watermark-removed/polity 1/` |
| Merged PDF | `polity 1_merged.pdf` |

`page_2.pdf` is ordered before `page_10.pdf`. Merged files are not shown in the menu.

## Setup

Python 3.9 or newer.

```bash
pip install pypdf
```

Copy `pdf_unwatermark.py` into the folder where your PDFs live, or download it into that folder. The menu only lists `*.pdf` files in the same directory as the script. It does not search subfolders.

Check that pypdf imports:

```bash
python -c "import pypdf; print(pypdf.__version__)"
```

## Usage

### Menu

From the script's folder:

```bash
python pdf_unwatermark.py
```

```text
PDFs in /path/to/script:
  1) history.pdf
  2) polity 1.pdf
Choose a PDF (1-2):
```

Enter a number. Invalid input is rejected and the prompt is shown again.

### One file, by path

```bash
python pdf_unwatermark.py "polity 1.pdf"
```

A relative path is tried in the current directory first, then next to the script.

### Split again

If `split-pages/<pdf name>/` already contains `page_*.pdf`, those pages are reused and the source PDF is not split again. Force a new split after you replace the source file:

```bash
python pdf_unwatermark.py "polity 1.pdf" --resplit
```

### Options

| Option | Meaning | Default |
|---|---|---|
| `pdf` | Input PDF. If omitted, the numbered menu is shown. | menu |
| `-o`, `--output` | Where to write the merged PDF. A relative path is from the current directory. | `<pdf name>_merged.pdf` next to the script |
| `--split-dir` | Parent folder for split pages. The PDF name is added as a subfolder. | `split-pages` next to the script |
| `--clean-dir` | Parent folder for cleaned pages. The PDF name is added as a subfolder. | `watermark-removed` next to the script |
| `--resplit` | Split again even when that PDF's page folder already exists. | off |

Examples:

```bash
python pdf_unwatermark.py "polity 1.pdf" -o ~/Desktop/polity-clean.pdf
python pdf_unwatermark.py notes.pdf --split-dir /tmp/pages --clean-dir /tmp/clean
```

Folders are created if they are missing. Old `page_*.pdf` files in that PDF's subfolder are replaced only when a new split runs. The folders themselves are never removed.

## How it works

1. **Choose** a PDF from the menu, or from the path you passed.
2. **Split** it into `split-pages/<pdf name>/page_N.pdf`, or reuse that folder.
3. **Detect** watermark text by reading every page, including text inside form XObjects.
4. **Remove** matching text operators, faded drawings, diagonal stamps, marked watermark content, and watermark annotations. Cleaned pages are written to `watermark-removed/<pdf name>/`.
5. **Merge** those pages into `<pdf name>_merged.pdf`.

While it runs, the script prints every repeated string it decided was a stamp. Check that list. If a real heading is in it, that heading was removed.

### What is treated as a watermark

- The same multi-word phrase on most pages (about 60% or more), such as `SAMPLE WATERMARK`.
- A short ALL-CAPS stamp on at least 80% of pages, such as `DRAFT` or `SAMPLE`.
- Any other string of 8 or more characters on at least 80% of pages.
- Text drawn more than once on the same page (a tiled stamp).
- Text rotated off the horizontal or vertical axis by more than 15 degrees. A slight italic skew is left alone. Text turned 90 degrees is left alone.
- Text or images painted with opacity below 0.95 (`/ca` or `/CA` in an ExtGState).
- Content wrapped in a watermark marked-content block, or an optional-content group whose name contains "watermark".
- Annotations whose subtype is Watermark.

Ordinary words that happen to appear on every page, such as `that` or `with`, are not removed. A string that is most of the real text on a page is not removed either.

## Uses

- Strip a site name or "confidential" stamp from a set of lecture notes before reading or printing.
- Clean several PDFs in one folder. Each file gets its own `split-pages` and `watermark-removed` subfolder.
- Re-run the cleaner after a detection tweak without splitting a large PDF again. Leave the page folder in place and run the script without `--resplit`.
- Keep the per-page PDFs. The split and cleaned folders are the working copies; only the merged file is the final document.

You are responsible for using this on files you are allowed to modify. Removing a watermark does not change the copyright of the document.

## Limitations

- **Opaque images and logos** are not removed. If the mark is a picture with no transparency and it is not inside a watermark marked-content block, it stays. The script warns when it clears nothing.
- **Vector drawings** (lines, shapes, paths) are not removed unless they sit in an explicit watermark block. A watermark built only from paths can remain.
- **Text broken into single letters** is removed when those letters are diagonal, faded, or inside a watermark mark. A horizontal watermark stored as one letter per text operator, with no other clue, can survive.
- **Repeated headings and footers** can be removed. A phrase that is identical on most pages looks like a stamp. Read the printed list before trusting the merged file.
- **Different wording on each page** is not treated as one stamp. `Downloaded from site, page 3` and `Downloaded from site, page 4` are different strings, so the cross-page rule will not group them. A diagonal or faded version of that line is still removed.
- **Encrypted PDFs** open only when the empty password works (the usual owner-password-only case). Anything else stops with an error.
- **Scanned pages** are images. There is no text layer to edit, so the scan is unchanged.
- **Complex font encodings** can hide the real characters from the content stream. A stamp that does not decode as the text you see on the page may be missed.
- **Bookmarks, links, and some interactive features** are not rebuilt. The result is the page content, merged in order.
- **One-page files** have no "same text on most pages" signal. Only tiled text, diagonal text, faded marks, and explicit watermark marks are removed.
- The menu sees only PDFs **next to the script**, not PDFs in subfolders and not files named `*_merged.pdf`.

## Output layout

```text
your-folder/
├── pdf_unwatermark.py
├── polity 1.pdf
├── history.pdf
├── polity 1_merged.pdf
├── history_merged.pdf
├── split-pages/
│   ├── polity 1/
│   │   ├── page_1.pdf
│   │   └── page_2.pdf
│   └── history/
│       └── page_1.pdf
└── watermark-removed/
    ├── polity 1/
    └── history/
```

Characters that are illegal in a folder name (`<>:"/\|?*` and control characters) are replaced with `_` in the subfolder name. The extension is not part of the folder name.

## Requirements

| Package | Why |
|---|---|
| Python 3.9+ | Runs the script |
| pypdf | Reads, edits, and writes the PDFs |

No other third-party packages are required.

## License

No license is set on this project yet. Add one before you publish the repository if you want others to reuse it.
