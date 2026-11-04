#!/usr/bin/env python3
"""Rename scanned CHATIBE KLN Delivery Notes and Distribution Receipts.

The demo uses local OCR and never uploads document images to a cloud service.
By default it copies and renames files into an output directory. Use --move
only after validating the extracted fields.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import numpy as np
    import pymupdf
    from rapidocr_onnxruntime import RapidOCR
except ImportError as exc:
    print(
        "Missing dependency. Run: python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


@dataclass
class OcrToken:
    text: str
    left: float
    top: float
    right: float
    bottom: float
    score: float

    @property
    def x_center(self) -> float:
        return (self.left + self.right) / 2

    @property
    def y_center(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class DocumentInfo:
    source_file: str
    status: str
    document_type: str = ""
    delivery_note_no: str = ""
    distribution_receipt_no: str = ""
    customer_so: str = ""
    etd: str = ""
    wms_refs: list[str] = field(default_factory=list)
    target_file: str = ""
    notes: str = ""


def compact(value: str) -> str:
    return re.sub(r"[^A-Z0-9#]", "", value.upper())


def normalized_number(value: str) -> str:
    """Ignore leading zeroes when matching WMS Ref# and Delivery Note No."""
    digits = re.sub(r"\D", "", value)
    return digits.lstrip("0") or "0"


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not value:
        raise ValueError("the extracted filename is empty")
    return value


def ocr_first_page(pdf_path: Path, ocr: RapidOCR, dpi: int) -> list[OcrToken]:
    with pymupdf.open(pdf_path) as document:
        if document.page_count < 1:
            raise ValueError("PDF has no pages")
        pixmap = document[0].get_pixmap(
            dpi=dpi,
            colorspace=pymupdf.csRGB,
            alpha=False,
        )

    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )
    result, _ = ocr(image)

    tokens: list[OcrToken] = []
    for box, text, score in result or []:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        tokens.append(
            OcrToken(
                text=str(text).strip(),
                left=min(xs),
                top=min(ys),
                right=max(xs),
                bottom=max(ys),
                score=float(score),
            )
        )
    return tokens


def classify(tokens: Iterable[OcrToken]) -> str:
    text = " ".join(compact(token.text) for token in tokens)
    if "DISTRIBUTIONRECEIPT" in text:
        return "Distribution Receipt"
    if "DELIVERYNOTE" in text:
        return "Delivery Note"
    raise ValueError("unsupported document layout")


def nearest_value_to_right(
    tokens: list[OcrToken],
    label: str,
    value_pattern: str,
) -> str:
    pattern = re.compile(value_pattern, flags=re.IGNORECASE)
    labels = [token for token in tokens if compact(token.text) == label]

    for label_token in labels:
        height = max(label_token.bottom - label_token.top, 1.0)
        candidates = []
        for token in tokens:
            if token is label_token or token.left <= label_token.right:
                continue
            if abs(token.y_center - label_token.y_center) > height * 1.6:
                continue
            match = pattern.search(token.text)
            if match:
                distance = token.left - label_token.right
                candidates.append((distance, match.group(0)))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]
    return ""


def extract_delivery_note(tokens: list[OcrToken]) -> dict[str, str]:
    text = " ".join(token.text for token in tokens)

    note_match = re.search(
        r"Delivery\s*Note\s*([0-9]{7,})(?:-[0-9A-Z]+)*",
        text,
        flags=re.IGNORECASE,
    )
    delivery_note_no = note_match.group(1) if note_match else ""

    customer_so = nearest_value_to_right(
        tokens,
        "CUSTOMERSO",
        r"(?:SO#|S0#|STOHK|ST0HK)[A-Z0-9#-]{6,}",
    )
    if not customer_so:
        fallback = re.search(
            r"\b(?:SO#|S0#)[A-Z0-9#-]{6,}\b",
            text,
            flags=re.IGNORECASE,
        )
        customer_so = fallback.group(0) if fallback else ""
    customer_so = customer_so.replace("S0#", "SO#").replace("ST0HK", "STOHK")

    etd = nearest_value_to_right(
        tokens,
        "ETD",
        r"\d{1,2}/\d{1,2}/\d{4}",
    )

    if not delivery_note_no:
        raise ValueError("Delivery Note number was not found")
    if not customer_so:
        raise ValueError("Customer SO was not found")
    if not etd:
        raise ValueError("ETD was not found")

    return {
        "delivery_note_no": delivery_note_no,
        "customer_so": customer_so,
        "etd": etd,
        "target_stem": customer_so,
    }


