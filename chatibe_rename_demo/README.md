# CHATIBE 单据自动改名工具

这个工具会对 KLN 的扫描 PDF 做本地 OCR，并生成正确的文件名：

- `Distribution Receipt`：读取 `No.`，去掉后面的 `-1-L` 等扫描分册后缀。例如
  `1013324217-1-L` 改为 `1013324217.pdf`。
- `Delivery Note`：读取 `Customer SO`。例如
  `SO#HK0001362587` 改为 `SO#HK0001362587.pdf`。
- `Packing List`：读取 `OrderKey` 或 `SO Ref`，命名规则与 Delivery Note
  相同。例如 `SO#HK0001362531` 改为 `SO#HK0001362531.pdf`。

同时会识别 Delivery Note 的 `ETD`、Delivery Note No.，以及 Distribution
Receipt 的 `WMS Ref#`，把 WMS Ref# 与本次批次中的 Delivery Note No. 做关联校验。

一个 PDF 可以由多份单据连续扫描组成。程序会逐页检测单据起始页，按照
`Page x/y` 页脚把续页归入上一份单据，再拆成独立 PDF 并分别改名。拆分只重排
PDF 页面对象，不会重新压缩或降低扫描页面清晰度。

Packing List 如果后面附带同 SO 的 Picking List 或其他 `Page 1/1` 附页，程序
会依据 SO 编号把这些附页归入同一份 Packing List。

## 给同事使用的免安装版本

`rename_gui.py` 是给普通用户使用的图形界面。Windows 管理员只需要在一台制作
机上执行一次 `build_windows.bat`，生成：

```text
dist\CHATIBE_Rename_Tool\CHATIBE_Rename_Tool.exe
```

将整个 `CHATIBE_Rename_Tool` 文件夹压缩后放到公司软件中心或批准的内网共享
位置。其他同事解压并双击 `CHATIBE_Rename_Tool.exe` 即可，**不需要安装
Python**，也不需要安装 OCR、Office 或其他组件。

必须分发整个程序文件夹，不能只发送 EXE 文件；OCR 模型及运行组件包含在同级
目录中。

由于程序内置 PDF 引擎、ONNX Runtime 和本地 OCR 模型，目录大小约为
250～350 MB。压缩后通过公司软件中心或内网共享分发，同事解压到本机后运行
即可，不建议直接从网络共享路径长期运行。

使用方式：

1. 双击程序。
2. 选择一份或多份 PDF，或选择一个文件夹。可以选择一份包含多张单据的合并
   扫描 PDF。
3. 选择改名后的保存位置。
4. 点击“开始拆分、识别并改名”。
5. 在结果表中检查单据类型、识别字段和新文件名。

程序默认复制原文件。已有同名但内容不同的文件不会被覆盖。输出目录还会生成
`rename_report.csv` 和 `rename_report.json`。

正式分发给同事前，建议由 IT 对生成目录中的 EXE 做公司代码签名，否则
Windows SmartScreen 可能显示“未知发布者”。

### 没有 Windows 制作机时的构建方式

PyInstaller 不支持从 macOS 交叉编译 Windows 程序。Mac 只能生成 macOS
可执行文件。

如果公司允许把源代码放到 GitHub 私有仓库，可以在仓库根目录使用已提供的
`.github/workflows/build-windows.yml`：

1. 创建 GitHub 私有仓库，只上传本目录的源码，不要上传客户 PDF。
2. 推送源码后，在 Actions 中选择 `Build Windows portable app`。
3. 等待 Windows 云端构建完成。
4. 下载 `CHATIBE_Rename_Tool_Windows_x64` 构建产物并解压。

构建过程中 Python 只安装在 GitHub 临时 Windows 构建机上，公司电脑不需要
安装 Python。下载下来的程序仍然是免安装文件夹。

如果源代码或业务流程不能放到 GitHub，可选以下方式：

- 公司的 Azure DevOps/Azure Pipelines Windows 托管构建机；
- 公司内部的 Windows 构建服务器；
- 在 Mac 上安装 Windows x64 虚拟机，只在虚拟机内构建，不安装到公司电脑。

在 Apple Silicon Mac 上直接安装 Windows ARM 虚拟机时，Python 和 PyInstaller
通常会生成 ARM64 程序；公司大量电脑是 x64 时不建议使用这种方式，除非已经
确认所有目标电脑都是 Windows ARM64。

## 开发环境

- Python 3.10 或以上
- 本地 OCR：RapidOCR / ONNX Runtime
- PDF 渲染：PyMuPDF

只有在制作免安装程序或修改源码时，制作机才需要安装 Python：

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

图形界面源码运行：

```bash
python rename_gui.py
```

## 命令行运行

默认复制并改名，不改动原始扫描件：

```bash
python rename_documents.py 3816_001.pdf 3817_001.pdf 3818_001.pdf -o ./renamed
```

也可以传入一个目录：

```bash
python rename_documents.py "/path/to/downloaded/attachments" -o "./renamed"
```

确认结果无误后，如果希望直接移动并改名，可以添加 `--move`：

```bash
python rename_documents.py "/path/to/downloaded/attachments" -o "./renamed" --move
```

输出目录会生成：

- 已改名的 PDF 文件；
- `rename_report.csv`：便于用 Excel 查看；
- `rename_report.json`：便于后续自动化程序读取。

合并扫描示例：

```bash
python rename_documents.py combined_scan.pdf -o ./renamed
```

结果会按照扫描顺序生成，例如：

```text
1013324217.pdf         第 1 页
SO#HK0001362587.pdf    第 2-3 页
STOHK0000010476.pdf    第 4-5 页
```

Packing List 混合批次示例：

```text
SO#HK0001362531.pdf        第 1-9 页（含同 SO 的 Picking List）
SO#HK0001362414.pdf        第 10 页
SO#HK0001362413.pdf        第 11 页
```

## 样张预期结果

| 原文件 | 类型 | 输出文件 |
|---|---|---|
| `3816_001.pdf` | Distribution Receipt | `1013324217.pdf` |
| `3817_001.pdf` | Delivery Note | `SO#HK0001362587.pdf` |
| `3818_001.pdf` | Delivery Note | `STOHK0000010476.pdf` |

将上述三份 PDF 按顺序合并成一个 5 页 PDF 后，程序仍会输出以上三个文件。

## 当前 Demo 的安全规则

- 先分类，再读取对应版式的字段。
- Delivery Note 的 ETD 或 Customer SO 缺失时不会猜文件名。
- 新单据必须从包含标题的起始页开始；续页不能与新单据的页面交替扫描。
- 如果页脚声明 `Page 1/2`，但批次中缺少第 2 页，程序会拒绝输出残缺文件。
- 输出目录已有同名但内容不同的文件时，不覆盖，状态记为 `ERROR`。
- 同一个输出名称在一个批次中出现两次时，相关文件需要人工检查。
- 默认使用复制模式，建议确认历史样本识别准确后再使用 `--move`。

这是改名阶段 Demo，尚未包含 Outlook 收件、月份文件夹创建、内网共享和
OneDrive 上传。
