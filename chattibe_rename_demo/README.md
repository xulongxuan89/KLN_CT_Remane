# CHATIBE 单据自动改名 Demo

这个演示脚本读取 KLN 扫描 PDF，自动判断它是 Distribution Receipt 还是
Delivery Note，并生成正确文件名：

- Distribution Receipt：使用 `No.` 字段，去掉末尾的 `-1-L` 等扫描分册后缀。
- Delivery Note：使用 `Customer SO` 字段。
- 默认复制原文件，不修改或删除 Outlook 下载的原始 PDF。
- 每页只对第一页做 OCR，最终文件保留完整 PDF 的所有页面。

## 安装

建议使用 Python 3.11 或 3.12。

```bash
python -m venv .venv
```

macOS/Linux：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

首次处理速度会稍慢，因为本地 OCR 模型需要初始化。识别过程不依赖外部 OCR
服务。

## 使用

处理一个目录中的全部 PDF，并将改名副本放入 `renamed`：

```bash
python rename_documents.py "D:\Scans\CHATIBE" -o "D:\Scans\CHATIBE\renamed"
```

也可以只处理指定文件：

```bash
python rename_documents.py \
  "/path/3816_001.pdf" \
  "/path/3817_001.pdf" \
  "/path/3818_001.pdf" \
  -o "/path/renamed"
```

确认结果无误后，如确实要移动原文件而不是复制，可以增加 `--move`。建议在
接入 Outlook 和公司共享目录前一直使用默认复制模式。

## 样张预期结果

| 原文件 | 单据类型 | 最终文件名 |
|---|---|---|
| `3816_001.pdf` | Distribution Receipt | `1013324217.pdf` |
| `3817_001.pdf` | Delivery Note | `SO#HK0001362587.pdf` |
| `3818_001.pdf` | Delivery Note | `STOHK0000100476.pdf` |

同时在输出目录生成 `recognition_manifest.json`，记录识别出的 WMS Ref#、
Delivery Note No.、Customer SO、ETD 和最终文件名。`wms_links` 用于验证
Distribution Receipt 与 Delivery Note 是否匹配。

## 当前 Demo 的边界

- 当前只处理 KLN 这两类固定版式，模板变化时可能需要调整字段定位规则。
- 同名但内容不同的目标文件不会被覆盖，脚本会报错等待人工处理。
- 本阶段不创建年月文件夹、不上传内网共享或 OneDrive、不读取 Outlook。
- 正式接入前应先用历史样本进行影子测试，再增加批次等待、异常通知和审计数据库。
