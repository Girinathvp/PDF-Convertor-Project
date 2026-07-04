"""
extractor.py
Core PDF -> table extraction logic, purpose-built for bank statements.

Unlike a generic "OCR the page and grid the words" approach, this module
understands that a bank statement transaction table has real semantic
columns (Date, Particulars/Narration, Cheque No, Withdrawals, Deposits,
Balance) and that:

  - narration frequently wraps onto a second/third visual line with NO date
    in the date column -> those lines must be merged back into the same
    logical row, not treated as new rows or split across cells.
  - every page repeats the bank's letterhead/address block and a per-page
    subtotal/footer -> these must be dropped, not merged into the data.
  - when multiple pages are merged into one sheet, the column header should
    appear exactly once, not once per page.

Extraction pipeline per page:
  1. Get "words" (word + bounding box), either from the PDF's real text
     layer (fast, exact) or, if the page has zero extractable characters
     (scanned / vector-flattened text), from Tesseract OCR on a rendered
     image of the page.
  2. Group words into visual lines (rows) by vertical position.
  3. Find the column-header line (looks for PARTICULARS/WITHDRAWALS/
     DEPOSITS/BALANCE/etc.) and derive column x-boundaries from it.
  4. Walk the lines below the header, assigning each word to its column by
     x-position. A line whose Date-column text is an actual date (or B/F)
     starts a new transaction row; any other line is a continuation and its
     text is appended into the same row's cells (so narration stays whole).
  5. Stop consuming lines once a footer marker is hit (page total, page
     number, disclaimer, etc.).

If no header/table can be detected on a page (e.g. a cover page), the page
contributes no rows, rather than dumping garbage into the sheet.
"""

import re
import pdfplumber
import pandas as pd


# ---------------------------------------------------------------------------
# Page counting / TAT estimation
# ---------------------------------------------------------------------------

