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
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
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
    source_pages: str = ""
    region: str = ""
    destination_file: str = ""
    copy_status: str = ""


@dataclass
class PageGroup:
    document_type: str
    page_indices: list[int]
    business_key: str = ""

    @property
    def page_label(self) -> str:
        if len(self.page_indices) == 1:
            return str(self.page_indices[0] + 1)
        return f"{self.page_indices[0] + 1}-{self.page_indices[-1] + 1}"


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


def ocr_page(
    pdf_path: Path,
    page_index: int,
    ocr: RapidOCR,
    dpi: int,
) -> list[OcrToken]:
    with pymupdf.open(pdf_path) as document:
        if page_index < 0 or page_index >= document.page_count:
            raise ValueError(f"PDF page {page_index + 1} does not exist")
        pixmap = document[page_index].get_pixmap(
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


def ocr_first_page(pdf_path: Path, ocr: RapidOCR, dpi: int) -> list[OcrToken]:
    with pymupdf.open(pdf_path) as document:
        if document.page_count < 1:
            raise ValueError("PDF has no pages")
    return ocr_page(pdf_path, 0, ocr, dpi)


def classify(tokens: Iterable[OcrToken]) -> str:
    text = " ".join(compact(token.text) for token in tokens)
    if "DISTRIBUTIONRECEIPT" in text:
        return "Distribution Receipt"
    if "PACKINGLIST" in text:
        return "Packing List"
    if "DELIVERYNOTE" in text:
        return "Delivery Note"
    raise ValueError("unsupported document layout")


def page_footer(tokens: Iterable[OcrToken]) -> tuple[int, int] | None:
    for token in tokens:
        match = re.search(
            r"\bPage\s*(\d+)\s*(?:/|of)\s*(\d+)",
            token.text,
            flags=re.IGNORECASE,
        )
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def page_business_key(tokens: Iterable[OcrToken]) -> str:
    text = " ".join(token.text for token in tokens)
    match = re.search(r"\b(?:SO#|S0#)[A-Z0-9#-]{6,}\b", text, re.IGNORECASE)
    if not match:
        return ""
    return match.group(0).upper().replace("S0#", "SO#")


def page_start_type(tokens: list[OcrToken]) -> str | None:
    footer = page_footer(tokens)
    if footer and footer[0] > 1:
        return None

    compact_page = "".join(compact(token.text) for token in tokens)
    business_key = page_business_key(tokens)

    if "DISTRIBUTIONRECEIPT" in compact_page:
        return "Distribution Receipt"
    if "PACKINGLIST" in compact_page:
        if business_key:
            return "Packing List"
    if re.search(r"DELIVERYNOTE[0-9]{7,}", compact_page):
        if "CUSTOMERSO" in compact_page and "ETD" in compact_page:
            return "Delivery Note"
    if footer and footer[0] == 1 and business_key:
        return "Packing List"
    if footer and footer[0] == 1:
        try:
            return classify(tokens)
        except ValueError:
            return None
    return None


def detect_page_groups(
    pdf_path: Path,
    ocr: RapidOCR,
    dpi: int,
) -> list[PageGroup]:
    with pymupdf.open(pdf_path) as document:
        page_count = document.page_count
    if page_count < 1:
        raise ValueError("PDF has no pages")

    groups: list[PageGroup] = []
    current_type: str | None = None
    current_pages: list[int] = []
    declared_total: int | None = None
    current_key = ""

    for page_index in range(page_count):
        tokens = ocr_page(pdf_path, page_index, ocr, dpi)
        start_type = page_start_type(tokens)

        if start_type:
            business_key = page_business_key(tokens)
            footer = page_footer(tokens)

            # A one-page picking list or attachment can follow a multi-page
            # Packing List and carry Page 1/1 again. Keep it in the same
            # business document when the SO number matches.
            if (
                current_type == "Packing List"
                and start_type == "Packing List"
                and current_key
                and business_key
                and business_key == current_key
            ):
                current_pages.append(page_index)
                continue

            if current_type and current_pages:
                if declared_total is not None and len(current_pages) < declared_total:
                    raise ValueError(
                        f"{current_type} on pages {current_pages[0] + 1}-"
                        f"{current_pages[-1] + 1} is incomplete: "
                        f"expected {declared_total} page(s), found {len(current_pages)}"
                    )
                groups.append(
                    PageGroup(current_type, current_pages, current_key)
                )
            current_type = start_type
            current_pages = [page_index]
            declared_total = footer[1] if footer and footer[0] == 1 else None
            current_key = business_key
        else:
            if current_type is None:
                raise ValueError(
                    f"Page {page_index + 1} is not a recognized document start page"
                )
            current_pages.append(page_index)

    if current_type and current_pages:
        if declared_total is not None and len(current_pages) < declared_total:
            raise ValueError(
                f"{current_type} on pages {current_pages[0] + 1}-"
                f"{current_pages[-1] + 1} is incomplete: "
                f"expected {declared_total} page(s), found {len(current_pages)}"
            )
        groups.append(PageGroup(current_type, current_pages, current_key))

    if not groups:
        raise ValueError("No supported documents were found")
    return groups


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


def extract_packing_list(tokens: list[OcrToken]) -> dict[str, str]:
    customer_so = page_business_key(tokens)
    if not customer_so:
        raise ValueError("Packing List SO/OrderKey was not found")

    return {
        "customer_so": customer_so,
        "target_stem": customer_so,
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
                "source_pages",
                "region",
                "destination_file",
                "copy_status",
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


def append_note(record: DocumentInfo, note: str) -> None:
    record.notes = "; ".join(filter(None, [record.notes, note]))


def region_for_type(document_type: str) -> str:
    if document_type in {"Distribution Receipt", "Delivery Note"}:
        return "Local"
    if document_type == "Packing List":
        return "Oversea"
    return ""


def assign_regions(records: list[DocumentInfo]) -> None:
    """Assign the fixed business regions for this workflow."""
    for record in records:
        if record.status == "ERROR":
            continue
        record.region = region_for_type(record.document_type)
        if record.document_type == "Distribution Receipt":
            append_note(record, "Region: Local (Distribution Receipt)")
        elif record.document_type == "Delivery Note":
            append_note(record, "Region: Local (Hong Kong Delivery Note)")
        elif record.document_type == "Packing List":
            append_note(record, "Region: Oversea (Packing List)")


def resolve_ct_rename_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.name.casefold() == "ct rename":
        return path
    return path / "CT Rename"


def create_task_directory(root: Path, task_name: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    hong_kong_time = datetime.now(timezone(timedelta(hours=8)))
    base_name = safe_filename(
        task_name or hong_kong_time.strftime("%Y-%m-%d_%H-%M-%S")
    )
    task_dir = root / base_name
    suffix = 2
    while task_dir.exists():
        task_dir = root / f"{base_name}_{suffix:02d}"
        suffix += 1
    (task_dir / "Local").mkdir(parents=True)
    (task_dir / "Oversea").mkdir()
    return task_dir


def move_to_region_folders(task_dir: Path, records: list[DocumentInfo]) -> None:
    for record in records:
        if record.status == "ERROR" or not record.target_file or not record.region:
            continue
        current = Path(record.target_file)
        destination = task_dir / record.region / current.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if current.resolve() != destination.resolve():
            if destination.exists():
                raise FileExistsError(
                    f"Target exists in {record.region} folder: {destination.name}"
                )
            shutil.move(str(current), str(destination))
        record.target_file = str(destination)


NETWORK_DESTINATIONS = {
    ("Local", "Distribution Receipt"): (
        r"\\klhkhqsrv251\Tradelink\FL\KTMS JOB SHEET\CHATIBE\2026"
    ),
    ("Local", "Delivery Note"): (
        r"\\Klhkhqnas002\il\Looks\CS_Charlotte_Tilbury\POD\LOCAL POD\2026"
    ),
    ("Oversea", "Packing List"): (
        r"\\Klhkhqnas002\il\Looks\CS_Charlotte_Tilbury\POD\Retail POD\2026"
    ),
}


def copy_to_network_destinations(
    records: list[DocumentInfo],
    enabled: bool,
) -> tuple[int, int]:
    copied = 0
    failed = 0

    for record in records:
        if record.status == "ERROR" or not record.target_file:
            continue
        destination_root_text = NETWORK_DESTINATIONS.get(
            (record.region, record.document_type)
        )
        if not destination_root_text:
            record.copy_status = "NO_DESTINATION_RULE"
            failed += 1
            continue
        if not enabled:
            record.copy_status = "NETWORK_DISABLED"
            continue

        source = Path(record.target_file)
        destination = Path(destination_root_text) / source.name
        record.destination_file = str(destination)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if sha256(destination) == sha256(source):
                    record.copy_status = "ALREADY_PRESENT"
                    copied += 1
                    continue
                raise FileExistsError(
                    "network destination already contains a different file"
                )
            shutil.copy2(source, destination)
            record.copy_status = "COPIED"
            copied += 1
        except Exception as exc:
            record.copy_status = "FAILED"
            append_note(record, f"Network copy failed: {exc}")
            failed += 1

    return copied, failed


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
        default=str(Path.home() / "Documents" / "CT Rename"),
        help=(
            "CT Rename root directory. A timestamped task folder is created "
            "inside it automatically (default: ~/Documents/CT Rename)."
        ),
    )
    parser.add_argument(
        "--task-name",
        help="Optional task folder name; mainly useful for controlled testing.",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Keep results in CT Rename only; skip copying to the UNC destinations.",
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
    reported_source: Path | None = None,
    source_pages: str = "",
) -> DocumentInfo:
    record = DocumentInfo(
        source_file=str(reported_source or source),
        status="ERROR",
        source_pages=source_pages,
    )
    try:
        tokens = ocr_first_page(source, ocr, dpi)
        record.document_type = classify(tokens)

        if record.document_type == "Delivery Note":
            fields = extract_delivery_note(tokens)
            record.delivery_note_no = str(fields["delivery_note_no"])
            record.customer_so = str(fields["customer_so"])
            record.etd = str(fields["etd"])
        elif record.document_type == "Packing List":
            fields = extract_packing_list(tokens)
            record.customer_so = str(fields["customer_so"])
        else:
            fields = extract_distribution_receipt(tokens)
            record.distribution_receipt_no = str(fields["distribution_receipt_no"])
            record.wms_refs = list(fields["wms_refs"])

        record.region = region_for_type(record.document_type)
        target = (
            output_dir
            / record.region
            / safe_filename(f"{fields['target_stem']}.pdf")
        )
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


def split_page_group(
    source: Path,
    group: PageGroup,
    destination: Path,
) -> None:
    with pymupdf.open(source) as original:
        output = pymupdf.open()
        try:
            output.insert_pdf(
                original,
                from_page=group.page_indices[0],
                to_page=group.page_indices[-1],
            )
            output.save(
                destination,
                garbage=3,
                deflate=True,
            )
        finally:
            output.close()


def process_pdf_batch(
    source: Path,
    output_dir: Path,
    ocr: RapidOCR,
    dpi: int,
    move: bool,
    planned_targets: dict[Path, Path],
) -> list[DocumentInfo]:
    """Split one scanned PDF into logical documents and rename each one."""
    try:
        groups = detect_page_groups(source, ocr, dpi)
    except Exception as exc:
        return [
            DocumentInfo(
                source_file=str(source),
                status="ERROR",
                notes=str(exc),
            )
        ]

    with pymupdf.open(source) as document:
        page_count = document.page_count

    is_whole_file = (
        len(groups) == 1
        and groups[0].page_indices == list(range(page_count))
    )
    if is_whole_file:
        return [
            process_document(
                source=source,
                output_dir=output_dir,
                ocr=ocr,
                dpi=dpi,
                move=move,
                planned_targets=planned_targets,
                source_pages=groups[0].page_label,
            )
        ]

    if move:
        return [
            DocumentInfo(
                source_file=str(source),
                status="ERROR",
                notes="A PDF containing multiple documents cannot be moved; "
                "use copy mode and remove the source manually after validation",
            )
        ]

    records: list[DocumentInfo] = []
    with tempfile.TemporaryDirectory(prefix="chatibe_split_") as temp_dir:
        temp_root = Path(temp_dir)
        for index, group in enumerate(groups, start=1):
            split_path = temp_root / f"{source.stem}_document_{index}.pdf"
            try:
                split_page_group(source, group, split_path)
                record = process_document(
                    source=split_path,
                    output_dir=output_dir,
                    ocr=ocr,
                    dpi=dpi,
                    move=False,
                    planned_targets=planned_targets,
                    reported_source=source,
                    source_pages=group.page_label,
                )
            except Exception as exc:
                record = DocumentInfo(
                    source_file=str(source),
                    status="ERROR",
                    source_pages=group.page_label,
                    notes=f"split failed: {exc}",
                )
            records.append(record)
    return records


def main() -> int:
    args = build_parser().parse_args()
    rename_root = resolve_ct_rename_root(Path(args.output))

    try:
        inputs = collect_inputs(args.inputs, args.recursive)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not inputs:
        print("ERROR: no PDF files found", file=sys.stderr)
        return 2

    if rename_root in {path.parent for path in inputs} and args.move:
        print(
            "ERROR: --move requires an output directory different from every input directory",
            file=sys.stderr,
        )
        return 2

    ocr = RapidOCR()
    records: list[DocumentInfo] = []
    planned_targets: dict[Path, Path] = {}
    task_dir = create_task_directory(rename_root, args.task_name)

    for source in inputs:
        source_records = process_pdf_batch(
            source=source,
            output_dir=task_dir,
            ocr=ocr,
            dpi=args.dpi,
            move=args.move,
            planned_targets=planned_targets,
        )
        records.extend(source_records)
        for record in source_records:
            page_suffix = f" p.{record.source_pages}" if record.source_pages else ""
            print(
                f"[{record.status:14}] {source.name}{page_suffix} -> "
                f"{Path(record.target_file).name if record.target_file else '-'}"
                + (
                    f" ({record.notes})"
                    if record.notes and record.status == "ERROR"
                    else ""
                )
            )

    annotate_wms_links(records)
    assign_regions(records)
    move_to_region_folders(task_dir, records)
    network_copied, network_failed = copy_to_network_destinations(
        records,
        enabled=not args.no_network,
    )
    write_reports(task_dir, records)

    failed = sum(record.status == "ERROR" for record in records)
    print(
        f"\nProcessed {len(records)} file(s): "
        f"{len(records) - failed} succeeded, {failed} failed."
    )
    print(f"Task folder: {task_dir}")
    print(f"Reports: {task_dir / 'rename_report.csv'}")
    if args.no_network:
        print("Network copy skipped by --no-network.")
    else:
        print(
            f"Network copy: {network_copied} succeeded, "
            f"{network_failed} failed."
        )
    return 1 if failed or network_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
