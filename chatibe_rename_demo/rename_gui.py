#!/usr/bin/env python3
"""Simple Windows-friendly GUI for the CHATIBE rename demo."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import Tk, StringVar, filedialog, messagebox
from tkinter import ttk

from rename_documents import (
    DocumentInfo,
    RapidOCR,
    annotate_wms_links,
    collect_inputs,
    process_pdf_batch,
    write_reports,
)


class RenameApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("CHATIBE 单据自动拆分与改名")
        self.root.geometry("980x610")
        self.root.minsize(800, 500)

        self.inputs: list[Path] = []
        self.output_var = StringVar()
        self.count_var = StringVar(value="尚未选择文件")
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.worker: threading.Thread | None = None

        self._build_ui()
        self.root.after(100, self._drain_ui_queue)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(4, weight=1)

        title = ttk.Label(
            container,
            text="KLN Distribution Receipt / Delivery Note 自动拆分与改名",
            font=("", 16, "bold"),
        )
        title.grid(row=0, column=0, sticky="w", pady=(0, 12))

        source_frame = ttk.LabelFrame(container, text="1. 选择待处理 PDF", padding=10)
        source_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        source_frame.columnconfigure(0, weight=1)
        ttk.Label(source_frame, textvariable=self.count_var).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            source_frame,
            text="选择 PDF 文件",
            command=self._choose_files,
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            source_frame,
            text="选择文件夹",
            command=self._choose_folder,
        ).grid(row=0, column=2, padx=(8, 0))

        output_frame = ttk.LabelFrame(container, text="2. 选择改名文件保存位置", padding=10)
        output_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        output_frame.columnconfigure(0, weight=1)
        ttk.Entry(output_frame, textvariable=self.output_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            output_frame,
            text="浏览",
            command=self._choose_output,
        ).grid(row=0, column=1, padx=(8, 0))

        action_frame = ttk.Frame(container)
        action_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.start_button = ttk.Button(
            action_frame,
            text="开始拆分、识别并改名",
            command=self._start,
        )
        self.start_button.pack(side="left")
        ttk.Button(
            action_frame,
            text="打开输出文件夹",
            command=self._open_output,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            action_frame,
            text="默认复制原文件；已有同名但内容不同的文件不会被覆盖。",
            foreground="#555555",
        ).pack(side="left", padx=(16, 0))

        result_frame = ttk.LabelFrame(container, text="处理结果", padding=8)
        result_frame.grid(row=4, column=0, sticky="nsew")
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)

        columns = ("source", "type", "identifier", "target", "status")
        self.tree = ttk.Treeview(
            result_frame,
            columns=columns,
            show="headings",
            height=12,
        )
        self.tree.heading("source", text="原文件")
        self.tree.heading("type", text="类型")
        self.tree.heading("identifier", text="识别字段")
        self.tree.heading("target", text="新文件名")
        self.tree.heading("status", text="状态")
        self.tree.column("source", width=150)
        self.tree.column("type", width=145)
        self.tree.column("identifier", width=245)
        self.tree.column("target", width=240)
        self.tree.column("status", width=90)

        scrollbar = ttk.Scrollbar(
            result_frame,
            orient="vertical",
            command=self.tree.yview,
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.status_var = StringVar(value="准备就绪")
        ttk.Label(container, textvariable=self.status_var).grid(
            row=5, column=0, sticky="w", pady=(8, 0)
        )

    def _choose_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="选择扫描 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        self.inputs = [Path(value) for value in selected]
        self._set_default_output(self.inputs[0].parent / "renamed")
        self.count_var.set(f"已选择 {len(self.inputs)} 个 PDF 文件")

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="选择包含扫描 PDF 的文件夹")
        if not selected:
            return
        try:
            self.inputs = collect_inputs([selected], recursive=False)
        except Exception as exc:
            messagebox.showerror("选择失败", str(exc))
            return
        if not self.inputs:
            messagebox.showwarning("没有 PDF", "所选文件夹中没有 PDF 文件。")
            return
        self._set_default_output(Path(selected) / "renamed")
        self.count_var.set(f"已选择 {len(self.inputs)} 个 PDF 文件")

    def _set_default_output(self, path: Path) -> None:
        if not self.output_var.get().strip():
            self.output_var.set(str(path))

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择输出文件夹")
        if selected:
            self.output_var.set(selected)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.inputs:
            messagebox.showwarning("尚未选择文件", "请先选择 PDF 文件或文件夹。")
            return
        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showwarning("尚未选择输出位置", "请选择输出文件夹。")
            return

        output_dir = Path(output_text)
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.start_button.configure(state="disabled")
        self.status_var.set("正在初始化本地 OCR，首次运行可能需要几秒……")

        self.worker = threading.Thread(
            target=self._run_batch,
            args=(list(self.inputs), output_dir),
            daemon=True,
        )
        self.worker.start()

    def _run_batch(self, inputs: list[Path], output_dir: Path) -> None:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            ocr = RapidOCR()
            records: list[DocumentInfo] = []
            planned_targets: dict[Path, Path] = {}

            for index, source in enumerate(inputs, start=1):
                self.ui_queue.put(
                    ("status", f"正在处理 {index}/{len(inputs)}：{source.name}")
                )
                source_records = process_pdf_batch(
                    source=source,
                    output_dir=output_dir,
                    ocr=ocr,
                    dpi=250,
                    move=False,
                    planned_targets=planned_targets,
                )
                records.extend(source_records)
                for record in source_records:
                    self.ui_queue.put(("row", record))

            annotate_wms_links(records)
            write_reports(output_dir, records)
            failures = sum(record.status == "ERROR" for record in records)
            self.ui_queue.put(
                (
                    "done",
                    f"完成：{len(records) - failures} 个成功，{failures} 个失败。"
                    f"报告已保存到 {output_dir}",
                )
            )
        except Exception as exc:
            self.ui_queue.put(("error", str(exc)))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                message = self.ui_queue.get_nowait()
                if message[0] == "status":
                    self.status_var.set(message[1])
                elif message[0] == "row":
                    self._add_row(message[1])
                elif message[0] == "done":
                    self.status_var.set(message[1])
                    self.start_button.configure(state="normal")
                    messagebox.showinfo("处理完成", message[1])
                elif message[0] == "error":
                    self.status_var.set(f"处理失败：{message[1]}")
                    self.start_button.configure(state="normal")
                    messagebox.showerror("处理失败", message[1])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def _add_row(self, record: DocumentInfo) -> None:
        identifier = (
            record.distribution_receipt_no
            or record.customer_so
            or ", ".join(record.wms_refs)
        )
        source_label = Path(record.source_file).name
        if record.source_pages:
            source_label = f"{source_label} p.{record.source_pages}"
        self.tree.insert(
            "",
            "end",
            values=(
                source_label,
                record.document_type,
                identifier,
                Path(record.target_file).name if record.target_file else "",
                record.status,
            ),
        )

    def _open_output(self) -> None:
        output_text = self.output_var.get().strip()
        if not output_text or not Path(output_text).exists():
            messagebox.showwarning("文件夹不存在", "请先选择输出文件夹并完成处理。")
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", output_text])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", output_text])
        else:
            subprocess.Popen(["xdg-open", output_text])


def main() -> None:
    root = Tk()
    RenameApp(root)
    root.mainloop()


if __name__ == "__main__":
    # Hidden CLI mode is used for packaging smoke tests and advanced users.
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from rename_documents import main as cli_main

        raise SystemExit(cli_main())
    main()
