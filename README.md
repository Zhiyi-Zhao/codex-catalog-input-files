# codex-catalog-input-files

[中文](#中文) · [English](#english)

`catalog-input-files` is a Codex skill that turns repeated input-file inspection into reusable, task-local documentation.

---

## 中文

### 为什么发布这个仓库

当 Agent 开始数据分析、代码生成或文件转换任务时，往往会先重复执行同一批检查：

- 文件是什么格式？
- 表格有多少列，各列是什么类型？
- JSON、YAML 或 R 对象如何嵌套？
- Excel 有哪些工作表？
- 哪些字段存在缺失？
- 文件是否已经变化？

这些检查本身很有必要，但如果每个会话、每个 Agent 都重新执行，会产生三个问题：

1. **浪费时间和上下文**：相同输入文件被反复读取，真正用于解决任务的上下文反而减少。
2. **污染正式代码**：临时的 `head()`、`str()`、`read_csv()`、字段打印和调试逻辑容易留在最终脚本中。
3. **难以复用知识**：即使同一文件在另一个任务或会话中再次使用，之前发现的数据结构通常没有被保存。

这个仓库发布 `catalog-input-files` 技能，把上述检查集中为一个可复用的预检层。技能为每个大型任务维护独立的 `.file-catalog`，生成简洁的 Markdown 说明和 SQLite 检索索引。后续 Agent 可以先读取说明，只在文件新增或变化时重新解析。

### 为什么每个任务拥有自己的目录

解释文档保存在 `<task-root>/.file-catalog`，而不是 Codex 的全局目录，原因是：

- 数据说明与对应任务一起移动、备份和归档。
- 不同任务不会互相污染索引。
- 任务内相对路径保持稳定，整个项目目录移动后仍能匹配。
- Markdown 说明可以选择性纳入版本控制。
- SQLite、锁文件和临时文件默认被 `.file-catalog/.gitignore` 排除。

### 隐私设计

技能的目标是保存“结构知识”，不是复制数据。生成的说明允许包含：

- 文件名、格式、大小、修改时间和 SHA-256；
- 字段名、键名、工作表名、函数名、类名和 R 对象名；
- 类型、维度、缺失量、样本内近似唯一值数量；
- PDF 页数、图片尺寸和文档结构计数。

说明不会保存：

- 原始数据行或单元格样例；
- 正文段落或源代码片段；
- 类别的实际值或高频值；
- 图片像素、PDF 正文或文档单元格内容。

解析器可能在内存中读取有界样本以推断结构，但不会把样本值写入目录。

### 支持的文件

| 类别 | 格式 | 说明 |
|---|---|---|
| 分隔表格 | CSV、TSV、TAB | 采样字段类型、缺失率和近似唯一值数量 |
| 工作簿 | XLSX、XLS、XLSM、ODS | 工作表、字段和每表有界样本结构 |
| 列式数据 | Parquet、Feather、Arrow IPC | 使用元数据读取字段、行组和记录批次 |
| 结构化文本 | JSON、JSONL、NDJSON、YAML、TOML | 键、嵌套层级和元素类型 |
| R 数据 | RDS、RDA、RData | 对象、类、维度、列、列表成员和缺失量 |
| 代码与文本 | Python、R、JS/TS、Markdown 及常见文本 | 编码、行数、声明、导入和标题结构 |
| 文档与媒体 | PDF、DOCX、常见图片 | 页面、段落、表格、尺寸和元数据键 |
| 其他 | 未知文本或二进制 | 至少生成 MIME 类型和基础元数据说明 |

解析库缺失或文件无法深度读取时，技能会生成带有 `unsupported` 或 `error` 状态的通用说明，而不是静默失败。

### 环境要求

- Python 3.9 或更高版本。
- 核心索引、通用文本和 Python 代码解析只依赖标准库。
- 完整格式支持建议安装：

```bash
python -m pip install pandas openpyxl pyarrow PyYAML pypdf Pillow tomli
```

- RDS/RData 深度解析需要：
  - `Rscript`；
  - R 包 `jsonlite`。

Rscript 的发现顺序是：

1. 环境变量 `R_SCRIPT_EXE`；
2. 系统 `PATH` 中的 `Rscript`；
3. Windows 常见 R 安装目录。

### 安装

先克隆仓库：

```bash
git clone https://github.com/<your-account>/codex-catalog-input-files.git
cd codex-catalog-input-files
```

Windows PowerShell：

```powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$destination = Join-Path $codexHome "skills\catalog-input-files"
Copy-Item -LiteralPath ".\skills\catalog-input-files" -Destination $destination -Recurse
```

macOS/Linux：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/catalog-input-files "${CODEX_HOME:-$HOME/.codex}/skills/"
```

安装后请启动一个新的 Codex 任务，使技能列表重新加载。

### 在 Codex 中使用

显式调用示例：

```text
使用 $catalog-input-files，先为这个任务目录中的输入文件建立说明，然后再设计分析代码。
```

技能也允许隐式触发：当任务涉及读取、分析、转换或基于本地文件编写代码时，Codex 可以先执行预检。

#### 选择任务根目录

`--task-root` 应指向包含当前大型任务全部输入、脚本和输出的最高合理目录。

- 优先使用用户明确指定的任务目录。
- 未指定时使用当前工作目录。
- 不自动向上搜索 Git 根目录。
- 所有待建档文件必须位于任务根目录内。

#### 首次建档

以下示例中的 `<skill-dir>` 是已安装的 `catalog-input-files` 技能目录。

Windows PowerShell：

```powershell
python "<skill-dir>\scripts\file_catalog.py" catalog --task-root "D:\projects\my-task"
```

macOS/Linux：

```bash
python "<skill-dir>/scripts/file_catalog.py" catalog --task-root "/work/my-task"
```

不提供具体文件时会递归处理整个任务目录，并跳过 `.git`、`.file-catalog`、依赖目录、虚拟环境和缓存目录。

#### 开始任务前查询

Windows PowerShell：

```powershell
python "<skill-dir>\scripts\file_catalog.py" lookup `
  --task-root "D:\projects\my-task" `
  "data\observations.csv"
```

macOS/Linux：

```bash
python "<skill-dir>/scripts/file_catalog.py" lookup \
  --task-root "/work/my-task" \
  "data/observations.csv"
```

如果返回 `fresh`，Agent 应优先读取返回的 Markdown 文档，而不是再次探查源文件。

#### 刷新增或变化的文件

```bash
python "<skill-dir>/scripts/file_catalog.py" catalog \
  --task-root "/work/my-task" \
  "data/observations.csv"
```

技能先比较文件大小和高精度修改时间；只有缺失或变化的条目才重新计算 SHA-256 并解析。

#### 跨子目录搜索

```bash
python "<skill-dir>/scripts/file_catalog.py" search \
  --task-root "/work/my-task" \
  "species"
```

搜索范围包括相对路径、文件名、格式、摘要、字段名、键名和其他结构标识符。

### `.file-catalog` 的结构

```text
<task-root>/
└── .file-catalog/
    ├── INDEX.md
    ├── documents/
    │   └── <relative-path-hash>.md
    ├── catalog.sqlite3
    └── .gitignore
```

- `INDEX.md`：适合人工浏览的紧凑索引。
- `documents/`：每个任务相对路径对应一份当前说明。
- `catalog.sqlite3`：供 `lookup` 和 `search` 使用的机器索引。
- `.gitignore`：只排除 SQLite、锁和临时文件，Markdown 可以提交。

### 状态含义

| 状态 | 含义 | 推荐动作 |
|---|---|---|
| `fresh` | 说明存在且文件大小/修改时间一致 | 直接读取说明 |
| `stale` | 源文件自上次解析后发生变化 | 运行 `catalog` 刷新 |
| `missing` | 文件或说明不存在 | 检查路径；存在源文件时运行 `catalog` |
| `unsupported` | 没有深度解析器，但已有通用元数据 | 使用现有说明，必要时人工检查 |
| `error` | 深度解析失败，并已生成降级说明 | 阅读警告，修复依赖或文件问题后刷新 |

### 示例结果

CSV/Excel 说明中的结构类似：

```json
{
  "column_count": 3,
  "sample_rows": 1000,
  "columns": [
    {"name": "species", "dtype": "object", "missing_percent_in_sample": 1.2},
    {"name": "abundance", "dtype": "int64", "missing_percent_in_sample": 0.0}
  ]
}
```

Parquet 说明使用文件元数据，不需要物化数据行：

```json
{
  "row_count": 125000,
  "row_group_count": 4,
  "columns": [
    {"name": "abundance", "type": "int64", "nullable": true}
  ]
}
```

RDS/RData 说明只保留对象结构：

```json
{
  "format": "RData",
  "objects": {
    "otu_table": {
      "class": "data.frame",
      "dimensions": [1200, 18],
      "column_count": 18
    }
  }
}
```

这些示例是结构示意，不包含真实输入数据。

### 推荐工作流

```text
确定 task root
  → lookup 输入文件
  → fresh：读取说明
  → missing/stale：catalog 后读取说明
  → 只有说明不足时才检查原文件
  → 将实际业务逻辑写入正式代码
```

不要把一次性的字段打印、样本输出和格式探测重新写入生产脚本。

### 常见问题

**为什么没有精确统计 CSV 总行数？**  
默认使用有界样本，以避免为结构说明完整扫描超大文本表格。文档会明确标注采样范围。

**为什么某个 Excel 文件显示 `error`？**  
旧式 XLS 或特殊工作簿可能需要额外解析库。安装对应 pandas 引擎后重新运行 `catalog`。

**为什么 TOML 降级？**  
Python 3.11+ 内置 `tomllib`；Python 3.9/3.10 需要安装 `tomli`。

**为什么 R 数据无法解析？**  
确认 `Rscript` 可用，并运行 `Rscript -e "install.packages('jsonlite')"`。也可以设置 `R_SCRIPT_EXE` 为可执行文件路径。

**可以提交 `.file-catalog` 吗？**  
可以提交 `INDEX.md` 和 `documents/`。SQLite 和临时文件默认被目录内 `.gitignore` 排除。

**会不会把隐私数据写入说明？**  
设计上不会保存原始行、单元格、正文或类别值。对于高度敏感的数据，仍建议在提交生成的 Markdown 前进行组织自己的安全审查。

---

## English

### Why this repository exists

Before an agent can analyze data, generate code, or transform files, it usually repeats the same discovery work:

- What format is this file?
- Which columns exist, and what are their types?
- How are JSON, YAML, or R objects nested?
- Which worksheets are present?
- Where are values missing?
- Has the file changed since the previous task?

Those checks are necessary, but repeating them in every session creates three problems:

1. **Time and context are wasted.** The same inputs are reopened while less context remains for the actual task.
2. **Production code becomes noisy.** Temporary `head()`, `str()`, `read_csv()`, schema prints, and debugging logic leak into final scripts.
3. **Knowledge is not reusable.** A later agent or session usually has to rediscover the same structure.

This repository publishes the `catalog-input-files` Codex skill as a reusable preflight layer. Each large task receives its own `.file-catalog` with concise Markdown explanations and a SQLite search index. Later agents can reuse those explanations and reparse only files that are new or stale.

### Why catalogs are task-local

Generated documentation lives under `<task-root>/.file-catalog`, not in a global Codex directory:

- Explanations move, archive, and back up with the task.
- Independent tasks cannot contaminate one another's indexes.
- Task-relative paths remain stable when the whole directory moves.
- Markdown explanations can be version-controlled when useful.
- SQLite, locks, and temporary files are ignored by the generated `.gitignore`.

### Privacy model

The skill stores structural knowledge, not a copy of the data. Explanations may contain:

- file names, formats, sizes, timestamps, and SHA-256 hashes;
- column, key, worksheet, function, class, and R object names;
- types, dimensions, missing counts, and approximate distinct counts within a sample;
- PDF page counts, image dimensions, and document structure counts.

They intentionally omit:

- raw rows and cell samples;
- paragraph excerpts and source-code snippets;
- actual category values and frequency lists;
- image pixels, PDF text, and document cell contents.

Parsers may inspect a bounded in-memory sample to infer structure, but sample values are not written to the catalog.

### Supported files

| Category | Formats | Structural information |
|---|---|---|
| Delimited tables | CSV, TSV, TAB | Sampled types, missing rates, and approximate distinct counts |
| Workbooks | XLSX, XLS, XLSM, ODS | Worksheets, fields, and bounded per-sheet samples |
| Columnar data | Parquet, Feather, Arrow IPC | Schema, row groups, and record batches from metadata |
| Structured text | JSON, JSONL, NDJSON, YAML, TOML | Keys, nesting, and element types |
| R data | RDS, RDA, RData | Objects, classes, dimensions, columns, members, and missing counts |
| Code and text | Python, R, JS/TS, Markdown, common text | Encoding, lines, declarations, imports, and headings |
| Documents and media | PDF, DOCX, common images | Pages, paragraphs, tables, dimensions, and metadata keys |
| Other | Unknown text or binary | MIME type and generic file metadata |

When an optional parser is unavailable, the skill produces a useful `unsupported` or `error` explanation with explicit warnings.

### Requirements

- Python 3.9 or newer.
- Core indexing, generic text inspection, and Python source analysis use the standard library.
- For full format coverage, install:

```bash
python -m pip install pandas openpyxl pyarrow PyYAML pypdf Pillow tomli
```

- Deep RDS/RData inspection requires `Rscript` and the R package `jsonlite`.

Rscript is resolved from:

1. `R_SCRIPT_EXE`;
2. `Rscript` on `PATH`;
3. common Windows R installation directories.

### Installation

Clone the repository:

```bash
git clone https://github.com/<your-account>/codex-catalog-input-files.git
cd codex-catalog-input-files
```

Windows PowerShell:

```powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$destination = Join-Path $codexHome "skills\catalog-input-files"
Copy-Item -LiteralPath ".\skills\catalog-input-files" -Destination $destination -Recurse
```

macOS/Linux:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/catalog-input-files "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Start a new Codex task after installation so the skill list reloads.

### Using the skill in Codex

Explicit invocation:

```text
Use $catalog-input-files to catalog this task folder before designing code against its inputs.
```

The skill also allows implicit invocation when a task involves reading, analyzing, transforming, or writing code against local files.

#### Choose a task root

`--task-root` should be the highest sensible directory that contains the inputs, scripts, and outputs for one large task.

- Prefer a root explicitly provided by the user.
- Otherwise use the current working directory.
- Do not infer a Git root.
- Every cataloged path must remain inside the task root.

#### Create the first catalog

`<skill-dir>` below means the installed `catalog-input-files` skill directory.

Windows PowerShell:

```powershell
python "<skill-dir>\scripts\file_catalog.py" catalog --task-root "D:\projects\my-task"
```

macOS/Linux:

```bash
python "<skill-dir>/scripts/file_catalog.py" catalog --task-root "/work/my-task"
```

With no explicit path, `catalog` recursively processes the task while excluding `.git`, `.file-catalog`, dependency folders, virtual environments, and caches.

#### Look up an input before starting work

Windows PowerShell:

```powershell
python "<skill-dir>\scripts\file_catalog.py" lookup `
  --task-root "D:\projects\my-task" `
  "data\observations.csv"
```

macOS/Linux:

```bash
python "<skill-dir>/scripts/file_catalog.py" lookup \
  --task-root "/work/my-task" \
  "data/observations.csv"
```

When the result is `fresh`, read the returned Markdown explanation instead of probing the source again.

#### Refresh a new or changed file

```bash
python "<skill-dir>/scripts/file_catalog.py" catalog \
  --task-root "/work/my-task" \
  "data/observations.csv"
```

The skill first compares file size and high-resolution modification time. It recalculates SHA-256 and reparses only missing or changed entries.

#### Search across subdirectories

```bash
python "<skill-dir>/scripts/file_catalog.py" search \
  --task-root "/work/my-task" \
  "species"
```

Search covers relative paths, file names, formats, summaries, fields, keys, and other structural identifiers.

### `.file-catalog` layout

```text
<task-root>/
└── .file-catalog/
    ├── INDEX.md
    ├── documents/
    │   └── <relative-path-hash>.md
    ├── catalog.sqlite3
    └── .gitignore
```

- `INDEX.md`: compact index for human browsing.
- `documents/`: one current explanation per task-relative path.
- `catalog.sqlite3`: machine index used by `lookup` and `search`.
- `.gitignore`: ignores SQLite, locks, and temporary files while leaving Markdown trackable.

### Status reference

| Status | Meaning | Recommended action |
|---|---|---|
| `fresh` | Explanation exists and size/mtime still match | Read the explanation |
| `stale` | Source changed after the last analysis | Run `catalog` |
| `missing` | Source or explanation is absent | Check the path, then catalog an existing source |
| `unsupported` | No deep parser matched, but generic metadata exists | Use the metadata or inspect manually if required |
| `error` | Deep parsing failed and a fallback explanation was produced | Read the warning, fix dependencies or the file, then refresh |

### Example structures

A CSV or Excel explanation may contain:

```json
{
  "column_count": 3,
  "sample_rows": 1000,
  "columns": [
    {"name": "species", "dtype": "object", "missing_percent_in_sample": 1.2},
    {"name": "abundance", "dtype": "int64", "missing_percent_in_sample": 0.0}
  ]
}
```

Parquet structure comes from metadata without materializing data rows:

```json
{
  "row_count": 125000,
  "row_group_count": 4,
  "columns": [
    {"name": "abundance", "type": "int64", "nullable": true}
  ]
}
```

RDS/RData explanations preserve object shape only:

```json
{
  "format": "RData",
  "objects": {
    "otu_table": {
      "class": "data.frame",
      "dimensions": [1200, 18],
      "column_count": 18
    }
  }
}
```

These are structural illustrations, not real input records.

### Recommended workflow

```text
Choose task root
  → lookup the inputs
  → fresh: read explanations
  → missing/stale: catalog, then read explanations
  → inspect raw sources only if the explanations are insufficient
  → keep production code focused on the actual task
```

Do not reintroduce one-off schema prints, sample dumps, or format probes into production scripts.

### Troubleshooting and FAQ

**Why is the exact CSV row count missing?**  
Delimited files use bounded sampling by default so a structural preflight does not fully scan a very large text table. The explanation records the sampling scope.

**Why does an Excel file report `error`?**  
Legacy XLS files or specialized workbooks may require an additional pandas engine. Install the relevant engine and rerun `catalog`.

**Why did TOML fall back?**  
Python 3.11+ includes `tomllib`; Python 3.9/3.10 require `tomli`.

**Why does R data inspection fail?**  
Confirm that `Rscript` is available and run `Rscript -e "install.packages('jsonlite')"`. You may also set `R_SCRIPT_EXE` to the executable path.

**Can `.file-catalog` be committed?**  
Yes. Commit `INDEX.md` and `documents/` when useful. SQLite and temporary files are ignored by the generated `.gitignore`.

**Can private values leak into explanations?**  
The implementation intentionally omits rows, cells, paragraph text, category values, and code snippets. Organizations handling highly sensitive data should still review generated Markdown before publishing it.

## License

MIT. See [LICENSE](LICENSE).
