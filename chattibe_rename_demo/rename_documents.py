#!/usr/bin/env python3
"""Recognize and rename CHATIBE Distribution Receipt and Delivery Note PDFs.

This is a local demo. It renders the first page of each PDF, runs offline OCR,
extracts the business identifiers from the KLN form layout, and writes renamed
copies to an output directory. Original PDFs are left unchanged unless --move
is explicitly supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pymupdf
from rapidocr_onnxruntime import RapidOCR


@dataclass
class OcrLine:
    text: str
    left: float
    top: float
    right: float
    bottom: float
    confidence: float

    @property
    def compact(self) -> str:
        return compact_text(self.text)


@dataclass
class DocumentResult:
    source_file: str
    document_type: str
    status: str
    final_name: str | None = None
    distribution_receipt_no: str | None = None
    delivery_note_no: str | None = None
    wms_refs: list[str] | None = None
    customer_so: str | None = None
    etd: str | None = None
    message: str | None = None


def compact_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9#]", "", value.upper())


def normalize_numeric_id(value: str) -> str:
    """Normalize OCR differences caused by leading zeroes."""
    digits = re.sub(r"\D", "", value)
    return digits.lstrip("0") or "0"


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        raise ValueError("The extracted filename is empty.")
    return value


def ocr_first_page(pdf_path: Path, ocr: RapidOCR, dpi: int) -> list[OcrLine]:
    with pymupdf.open(pdf_path) as document:
        if document.page_count < 1:
            raise ValueError("PDF has no pages.")
        pixmap = document[0].get_pixmap(
            dpi=dpi,
            colorspace=pymupdf.csRGB,
            alpha=False,
        )

    image = np.frombuffer(
        pixmap.samples,
        dtype=np.uint8,
    ).reshape(pixmap.height, pixmap.width, pixmap.n)

    result, _ = ocr(image)
    lines: list[OcrLine] = []
    for box, text, confidence in result or []:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        lines.append(
            OcrLine(
                text=str(text).strip(),
                left=min(xs),
                top=min(ys),
                right=max(xs),
                bottom=max(ys),
                confidence=float(confidence),
            )
        )
    return lines


def classify_document(lines: Iterable[OcrLine]) -> str:
    compact_lines = [line.compact for line in lines]
    joined = "|".join(compact_lines)
    if "DISTRIBUTIONRECEIPT" in joined or "配送收據" in "".join(
        line.text for line in lines
    ):
        return "DISTRIBUTION_RECEIPT"
    if "DELIVERYNOTE" in joined:
        return "DELIVERY_NOTE"
    raise ValueError("Unsupported document: neither Distribution Receipt nor Delivery Note was detected.")


def find_right_value(
    lines: list[OcrLine],
    label_pattern: re.Pattern[str],
) -> str | None:
    """Return the nearest OCR value to the right of a form label."""
    labels = [line for line in lines if label_pattern.search(line.compact)]
    if not labels:
        return None

    candidates: list[tuple[float, OcrLine]] = []
    for label in labels:
        label_height = max(label.bottom - label.top, 1.0)
        label_center = (label.top + label.bottom) / 2
        for candidate in lines:
            if candidate is label or candidate.right <= label.right:
                continue
            candidate_center = (candidate.top + candidate.bottom) / 2
            vertical_distance = abs(candidate_center - label_center)
            if vertical_distance > label_height * 1.8:
                continue
            horizontal_gap = max(0.0, candidate.left - label.right)
            score = vertical_distance * 4 + horizontal_gap
            candidates.append((score, candidate))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1].text.strip()


def parse_delivery_date(value: str) -> str | None:
    slash_date = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if slash_date:
        first, second, year = map(int, slash_date.groups())
        # The current KLN sample uses MM/DD/YYYY.
        month, day = first, second
        try:
            parsed = datetime(year, month, day)
        except ValueError:
            return None
        return parsed.strftime("%Y-%m-%d")

    iso_date = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if iso_date:
        try:
            return datetime(
                int(iso_date.group(1)),
                int(iso_date.group(2)),
                int(iso_date.group(3)),
            ).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def extract_delivery_note(lines: list[OcrLine]) -> DocumentResult:
    note_number = None
    for line in lines:
        match = re.search(
            r"Delivery\s*Note\s*([0-9]{8,})(?:-[0-9A-Z]+)*",
            line.text,
            flags=re.IGNORECASE,
        )
        if match:
            note_number = match.group(1)
            break

    so_value = find_right_value(
        lines,
        re.compile(r"CUSTOMER(?:SO|S0)"),
    )
    if so_value:
        so_match = re.search(r"(?:SO#|S0#|STO)?[A-Z0-9#-]{6,}", so_value.upper())
        so_value = so_match.group(0) if so_match else so_value
        so_value = so_value.replace("S0#", "SO#")

    etd_value = None
    etd_label = next(
        (line for line in lines if line.compact in {"ETD", "E.T.D"}),
        None,
    )
    if etd_label:
        for line in lines:
            if line is etd_label or line.left <= etd_label.right:
                continue
            if abs((line.top + line.bottom) / 2 - (etd_label.top + etd_label.bottom) / 2) < 80:
                etd_value = parse_delivery_date(line.text)
                if etd_value:
                    break

    if not note_number:
        raise ValueError("Delivery Note number was not found.")
    if not so_value:
        raise ValueError("Customer SO was not found.")
    if not etd_value:
        raise ValueError("ETD was not found or is invalid.")

    return DocumentResult(
        source_file="",
        document_type="DELIVERY_NOTE",
        status="RECOGNIZED",
        final_name=f"{sanitize_filename(so_value)}.pdf",
        delivery_note_no=note_number,
        customer_so=so_value,
        etd=etd_value,
    )


def extract_distribution_receipt(lines: list[OcrLine]) -> DocumentResult:
    receipt_number = None
    for line in lines:
        match = re.search(
            r"\bNo\.?\s*([0-9]{7,})(?:-[0-9A-Z]+)*",
            line.text,
            flags=re.IGNORECASE,
        )
        if match:
            receipt_number = match.group(1)
            break

    wms_refs: list[str] = []
    for line in lines:
        if "WMS" not in line.text.upper():
            continue
        wms_refs.extend(re.findall(r"\d{7,}", line.text))

    # Remove duplicate OCR values while preserving document order.
    wms_refs = list(dict.fromkeys(wms_refs))

    if not receipt_number:
        raise ValueError("Distribution Receipt No. was not found.")
    if not wms_refs:
        raise ValueError("WMS Ref# was not found.")

    return DocumentResult(
        source_file="",
        document_type="DISTRIBUTION_RECEIPT",
        status="RECOGNIZED",
        final_name=f"{sanitize_filename(receipt_number)}.pdf",
        distribution_receipt_no=receipt_number,
        wms_refs=wms_refs,
    )


def expand_inputs(inputs: Iterable[str], output_dir: Path) -> list[Path]:
    files: list[Path] = []
    output_resolved = output_dir.resolve()
    for raw_input in inputs:
        path = Path(raw_input).expanduser()
        if path.is_dir():
            for candidate in sorted(path.rglob("*.pdf")):
                if output_resolved not in candidate.resolve().parents:
                    files.append(candidate.resolve())
        elif path.is_file() and path.suffix.lower() == ".pdf":
            files.append(path.resolve())
        else:
            raise FileNotFoundError(f"Input not found or not a PDF: {raw_input}")
    return list(dict.fromkeys(files))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_copy(source: Path, target: Path, move: bool) -> str:
    if target.exists():
        if file_sha256(source) == file_sha256(target):
            return "ALREADY_PRESENT"
        raise FileExistsError(f"Target exists with different content: {target.name}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(source), str(target))
    else:
        shutil.copy2(source, target)
    return "MOVED" if move else "COPIED"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recognize and rename CHATIBE KLN Distribution Receipt/Delivery Note PDFs."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="PDF files or directories containing PDFs.",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Directory for renamed PDFs and the recognition manifest.",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Move source PDFs instead of copying them. Default is copy.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=250,
        help="OCR render resolution. Default: 250.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_files = expand_inputs(args.inputs, output_dir)
    if not input_files:
        print("No PDF files found.", file=sys.stderr)
        return 2

    ocr = RapidOCR()
    results: list[DocumentResult] = []

    for source in input_files:
        try:
            lines = ocr_first_page(source, ocr, args.dpi)
            document_type = classify_document(lines)
            if document_type == "DISTRIBUTION_RECEIPT":
                result = extract_distribution_receipt(lines)
            else:
                result = extract_delivery_note(lines)
            result.source_file = source.name

            assert result.final_name is not None
            target = output_dir / result.final_name
            result.status = publish_copy(source, target, args.move)
            results.append(result)
            print(f"{source.name} -> {result.final_name} [{result.status}]")
        except Exception as exc:
            results.append(
                DocumentResult(
                    source_file=source.name,
                    document_type="UNKNOWN",
                    status="ERROR",
                    message=str(exc),
                )
            )
            print(f"{source.name} -> ERROR: {exc}", file=sys.stderr)

    delivery_notes = [
        result for result in results if result.document_type == "DELIVERY_NOTE"
    ]
    receipts = [
        result
        for result in results
        if result.document_type == "DISTRIBUTION_RECEIPT"
    ]
    wms_links: dict[str, list[str]] = {}
    for receipt in receipts:
        for wms_ref in receipt.wms_refs or []:
            wms_key = normalize_numeric_id(wms_ref)
            wms_links[wms_key] = [
                note.final_name or note.source_file
                for note in delivery_notes
                if note.delivery_note_no
                and normalize_numeric_id(note.delivery_note_no) == wms_key
            ]

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "move" if args.move else "copy",
        "documents": [asdict(result) for result in results],
        "wms_links": wms_links,
    }
    manifest_path = output_dir / "recognition_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Manifest: {manifest_path}")

    return 1 if any(result.status == "ERROR" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