def get_page_count(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def estimate_tat(num_pages, likely_ocr=False):
    base_overhead = 3
    per_page = 1.6 if likely_ocr else 0.5
    return round(base_overhead + per_page * num_pages)


# ---------------------------------------------------------------------------
# Column-header / footer vocabulary (kept generic across banks)
# ---------------------------------------------------------------------------

HEADER_KEYWORDS = [
    "date", "particulars", "narration", "description", "details",
    "chq", "cheque", "ref", "reference",
    "withdrawal", "debit", "deposit", "credit", "balance", "amount",
]

FOOTER_MARKERS = [
    "page total", "b/f total", "closing balance total",
    "this is a system", "does not require any signature",
    "visit us at", "customer care", "toll-free", "toll free",
    "generated statement",
]

DATE_RE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
CARRY_FORWARD_RE = re.compile(r"^(b/f|c/f|bf|cf)$", re.IGNORECASE)


def _looks_like_date(text):
    text = (text or "").strip()
    return bool(DATE_RE.match(text)) or bool(CARRY_FORWARD_RE.match(text))


def _is_footer_text(joined_lower):
    return any(marker in joined_lower for marker in FOOTER_MARKERS)


# ---------------------------------------------------------------------------
# Word grouping into visual lines
# ---------------------------------------------------------------------------

def _group_words_into_lines(words, y_tol):
    """words: list of dicts with text,x0,x1,top,bottom. Returns list of
    lines, each a list of words sorted left-to-right, sorted top-to-bottom."""
    if not words:
        return []
    ws = sorted(words, key=lambda w: w["top"])
    lines = []
    current = [ws[0]]
    current_top = ws[0]["top"]
    for w in ws[1:]:
        if abs(w["top"] - current_top) <= y_tol:
            current.append(w)
            current_top = sum(x["top"] for x in current) / len(current)
        else:
            lines.append(sorted(current, key=lambda x: x["x0"]))
            current = [w]
            current_top = w["top"]
    lines.append(sorted(current, key=lambda x: x["x0"]))
    return lines


# ---------------------------------------------------------------------------
# Header detection + column boundary construction
# ---------------------------------------------------------------------------

def _find_header_line_index(lines):
    best_idx, best_score = None, 0
    for idx, line in enumerate(lines):
        score = 0
        for w in line:
            t = w["text"].lower().strip(":.")
            if any(t == k or t.startswith(k) for k in HEADER_KEYWORDS):
                score += 1
        if score > best_score:
            best_score, best_idx = score, idx
    # Require at least 3 recognizable header labels to trust this line
    return best_idx if best_score >= 3 else None


def _merge_adjacent_header_words(header_words, merge_gap):
    """Merge OCR/text fragments like 'CHQ' + '.NO.' into one label when the
    gap between them is small (same column heading split into two words)."""
    ordered = sorted(header_words, key=lambda w: w["x0"])
    merged = []
    for w in ordered:
        if merged and (w["x0"] - merged[-1]["x1"]) < merge_gap:
            merged[-1]["text"] += " " + w["text"]
            merged[-1]["x1"] = max(merged[-1]["x1"], w["x1"])
        else:
            merged.append(dict(w))
    return merged


def _build_columns(header_words, page_width, merge_gap):
    merged = _merge_adjacent_header_words(header_words, merge_gap)
    merged.sort(key=lambda w: w["x0"])

    # If no column looks like a Date column, assume an implicit Date column
    # occupies everything to the left of the first detected header (common
    # when OCR fails to read a short/faint "DATE" label).
    has_date_col = any("date" in w["text"].lower() for w in merged)
    if not has_date_col and merged and merged[0]["x0"] > page_width * 0.05:
        merged.insert(0, {"text": "DATE", "x0": 0.0, "x1": merged[0]["x0"]})

    columns = []
    for i, w in enumerate(merged):
        start = 0.0 if i == 0 else (merged[i - 1]["x0"] + w["x0"]) / 2
        end = page_width + 1000 if i == len(merged) - 1 else (w["x0"] + merged[i + 1]["x0"]) / 2
        label = w["text"].strip().rstrip(".:").strip()
        columns.append({"label": label or f"Col{i+1}", "x_start": start, "x_end": end})
    return columns


def _assign_line_to_columns(line, columns):
    row = {c["label"]: "" for c in columns}
    for w in line:
        for c in columns:
            if c["x_start"] <= w["x0"] < c["x_end"]:
                row[c["label"]] = (row[c["label"]] + " " + w["text"]).strip()
                break
        else:
            # falls right of the last boundary -> last column
            last = columns[-1]["label"]
            row[last] = (row[last] + " " + w["text"]).strip()
    return row


_NUMERIC_CODE_RE = re.compile(r"^[\d/\-\.]{3,}$")
_REF_COLUMN_KEYWORDS = ("chq", "cheque", "ref", "reference")


def _reconcile_reference_column(row, columns):
    """
    A Chq No / Reference column should only ever hold numeric-looking codes.
    When narration text is long enough to physically overlap that column's
    x-range on a given line (common when there's no chq number on that
    line), stray words land there. Move anything that isn't a numeric code
    back onto the Particulars/Narration column, in place, so the sentence
    reads correctly once lines are stitched together.
    """
    particulars_label = next(
        (c["label"] for c in columns
         if any(k in c["label"].lower() for k in ("particular", "narration", "description", "detail"))),
        None,
    )
    if particulars_label is None:
        return row

    for c in columns:
        label_lower = c["label"].lower()
        if not any(k in label_lower for k in _REF_COLUMN_KEYWORDS):
            continue
        text = row.get(c["label"], "")
        if not text:
            continue
        tokens = text.split()
        numeric_tokens = [t for t in tokens if _NUMERIC_CODE_RE.match(t)]
        stray_tokens = [t for t in tokens if not _NUMERIC_CODE_RE.match(t)]
        if stray_tokens:
            row[particulars_label] = f"{row[particulars_label]} {' '.join(stray_tokens)}".strip()
            row[c["label"]] = " ".join(numeric_tokens)
    return row


# ---------------------------------------------------------------------------
# Structured extraction for one page
# ---------------------------------------------------------------------------

def _extract_structured(words, page_width, y_tol, merge_gap):
    """
    Returns (header_labels, rows) where rows is a list of dicts keyed by
    header_labels, or (None, None) if no table header could be found.
    """
    lines = _group_words_into_lines(words, y_tol=y_tol)
    header_idx = _find_header_line_index(lines)
    if header_idx is None:
        return None, None

    columns = _build_columns(lines[header_idx], page_width, merge_gap)
    date_label = columns[0]["label"]

    rows = []
    current = None
    for line in lines[header_idx + 1:]:
        joined_lower = " ".join(w["text"] for w in line).lower()
        if _is_footer_text(joined_lower):
            break

        row = _assign_line_to_columns(line, columns)
        row = _reconcile_reference_column(row, columns)
        date_val = row.get(date_label, "")

        if _looks_like_date(date_val):
            if current is not None:
                rows.append(current)
            current = row
        else:
            if current is None:
                current = row  # no date seen yet this page; start anyway
            else:
                for label, val in row.items():
                    if not val:
                        continue
                    current[label] = f"{current[label]} {val}".strip() if current[label] else val

    if current is not None:
        rows.append(current)

    header_labels = [c["label"] for c in columns]
    return header_labels, rows


# ---------------------------------------------------------------------------
# Word sourcing: real text layer vs. OCR
# ---------------------------------------------------------------------------

def _words_from_text_layer(page):
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    return [
        {"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "bottom": w["bottom"]}
        for w in words
    ]


def _words_from_ocr(page, dpi=250):
    import pytesseract
    import os as _os

    # On Windows, fall back to the default install path if tesseract isn't
    # on PATH.
    if _os.name == "nt":
        default_win_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if _os.path.exists(default_win_path):
            pytesseract.pytesseract.tesseract_cmd = default_win_path

    pil_image = page.to_image(resolution=dpi).original
    data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)

    scale = 72.0 / dpi  # OCR pixels (at `dpi`) -> PDF points (72 dpi)
    words = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        conf = data.get("conf", ["-1"] * n)[i]
        try:
            if float(conf) < 0:
                continue
        except (ValueError, TypeError):
            pass
        left = data["left"][i] * scale
        top = data["top"][i] * scale
        width = data["width"][i] * scale
        height = data["height"][i] * scale
        words.append(
            {"text": text, "x0": left, "x1": left + width, "top": top, "bottom": top + height}
        )
    return words


# ---------------------------------------------------------------------------
# Per-page orchestration
# ---------------------------------------------------------------------------

def extract_page(page):
    """
    Returns (page_result, method) where page_result is either:
      {"type": "structured", "header": [...], "rows": [dict, ...]}
      {"type": "none"}   (no table detected on this page - e.g. cover page)
    method is 'text' or 'ocr' for diagnostics.
    """
    has_chars = len(page.chars) > 0

    if has_chars:
        words = _words_from_text_layer(page)
        method = "text"
        y_tol, merge_gap = 3, 6
    else:
        words = _words_from_ocr(page)
        method = "ocr"
        y_tol, merge_gap = 6, 15

    header, rows = _extract_structured(words, page.width, y_tol=y_tol, merge_gap=merge_gap)
    if header is None:
        return {"type": "none"}, method

    return {"type": "structured", "header": header, "rows": rows}, method


def process_pdf(pdf_path, progress_callback=None):
    """
    Returns: (results, methods)
      results: {page_number: page_result}
      methods: {page_number: 'text' | 'ocr'}
    """
    results = {}
    methods = {}
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            page_result, method = extract_page(page)
            results[i + 1] = page_result
            methods[i + 1] = method
            if progress_callback:
                progress_callback(i + 1, total)
    return results, methods


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------

def _canonical_header(results):
    """Pick the header (column label list) from the first page that has one,
    preferring the longest/most complete header seen across all pages."""
    candidates = [
        r["header"] for r in results.values()
        if r.get("type") == "structured" and r.get("header")
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def _row_to_ordered_list(row, header):
    """Map a row dict onto the canonical header order, tolerating pages
    whose detected labels differ slightly (fuzzy match by substring)."""
    out = []
    row_keys_lower = {k.lower(): k for k in row.keys()}
    for label in header:
        key = row_keys_lower.get(label.lower())
        if key is None:
            # fuzzy fallback: find a row key that contains/is contained by label
            key = next(
                (k for k in row.keys() if label.lower() in k.lower() or k.lower() in label.lower()),
                None,
            )
        out.append(row.get(key, "") if key else "")
    return out


def write_excel(results, output_path, merge_pages=True):
    header = _canonical_header(results)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        if merge_pages:
            all_rows = []
            for page_num in sorted(results.keys()):
                res = results[page_num]
                if res.get("type") != "structured":
                    continue
                for row in res["rows"]:
                    all_rows.append(_row_to_ordered_list(row, header) if header else list(row.values()))

            if header:
                df = pd.DataFrame(all_rows, columns=header)
            else:
                df = pd.DataFrame(all_rows)
            df.to_excel(writer, sheet_name="Statement", index=False)

        else:
            used_names = set()
            for page_num in sorted(results.keys()):
                res = results[page_num]
                if res.get("type") != "structured" or not res["rows"]:
                    continue
                page_header = res["header"]
                rows = [list(r.values()) for r in res["rows"]]
                df = pd.DataFrame(rows, columns=page_header)

                sheet_name = f"Page_{page_num}"[:31]
                base, suffix = sheet_name, 1
                while sheet_name in used_names:
                    sheet_name = f"{base[:28]}_{suffix}"
                    suffix += 1
                used_names.add(sheet_name)
                df.to_excel(writer, sheet_name=sheet_name, index=False)