def extract_distribution_receipt(tokens: list[OcrToken]) -> dict[str, object]:
    text = " ".join(token.text for token in tokens)

    number_match = re.search(
        r"\bNo\.?\s*:?\s*([0-9]{7,})(?:-[0-9A-Z]+)*",
        text,
        flags=re.IGNORECASE,
    )
    receipt_no = number_match.group(1) if number_match else ""

    wms_refs: list[str] = []
    for token in tokens:
        upper = token.text.upper()
        if "WMS" not in upper or "#" not in upper:
            continue
        for value in re.findall(r"\d{7,}", token.text):
            if value not in wms_refs:
                wms_refs.append(value)

    if not receipt_no:
        raise ValueError("Distribution Receipt No. was not found")
    if not wms_refs:
        raise ValueError("WMS Ref# was not found")

    return {
        "distribution_receipt_no": receipt_no,
        "wms_refs": wms_refs,
        "target_stem": receipt_no,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_inputs(paths: list[str], recursive: bool) -> list[Path]:
    result: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            result.extend(
                item.resolve()
                for item in sorted(iterator)
                if item.is_file() and item.suffix.lower() == ".pdf"
            )
        elif path.is_file() and path.suffix.lower() == ".pdf":
            result.append(path.resolve())
        else:
            raise FileNotFoundError(f"Not a PDF or directory: {path}")

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in result:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def write_reports(output_dir: Path, records: list[DocumentInfo]) -> None:
    report_json = output_dir / "rename_report.json"
    report_json.write_text(
        json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report_csv = output_dir / "rename_report.csv"
    with report_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "source_file",
                "status",
                "document_type",
                "delivery_note_no",
                "distribution_receipt_no",
                "customer_so",
                "etd",
                "wms_refs",
                "target_file",
                "notes",
            ],
        )
        writer.writeheader()
        for record in records:
            row = asdict(record)
            row["wms_refs"] = ", ".join(record.wms_refs)
            writer.writerow(row)


def annotate_wms_links(records: list[DocumentInfo]) -> None:
    notes_by_number = {
        normalized_number(record.delivery_note_no): record
        for record in records
        if record.document_type == "Delivery Note" and record.delivery_note_no
    }
    for record in records:
        if record.document_type != "Distribution Receipt":
            continue
        matched = [
            ref for ref in record.wms_refs if normalized_number(ref) in notes_by_number
        ]
        unmatched = [
            ref for ref in record.wms_refs if normalized_number(ref) not in notes_by_number
        ]
        details = [f"WMS matched {len(matched)}/{len(record.wms_refs)}"]
        if unmatched:
            details.append("unmatched WMS: " + ", ".join(unmatched))
        record.notes = "; ".join(filter(None, [record.notes, *details]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recognize and rename KLN CHATIBE PDF documents."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="One or more PDF files or directories.",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Directory for renamed files and reports.",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Move originals instead of copying them.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan input directories recursively.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=250,
        help="PDF rendering DPI for OCR (default: 250).",
    )
    return parser


def process_document(
    source: Path,
    output_dir: Path,
    ocr: RapidOCR,
    dpi: int,
    move: bool,
    planned_targets: dict[Path, Path],
) -> DocumentInfo:
    record = DocumentInfo(source_file=str(source), status="ERROR")
    try:
        tokens = ocr_first_page(source, ocr, dpi)
        record.document_type = classify(tokens)

        if record.document_type == "Delivery Note":
            fields = extract_delivery_note(tokens)
            record.delivery_note_no = str(fields["delivery_note_no"])
            record.customer_so = str(fields["customer_so"])
            record.etd = str(fields["etd"])
        else:
            fields = extract_distribution_receipt(tokens)
            record.distribution_receipt_no = str(fields["distribution_receipt_no"])
            record.wms_refs = list(fields["wms_refs"])

        target = output_dir / safe_filename(f"{fields['target_stem']}.pdf")
        record.target_file = str(target)

        if target in planned_targets and planned_targets[target] != source:
            raise ValueError(
                f"duplicate output filename; also produced by {planned_targets[target].name}"
            )
        planned_targets[target] = source

        if target.exists():
            if sha256(target) == sha256(source):
                record.status = "ALREADY_NAMED"
            else:
                raise ValueError("target file already exists with different content")
        else:
            if move:
                shutil.move(str(source), str(target))
                record.status = "MOVED"
            else:
                shutil.copy2(source, target)
                record.status = "COPIED"

    except Exception as exc:
        record.status = "ERROR"
        record.notes = str(exc)

    return record


def main() -> int:
    args = build_parser().parse_args()
    output_dir = Path(args.output).expanduser().resolve()

    try:
        inputs = collect_inputs(args.inputs, args.recursive)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not inputs:
        print("ERROR: no PDF files found", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir in {path.parent for path in inputs} and args.move:
        print(
            "ERROR: --move requires an output directory different from every input directory",
            file=sys.stderr,
        )
        return 2

    ocr = RapidOCR()
    records: list[DocumentInfo] = []
    planned_targets: dict[Path, Path] = {}

    for source in inputs:
        record = process_document(
            source=source,
            output_dir=output_dir,
            ocr=ocr,
            dpi=args.dpi,
            move=args.move,
            planned_targets=planned_targets,
        )
        records.append(record)
        print(
            f"[{record.status:14}] {source.name} -> "
            f"{Path(record.target_file).name if record.target_file else '-'}"
            + (f" ({record.notes})" if record.notes and record.status == "ERROR" else "")
        )

    annotate_wms_links(records)
    write_reports(output_dir, records)

    failed = sum(record.status == "ERROR" for record in records)
    print(
        f"\nProcessed {len(records)} file(s): "
        f"{len(records) - failed} succeeded, {failed} failed."
    )
    print(f"Reports: {output_dir / 'rename_report.csv'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
