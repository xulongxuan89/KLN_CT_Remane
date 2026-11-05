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
    assign_regions,
    collect_inputs,
    copy_to_network_destinations,
    create_task_directory,
    move_to_region_folders,
    process_pdf_batch,
    resolve_ct_rename_root,
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
        self.network_var = BooleanVar(value=True)
        self.count_var = StringVar(value="尚未选择文件")
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_task_dir: Path | None = None

        self._build_ui()
        self.output_var.set(str(Path.home() / "Documents" / "CT Rename"))
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

        output_frame = ttk.LabelFrame(
            container,
            text="2. CT Rename 总文件夹（任务时间文件夹自动创建）",
            padding=10,
        )
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
        ttk.Checkbutton(
            action_frame,
            text="完成后复制到内网",
            variable=self.network_var,
        ).pack(side="left", padx=(16, 0))
        ttk.Label(
            action_frame,
            text="本地任务文件会保留；内网失败可重试。",
            foreground="#555555",
        ).pack(side="left", padx=(8, 0))

        result_frame = ttk.LabelFrame(container, text="处理结果", padding=8)
        result_frame.grid(row=4, column=0, sticky="nsew")
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)

        columns = ("source", "type", "region", "identifier", "target", "status")
        self.tree = ttk.Treeview(
            result_frame,
            columns=columns,
            show="headings",
            height=12,
        )
        self.tree.heading("source", text="原文件")
        self.tree.heading("type", text="类型")
        self.tree.heading("region", text="区域")
        self.tree.heading("identifier", text="识别字段")
        self.tree.heading("target", text="新文件名")
        self.tree.heading("status", text="状态")
        self.tree.column("source", width=150)
        self.tree.column("type", width=145)
        self.tree.column("region", width=75)
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
        self.count_var.set(f"已选择 {len(self.inputs)} 个 PDF 文件")

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择 CT Rename 总文件夹")
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
            args=(list(self.inputs), output_dir, self.network_var.get()),
            daemon=True,
        )
        self.worker.start()

    def _run_batch(
        self,
        inputs: list[Path],
        output_dir: Path,
        network_enabled: bool,
    ) -> None:
        try:
            rename_root = resolve_ct_rename_root(output_dir)
            task_dir = create_task_directory(rename_root)
            self.last_task_dir = task_dir
            ocr = RapidOCR()
            records: list[DocumentInfo] = []
            planned_targets: dict[Path, Path] = {}

            for index, source in enumerate(inputs, start=1):
                self.ui_queue.put(
                    ("status", f"正在处理 {index}/{len(inputs)}：{source.name}")
                )
                source_records = process_pdf_batch(
                    source=source,
                    output_dir=task_dir,
                    ocr=ocr,
                    dpi=250,
                    move=False,
                    planned_targets=planned_targets,
                )
                records.extend(source_records)
                for record in source_records:
                    self.ui_queue.put(("row", record))

            annotate_wms_links(records)
            assign_regions(records)
            move_to_region_folders(task_dir, records)
            network_copied, network_failed = copy_to_network_destinations(
                records,
                enabled=network_enabled,
            )
            write_reports(task_dir, records)
            failures = sum(record.status == "ERROR" for record in records)
            self.ui_queue.put(
                (
                    "done",
                    f"完成：{len(records) - failures} 个成功，{failures} 个失败。"
                    f"内网复制：{network_copied} 成功，{network_failed} 失败。"
                    f"任务文件夹：{task_dir}",
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
                record.region,
                identifier,
                Path(record.target_file).name if record.target_file else "",
                record.status,
            ),
        )

    def _open_output(self) -> None:
        task_dir = self.last_task_dir
        if task_dir is None or not task_dir.exists():
            messagebox.showwarning("文件夹不存在", "请先完成一次处理。")
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(task_dir)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(task_dir)])
        else:
            subprocess.Popen(["xdg-open", str(task_dir)])


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
