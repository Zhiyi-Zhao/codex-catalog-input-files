#!/usr/bin/env python
"""
===============================================================================
代码介绍
===============================================================================
输入：
  1. 子命令 catalog、lookup 或 search。
  2. --task-root 指定“大任务”根目录；省略时使用当前工作目录。
  3. catalog/lookup 接受任务根目录内的文件或目录路径；search 接受检索词。

输出：
  - 标准输出只返回受 --limit 限制的简短状态行，适合 Agent 直接读取。
  - catalog 在 <task-root>/.file-catalog/ 中写入：
      INDEX.md
      documents/<任务相对路径哈希>.md
      catalog.sqlite3
      .gitignore
  - Markdown 说明包含路径、格式、结构、统计和解析限制，不保存数据行、单元格样例、
    正文段落、类别值或源代码片段。

作用：
  把每次任务都会重复出现的“先检查输入文件”步骤封装为可复用工具。Agent 先调用
  lookup；说明缺失或过期时再调用 catalog；之后直接使用说明文档完成任务设计，
  避免在生产脚本中混入冗长且不可复用的文件探查代码。

设计逻辑：
  - 以任务内相对路径作为稳定身份，因此整个任务文件夹移动后仍能匹配。
  - 以大小和 mtime_ns 快速判断新鲜度；需要更新时再流式计算 SHA-256。
  - 同一任务内 SHA-256 相同的文件复用已有结构结果。
  - 解析器按格式分层；缺少依赖或格式未知时生成通用说明和明确警告。
  - 所有读取均采用流式读取或有界采样；R 数据由同目录 inspect_r_data.R 处理。
  - SQLite 使用 WAL、busy_timeout 和事务；Markdown 使用临时文件 + os.replace 原子写入。

主要函数：
  resolve_task_root()      解析并验证任务根目录。
  gather_files()           递归收集任务文件并应用排除规则。
  sha256_file()            流式计算内容哈希。
  analyze_file()           按扩展名路由到具体解析器。
  catalog_files()          增量建档、内容复用、缺失标记和索引更新。
  lookup_files()           判断说明的新鲜度。
  search_catalog()         查询任务内 SQLite 索引。
  render_document()        生成不含原始样例的 Markdown 说明。
  render_index()           生成可跟踪的任务级 INDEX.md。
  main()                   解析命令行并调度子命令。

调用方式：
  python file_catalog.py catalog --task-root "D:\\project"
  python file_catalog.py catalog --task-root "D:\\project" "data\\input.csv"
  python file_catalog.py lookup --task-root "D:\\project" "data\\input.csv"
  python file_catalog.py search --task-root "D:\\project" "species"
===============================================================================
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree


CATALOG_VERSION = 1
SAMPLE_BYTES = 2 * 1024 * 1024
TABLE_SAMPLE_ROWS = 1000
MAX_STRUCTURAL_ITEMS = 200
MAX_JSON_BYTES = 64 * 1024 * 1024
EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".file-catalog",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "dist",
    "build",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".log",
    ".sql",
    ".ini",
    ".cfg",
    ".conf",
}
CODE_EXTENSIONS = {
    ".py",
    ".r",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".sh",
    ".ps1",
}


@dataclass
class Analysis:
    format_name: str
    analyzer: str
    status: str
    language: str
    summary_zh: str
    summary_en: str
    structure: Dict[str, Any]
    warnings: List[str]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def yaml_quote(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def clean_structural_name(value: Any, limit: int = 160) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    return text[:limit]


def detect_language(text: str) -> str:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk >= 4 and cjk >= max(1, int(latin * 0.12)):
        return "zh"
    if latin >= 8:
        return "en"
    return "zh"


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_task_root(value: Optional[str]) -> Path:
    root = Path(value).expanduser() if value else Path.cwd()
    root = root.resolve()
    if not root.exists():
        raise ValueError(f"Task root does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"Task root is not a directory: {root}")
    return root


def resolve_input_path(raw: str, task_root: Path) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = task_root / candidate
    candidate = candidate.resolve(strict=False)
    if not is_within(candidate, task_root):
        raise ValueError(f"Input is outside task root: {candidate}")
    return candidate


def relative_key(path: Path, task_root: Path) -> str:
    return path.relative_to(task_root).as_posix()


def document_id(relative_path: str) -> str:
    return hashlib.sha256(relative_path.casefold().encode("utf-8")).hexdigest()[:24]


def gather_files(
    task_root: Path,
    raw_paths: Sequence[str],
) -> Tuple[List[Path], List[Dict[str, str]], bool]:
    full_scan = len(raw_paths) == 0
    requested = [task_root] if full_scan else [resolve_input_path(x, task_root) for x in raw_paths]
    files: Dict[str, Path] = {}
    issues: List[Dict[str, str]] = []

    for requested_path in requested:
        if not requested_path.exists():
            issues.append(
                {
                    "status": "missing",
                    "source": str(requested_path),
                    "relative_path": (
                        relative_key(requested_path, task_root)
                        if is_within(requested_path, task_root)
                        else str(requested_path)
                    ),
                    "document": "",
                }
            )
            continue

        if requested_path.is_file():
            key = relative_key(requested_path, task_root)
            files[key.casefold()] = requested_path
            continue

        for current_root, directory_names, file_names in os.walk(
            str(requested_path), followlinks=False
        ):
            current = Path(current_root)
            directory_names[:] = [
                name
                for name in directory_names
                if name.casefold() not in EXCLUDED_DIR_NAMES
                and not (current / name).is_symlink()
            ]
            for file_name in file_names:
                candidate = current / file_name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                key = relative_key(candidate.resolve(), task_root)
                files[key.casefold()] = candidate.resolve()

    ordered = sorted(files.values(), key=lambda p: relative_key(p, task_root).casefold())
    return ordered, issues, full_scan


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_text_sample(path: Path, max_bytes: int = SAMPLE_BYTES) -> Tuple[Optional[str], str, bool]:
    raw = path.open("rb").read(max_bytes)
    truncated = path.stat().st_size > len(raw)
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None, "binary", truncated

    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            text = raw.decode(encoding)
            printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
            if not text or printable / max(1, len(text)) >= 0.8:
                return text, encoding, truncated
        except UnicodeDecodeError:
            continue

    try:
        text = raw.decode("latin-1")
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
        if not text or printable / max(1, len(text)) >= 0.9:
            return text, "latin-1", truncated
    except UnicodeDecodeError:
        pass
    return None, "binary", truncated


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def structural_schema(value: Any, depth: int = 0) -> Dict[str, Any]:
    result: Dict[str, Any] = {"type": json_type(value)}
    if depth >= 4:
        result["truncated_depth"] = True
        return result

    if isinstance(value, dict):
        keys = list(value.keys())
        selected = keys[:MAX_STRUCTURAL_ITEMS]
        result["key_count"] = len(keys)
        result["keys"] = {
            clean_structural_name(key): structural_schema(value[key], depth + 1)
            for key in selected
        }
        result["truncated_keys"] = len(keys) > len(selected)
    elif isinstance(value, list):
        sample = value[:100]
        result["item_count"] = len(value)
        result["sampled_item_count"] = len(sample)
        types = sorted({json_type(item) for item in sample})
        result["item_types"] = types
        if sample:
            representatives: Dict[str, Any] = {}
            for item in sample:
                kind = json_type(item)
                if kind not in representatives:
                    representatives[kind] = structural_schema(item, depth + 1)
            result["item_schemas"] = representatives
    return result


def column_structure(frame: Any) -> List[Dict[str, Any]]:
    columns: List[Dict[str, Any]] = []
    for name in list(frame.columns)[:MAX_STRUCTURAL_ITEMS]:
        series = frame[name]
        non_missing = series.dropna()
        columns.append(
            {
                "name": clean_structural_name(name),
                "dtype": str(series.dtype),
                "missing_in_sample": int(series.isna().sum()),
                "missing_percent_in_sample": round(float(series.isna().mean() * 100), 3),
                "approximate_unique_in_sample": int(non_missing.nunique(dropna=True)),
            }
        )
    return columns


def analyze_delimited(path: Path, separator: str) -> Analysis:
    import pandas as pd

    warnings: List[str] = []
    last_error: Optional[Exception] = None
    frame = None
    encoding = ""
    for candidate_encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            frame = pd.read_csv(
                path,
                sep=separator,
                nrows=TABLE_SAMPLE_ROWS,
                encoding=candidate_encoding,
                low_memory=False,
            )
            encoding = candidate_encoding
            break
        except Exception as error:
            last_error = error
    if frame is None:
        raise RuntimeError(f"Delimited parser failed: {last_error}")

    names = " ".join(clean_structural_name(x) for x in frame.columns)
    language = detect_language(names)
    structure = {
        "encoding": encoding,
        "sample_rows": int(len(frame)),
        "row_count": "not fully counted",
        "column_count": int(len(frame.columns)),
        "columns": column_structure(frame),
        "truncated_columns": len(frame.columns) > MAX_STRUCTURAL_ITEMS,
    }
    warnings.append(
        f"Row and column statistics use at most the first {TABLE_SAMPLE_ROWS} records."
    )
    return Analysis(
        format_name="TSV" if separator == "\t" else "CSV",
        analyzer="pandas-delimited",
        status="fresh",
        language=language,
        summary_zh=f"分隔文本表格；已采样 {len(frame)} 行并识别 {len(frame.columns)} 个字段。",
        summary_en=f"Delimited table; sampled {len(frame)} rows and identified {len(frame.columns)} fields.",
        structure=structure,
        warnings=warnings,
    )


def analyze_excel(path: Path) -> Analysis:
    import pandas as pd

    workbook = pd.ExcelFile(path)
    sheet_names = list(workbook.sheet_names)
    sheets: List[Dict[str, Any]] = []
    language_material: List[str] = sheet_names.copy()
    for sheet_name in sheet_names[:50]:
        frame = workbook.parse(sheet_name=sheet_name, nrows=500)
        language_material.extend(str(x) for x in frame.columns)
        sheets.append(
            {
                "name": clean_structural_name(sheet_name),
                "sample_rows": int(len(frame)),
                "column_count": int(len(frame.columns)),
                "columns": column_structure(frame),
                "truncated_columns": len(frame.columns) > MAX_STRUCTURAL_ITEMS,
            }
        )
    language = detect_language(" ".join(language_material))
    return Analysis(
        format_name="Excel workbook",
        analyzer="pandas-excel",
        status="fresh",
        language=language,
        summary_zh=f"Excel 工作簿；识别 {len(sheet_names)} 个工作表并提取字段结构。",
        summary_en=f"Excel workbook; identified {len(sheet_names)} worksheets and their field structures.",
        structure={
            "sheet_count": len(sheet_names),
            "sheets": sheets,
            "truncated_sheets": len(sheet_names) > len(sheets),
            "statistics_scope": "up to 500 rows per sheet",
        },
        warnings=["Worksheet statistics are sample-based and do not contain cell values."],
    )


def analyze_parquet(path: Path) -> Analysis:
    import pyarrow.parquet as parquet

    metadata = parquet.ParquetFile(path)
    schema = metadata.schema_arrow
    fields = [
        {"name": clean_structural_name(field.name), "type": str(field.type), "nullable": field.nullable}
        for field in list(schema)[:MAX_STRUCTURAL_ITEMS]
    ]
    language = detect_language(" ".join(field["name"] for field in fields))
    return Analysis(
        format_name="Parquet",
        analyzer="pyarrow-parquet-metadata",
        status="fresh",
        language=language,
        summary_zh=f"Parquet 列式数据；元数据记录 {metadata.metadata.num_rows} 行、{len(schema)} 个字段。",
        summary_en=f"Parquet columnar data; metadata reports {metadata.metadata.num_rows} rows and {len(schema)} fields.",
        structure={
            "row_count": metadata.metadata.num_rows,
            "row_group_count": metadata.metadata.num_row_groups,
            "column_count": len(schema),
            "columns": fields,
            "truncated_columns": len(schema) > len(fields),
        },
        warnings=[],
    )


def analyze_feather(path: Path) -> Analysis:
    import pyarrow as pa
    import pyarrow.ipc as ipc

    source = pa.memory_map(str(path), "r")
    reader = ipc.open_file(source)
    schema = reader.schema
    fields = [
        {"name": clean_structural_name(field.name), "type": str(field.type), "nullable": field.nullable}
        for field in list(schema)[:MAX_STRUCTURAL_ITEMS]
    ]
    language = detect_language(" ".join(field["name"] for field in fields))
    return Analysis(
        format_name="Feather/Arrow IPC",
        analyzer="pyarrow-ipc-metadata",
        status="fresh",
        language=language,
        summary_zh=f"Feather/Arrow 文件；识别 {len(schema)} 个字段和 {reader.num_record_batches} 个记录批次。",
        summary_en=f"Feather/Arrow file; identified {len(schema)} fields and {reader.num_record_batches} record batches.",
        structure={
            "record_batch_count": reader.num_record_batches,
            "column_count": len(schema),
            "columns": fields,
            "truncated_columns": len(schema) > len(fields),
        },
        warnings=["Row count was not materialized to avoid loading record batches."],
    )


def analyze_json(path: Path, json_lines: bool) -> Analysis:
    size = path.stat().st_size
    warnings: List[str] = []
    if json_lines:
        records: List[Any] = []
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= TABLE_SAMPLE_ROWS:
                    warnings.append(
                        f"Only the first {TABLE_SAMPLE_ROWS} JSONL records were parsed."
                    )
                    break
                if line.strip():
                    records.append(json.loads(line))
        value: Any = records
        format_name = "JSON Lines"
    else:
        if size > MAX_JSON_BYTES:
            raise RuntimeError(
                f"JSON file exceeds bounded parse limit of {MAX_JSON_BYTES} bytes."
            )
        with path.open("r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
        format_name = "JSON"

    structure = structural_schema(value)
    structural_text = json.dumps(structure, ensure_ascii=False)
    language = detect_language(structural_text)
    return Analysis(
        format_name=format_name,
        analyzer="python-json",
        status="fresh",
        language=language,
        summary_zh=f"{format_name} 结构化数据；已提取键、嵌套层级和元素类型。",
        summary_en=f"{format_name} structured data; extracted keys, nesting, and element types.",
        structure=structure,
        warnings=warnings,
    )


def analyze_yaml(path: Path) -> Analysis:
    import yaml

    if path.stat().st_size > MAX_JSON_BYTES:
        raise RuntimeError(
            f"YAML file exceeds bounded parse limit of {MAX_JSON_BYTES} bytes."
        )
    with path.open("r", encoding="utf-8-sig") as handle:
        value = yaml.safe_load(handle)
    structure = structural_schema(value)
    structural_text = json.dumps(structure, ensure_ascii=False)
    return Analysis(
        format_name="YAML",
        analyzer="pyyaml",
        status="fresh",
        language=detect_language(structural_text),
        summary_zh="YAML 配置或结构化数据；已提取键、嵌套层级和元素类型。",
        summary_en="YAML configuration or structured data; extracted keys, nesting, and element types.",
        structure=structure,
        warnings=[],
    )


def analyze_toml(path: Path) -> Analysis:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise RuntimeError(
            f"TOML file exceeds bounded parse limit of {MAX_JSON_BYTES} bytes."
        )
    try:
        import tomllib as toml_reader  # type: ignore
    except ImportError:
        try:
            import tomli as toml_reader  # type: ignore
        except ImportError as error:
            raise RuntimeError("Neither tomllib nor tomli is available.") from error
    with path.open("rb") as handle:
        value = toml_reader.load(handle)
    structure = structural_schema(value)
    structural_text = json.dumps(structure, ensure_ascii=False)
    return Analysis(
        format_name="TOML",
        analyzer="toml",
        status="fresh",
        language=detect_language(structural_text),
        summary_zh="TOML 配置；已提取节、键和嵌套结构。",
        summary_en="TOML configuration; extracted sections, keys, and nesting.",
        structure=structure,
        warnings=[],
    )


def analyze_python_source(path: Path, text: str, encoding: str, truncated: bool) -> Analysis:
    if truncated:
        raise RuntimeError("Python source exceeds the bounded parser size.")
    tree = ast.parse(text)
    imports: List[str] = []
    functions: List[str] = []
    classes: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
    structure = {
        "encoding": encoding,
        "line_count": len(text.splitlines()),
        "imports": [clean_structural_name(x) for x in imports[:MAX_STRUCTURAL_ITEMS]],
        "functions": [clean_structural_name(x) for x in functions[:MAX_STRUCTURAL_ITEMS]],
        "classes": [clean_structural_name(x) for x in classes[:MAX_STRUCTURAL_ITEMS]],
        "module_docstring_present": ast.get_docstring(tree) is not None,
    }
    return Analysis(
        format_name="Python source",
        analyzer="python-ast",
        status="fresh",
        language=detect_language(text[:10000]),
        summary_zh=f"Python 源代码；识别 {len(functions)} 个函数、{len(classes)} 个类和 {len(imports)} 个导入。",
        summary_en=f"Python source; identified {len(functions)} functions, {len(classes)} classes, and {len(imports)} imports.",
        structure=structure,
        warnings=[],
    )


def analyze_code_or_text(path: Path) -> Analysis:
    text, encoding, truncated = read_text_sample(path, max_bytes=5 * 1024 * 1024)
    if text is None:
        raise RuntimeError("The file did not pass text decoding checks.")
    extension = path.suffix.casefold()
    if extension == ".py":
        return analyze_python_source(path, text, encoding, truncated)

    lines = text.splitlines()
    structure: Dict[str, Any] = {
        "encoding": encoding,
        "sample_line_count": len(lines),
        "sample_truncated": truncated,
    }
    format_name = "Text"
    analyzer = "bounded-text-structure"

    if extension in {".md", ".markdown", ".rst"}:
        headings = [
            clean_structural_name(match.group(2))
            for line in lines
            for match in [re.match(r"^(#{1,6})\s+(.+?)\s*$", line)]
            if match
        ]
        structure["headings"] = headings[:MAX_STRUCTURAL_ITEMS]
        structure["heading_count_in_sample"] = len(headings)
        format_name = "Markdown/text document"
    elif extension == ".r":
        functions = re.findall(
            r"(?m)^\s*([A-Za-z.][A-Za-z0-9._]*)\s*(?:<-|=)\s*function\s*\(",
            text,
        )
        packages = re.findall(
            r"(?m)\b(?:library|require)\s*\(\s*[\"']?([A-Za-z0-9._]+)",
            text,
        )
        structure["functions"] = [
            clean_structural_name(x) for x in functions[:MAX_STRUCTURAL_ITEMS]
        ]
        structure["packages"] = [
            clean_structural_name(x) for x in packages[:MAX_STRUCTURAL_ITEMS]
        ]
        format_name = "R source"
        analyzer = "r-source-structure"
    elif extension in {".js", ".jsx", ".ts", ".tsx"}:
        functions = re.findall(
            r"(?m)\b(?:function|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
            text,
        )
        imports = re.findall(
            r"(?m)^\s*import\s+.*?\s+from\s+[\"']([^\"']+)[\"']",
            text,
        )
        structure["declared_functions_or_classes"] = [
            clean_structural_name(x) for x in functions[:MAX_STRUCTURAL_ITEMS]
        ]
        structure["imports"] = [
            clean_structural_name(x) for x in imports[:MAX_STRUCTURAL_ITEMS]
        ]
        format_name = "JavaScript/TypeScript source"
        analyzer = "js-ts-source-structure"
    elif extension in CODE_EXTENSIONS:
        functions = re.findall(
            r"(?m)^\s*(?:def|func|fn|function|class|struct|interface)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            text,
        )
        structure["declarations"] = [
            clean_structural_name(x) for x in functions[:MAX_STRUCTURAL_ITEMS]
        ]
        format_name = f"{extension.lstrip('.').upper()} source"
        analyzer = "generic-source-structure"

    language = detect_language(text[:20000])
    return Analysis(
        format_name=format_name,
        analyzer=analyzer,
        status="fresh",
        language=language,
        summary_zh=f"{format_name}；已记录编码、行数和可识别的结构性名称。",
        summary_en=f"{format_name}; recorded encoding, line counts, and recognizable structural names.",
        structure=structure,
        warnings=(
            ["Only a bounded prefix was inspected; counts may be incomplete."]
            if truncated
            else []
        ),
    )


def analyze_pdf(path: Path) -> Analysis:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    metadata_keys = sorted(str(key) for key in (reader.metadata or {}).keys())
    first_page_box: Optional[List[float]] = None
    if reader.pages:
        box = reader.pages[0].mediabox
        first_page_box = [float(box.width), float(box.height)]
    return Analysis(
        format_name="PDF",
        analyzer="pypdf-metadata",
        status="fresh",
        language="zh",
        summary_zh=f"PDF 文档；识别 {len(reader.pages)} 页，仅记录页面和元数据结构。",
        summary_en=f"PDF document; identified {len(reader.pages)} pages and recorded metadata structure only.",
        structure={
            "page_count": len(reader.pages),
            "encrypted": bool(reader.is_encrypted),
            "metadata_keys": metadata_keys,
            "first_page_size_points": first_page_box,
        },
        warnings=["Document text was not copied into the catalog."],
    )


def analyze_docx(path: Path) -> Analysis:
    namespaces = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    }
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(document_xml)
        paragraphs = root.findall(".//w:p", namespaces)
        tables = root.findall(".//w:tbl", namespaces)
        heading_styles: Dict[str, int] = {}
        language_material: List[str] = []
        for paragraph in paragraphs[:5000]:
            style = paragraph.find("./w:pPr/w:pStyle", namespaces)
            if style is not None:
                style_name = style.attrib.get(
                    f"{{{namespaces['w']}}}val", ""
                )
                if style_name.lower().startswith("heading"):
                    heading_styles[style_name] = heading_styles.get(style_name, 0) + 1
            for text_node in paragraph.findall(".//w:t", namespaces):
                if text_node.text and sum(len(x) for x in language_material) < 20000:
                    language_material.append(text_node.text)
        names = set(archive.namelist())
    return Analysis(
        format_name="DOCX",
        analyzer="docx-zip-xml",
        status="fresh",
        language=detect_language(" ".join(language_material)),
        summary_zh=f"DOCX 文档；识别 {len(paragraphs)} 个段落和 {len(tables)} 个表格。",
        summary_en=f"DOCX document; identified {len(paragraphs)} paragraphs and {len(tables)} tables.",
        structure={
            "paragraph_count": len(paragraphs),
            "table_count": len(tables),
            "heading_style_counts": heading_styles,
            "has_headers": any(name.startswith("word/header") for name in names),
            "has_footers": any(name.startswith("word/footer") for name in names),
            "embedded_media_count": sum(
                name.startswith("word/media/") for name in names
            ),
        },
        warnings=["Paragraph and cell text was not copied into the catalog."],
    )


def analyze_image(path: Path) -> Analysis:
    from PIL import Image

    with Image.open(path) as image:
        structure = {
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
            "frame_count": getattr(image, "n_frames", 1),
            "metadata_keys": sorted(clean_structural_name(x) for x in image.info.keys()),
        }
        format_name = f"{image.format or path.suffix.lstrip('.').upper()} image"
    return Analysis(
        format_name=format_name,
        analyzer="pillow-metadata",
        status="fresh",
        language="zh",
        summary_zh=f"图像文件；尺寸为 {structure['width']}×{structure['height']}，模式为 {structure['mode']}。",
        summary_en=f"Image file; dimensions are {structure['width']}×{structure['height']} with mode {structure['mode']}.",
        structure=structure,
        warnings=["Pixel content and metadata values were not copied into the catalog."],
    )


def locate_rscript() -> Optional[str]:
    environment_value = os.environ.get("R_SCRIPT_EXE")
    candidates = [
        environment_value,
        shutil.which("Rscript"),
    ]
    if os.name == "nt":
        windows_roots: List[Path] = []
        program_files = os.environ.get("ProgramFiles")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if program_files:
            windows_roots.append(Path(program_files) / "R")
        if local_app_data:
            windows_roots.append(Path(local_app_data) / "Programs" / "R")
        for root in windows_roots:
            if not root.is_dir():
                continue
            versions = sorted(
                (path for path in root.glob("R-*") if path.is_dir()),
                key=lambda path: path.name.casefold(),
                reverse=True,
            )
            for version in versions:
                candidates.extend(
                    [
                        str(version / "bin" / "Rscript.exe"),
                        str(version / "bin" / "x64" / "Rscript.exe"),
                    ]
                )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def analyze_r_data(path: Path) -> Analysis:
    rscript = locate_rscript()
    if not rscript:
        raise RuntimeError("Rscript could not be located.")
    helper = Path(__file__).with_name("inspect_r_data.R")
    completed = subprocess.run(
        [rscript, "--vanilla", str(helper), str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    stdout = completed.stdout[:2_000_000]
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        stderr = completed.stderr[-1000:].strip()
        raise RuntimeError(f"R inspector returned invalid JSON: {stderr}") from error
    if completed.returncode != 0 or payload.get("status") != "ok":
        raise RuntimeError(str(payload.get("message", "R inspector failed.")))
    object_names = list((payload.get("objects") or {}).keys())
    language = detect_language(" ".join(object_names))
    return Analysis(
        format_name=str(payload.get("format", "R data")),
        analyzer="r-structure-helper",
        status="fresh",
        language=language,
        summary_zh=f"R 数据文件；识别 {len(object_names)} 个顶层对象并提取类、维度和字段结构。",
        summary_en=f"R data file; identified {len(object_names)} top-level objects and extracted classes, dimensions, and fields.",
        structure=payload,
        warnings=[str(x) for x in payload.get("warnings", [])],
    )


def generic_analysis(path: Path, reason: Optional[str] = None) -> Analysis:
    mime_type, encoding_hint = mimetypes.guess_type(str(path))
    text, encoding, truncated = read_text_sample(path)
    if text is not None:
        lines = text.splitlines()
        warnings = (
            ["Only a bounded text prefix was inspected."] if truncated else []
        )
        if reason:
            warnings.append(reason)
        return Analysis(
            format_name=mime_type or "Generic text",
            analyzer="generic-text-metadata",
            status="fresh" if reason is None else "error",
            language=detect_language(text[:20000]),
            summary_zh="通用文本文件；已记录编码、样本行数和 MIME 类型。",
            summary_en="Generic text file; recorded encoding, sampled line count, and MIME type.",
            structure={
                "extension": path.suffix,
                "mime_type": mime_type,
                "encoding_hint": encoding_hint,
                "detected_encoding": encoding,
                "sample_line_count": len(lines),
                "sample_truncated": truncated,
            },
            warnings=warnings,
        )

    warnings = ["No deep parser matched; only file metadata was recorded."]
    if reason:
        warnings.append(reason)
    return Analysis(
        format_name=mime_type or "Unknown binary",
        analyzer="generic-binary-metadata",
        status="unsupported" if reason is None else "error",
        language="zh",
        summary_zh="二进制或未知格式文件；仅记录基础元数据。",
        summary_en="Binary or unknown-format file; recorded basic metadata only.",
        structure={
            "extension": path.suffix,
            "mime_type": mime_type,
            "encoding_hint": encoding_hint,
        },
        warnings=warnings,
    )


def analyze_file(path: Path) -> Analysis:
    extension = path.suffix.casefold()
    try:
        if extension == ".csv":
            return analyze_delimited(path, ",")
        if extension in {".tsv", ".tab"}:
            return analyze_delimited(path, "\t")
        if extension in {".xlsx", ".xls", ".xlsm", ".ods"}:
            return analyze_excel(path)
        if extension == ".parquet":
            return analyze_parquet(path)
        if extension in {".feather", ".arrow"}:
            return analyze_feather(path)
        if extension == ".json":
            return analyze_json(path, json_lines=False)
        if extension in {".jsonl", ".ndjson"}:
            return analyze_json(path, json_lines=True)
        if extension in {".yaml", ".yml"}:
            return analyze_yaml(path)
        if extension == ".toml":
            return analyze_toml(path)
        if extension in {".rds", ".rda", ".rdata"}:
            return analyze_r_data(path)
        if extension == ".pdf":
            return analyze_pdf(path)
        if extension == ".docx":
            return analyze_docx(path)
        if extension in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".tif",
            ".tiff",
            ".webp",
        }:
            return analyze_image(path)
        if extension in TEXT_EXTENSIONS or extension in CODE_EXTENSIONS:
            return analyze_code_or_text(path)
        return generic_analysis(path)
    except Exception as error:
        return generic_analysis(
            path,
            reason=f"{type(error).__name__}: {str(error)[:500]}",
        )


def catalog_paths(task_root: Path) -> Tuple[Path, Path, Path]:
    catalog_root = task_root / ".file-catalog"
    return (
        catalog_root,
        catalog_root / "documents",
        catalog_root / "catalog.sqlite3",
    )


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(str(temporary), str(path))


def ensure_catalog(task_root: Path) -> Tuple[Path, Path, Path]:
    catalog_root, documents_root, database_path = catalog_paths(task_root)
    documents_root.mkdir(parents=True, exist_ok=True)
    gitignore = "\n".join(
        [
            "# Machine index and transient files; Markdown explanations remain trackable.",
            "/catalog.sqlite3",
            "/catalog.sqlite3-shm",
            "/catalog.sqlite3-wal",
            "*.tmp",
            "",
        ]
    )
    gitignore_path = catalog_root / ".gitignore"
    if not gitignore_path.exists() or gitignore_path.read_text(
        encoding="utf-8", errors="replace"
    ) != gitignore:
        atomic_write_text(gitignore_path, gitignore)
    return catalog_root, documents_root, database_path


def connect_database(database_path: Path, create: bool) -> Optional[sqlite3.Connection]:
    if not create and not database_path.exists():
        return None
    connection = sqlite3.connect(str(database_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    if create:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                relative_path TEXT PRIMARY KEY,
                source_absolute TEXT NOT NULL,
                task_root_at_scan TEXT NOT NULL,
                document_relative TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                file_type TEXT NOT NULL,
                analyzer TEXT NOT NULL,
                language TEXT NOT NULL,
                status TEXT NOT NULL,
                summary_zh TEXT NOT NULL,
                summary_en TEXT NOT NULL,
                structure_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                search_text TEXT NOT NULL,
                analyzed_at TEXT NOT NULL,
                reused_from TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)"
        )
        connection.commit()
    return connection


def analysis_from_row(row: sqlite3.Row) -> Analysis:
    return Analysis(
        format_name=row["file_type"],
        analyzer=row["analyzer"],
        status=row["status"],
        language=row["language"],
        summary_zh=row["summary_zh"],
        summary_en=row["summary_en"],
        structure=json.loads(row["structure_json"]),
        warnings=json.loads(row["warnings_json"]),
    )


def render_document(
    task_root: Path,
    source: Path,
    relative_path: str,
    digest: str,
    analysis: Analysis,
    reused_from: Optional[str],
) -> str:
    stat = source.stat()
    modified = dt.datetime.fromtimestamp(
        stat.st_mtime, tz=dt.timezone.utc
    ).replace(microsecond=0).isoformat()
    analyzed = utc_now()
    title = source.name
    structure_json = json.dumps(
        analysis.structure,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    warning_lines = analysis.warnings or ["None"]

    frontmatter = "\n".join(
        [
            "---",
            f"catalog_version: {CATALOG_VERSION}",
            f"relative_path: {yaml_quote(relative_path)}",
            f"absolute_path_at_scan: {yaml_quote(str(source))}",
            f"task_root_at_scan: {yaml_quote(str(task_root))}",
            f"sha256: {yaml_quote(digest)}",
            f"size_bytes: {stat.st_size}",
            f"modified_utc: {yaml_quote(modified)}",
            f"analyzed_utc: {yaml_quote(analyzed)}",
            f"status: {yaml_quote(analysis.status)}",
            f"format: {yaml_quote(analysis.format_name)}",
            f"analyzer: {yaml_quote(analysis.analyzer)}",
            "---",
        ]
    )

    if analysis.language == "en":
        summary = analysis.summary_en
        warning_block = "\n".join(f"- {item}" for item in warning_lines)
        reuse_line = (
            f"- Reused structure from task-relative path: `{reused_from}`"
            if reused_from
            else "- Structure was parsed from this file."
        )
        body = f"""
# {title}

## Location and freshness

- Task-relative path: `{relative_path}`
- Absolute path at scan: `{source}`
- Size: {stat.st_size} bytes
- Modified: {modified}
- SHA-256: `{digest}`
- Status: `{analysis.status}`
- Format/analyzer: `{analysis.format_name}` / `{analysis.analyzer}`
{reuse_line}

## Content overview

{summary}

## Data or file structure

```json
{structure_json}
```

## Limits and warnings

{warning_block}

This explanation intentionally omits raw rows, cell samples, paragraph excerpts, category values, and source-code snippets.
""".lstrip()
    else:
        summary = analysis.summary_zh
        warning_block = "\n".join(f"- {item}" for item in warning_lines)
        reuse_line = (
            f"- 结构复用来源（任务相对路径）：`{reused_from}`"
            if reused_from
            else "- 结构由当前文件解析得到。"
        )
        body = f"""
# {title}

## 位置与新鲜度

- 任务相对路径：`{relative_path}`
- 扫描时绝对路径：`{source}`
- 大小：{stat.st_size} 字节
- 修改时间：{modified}
- SHA-256：`{digest}`
- 状态：`{analysis.status}`
- 格式/解析器：`{analysis.format_name}` / `{analysis.analyzer}`
{reuse_line}

## 内容概览

{summary}

## 数据或文件结构

```json
{structure_json}
```

## 限制与警告

{warning_block}

本说明有意省略原始数据行、单元格样例、正文段落、类别值和源代码片段。
""".lstrip()
    return frontmatter + "\n\n" + body


def upsert_entry(
    connection: sqlite3.Connection,
    task_root: Path,
    source: Path,
    relative_path: str,
    document_relative: str,
    digest: str,
    analysis: Analysis,
    reused_from: Optional[str],
) -> None:
    stat = source.stat()
    structure_json = json.dumps(analysis.structure, ensure_ascii=False, sort_keys=True)
    warnings_json = json.dumps(analysis.warnings, ensure_ascii=False)
    search_text = "\n".join(
        [
            relative_path,
            source.name,
            analysis.format_name,
            analysis.analyzer,
            analysis.summary_zh,
            analysis.summary_en,
            structure_json,
        ]
    )
    connection.execute(
        """
        INSERT INTO files (
            relative_path, source_absolute, task_root_at_scan, document_relative,
            size_bytes, mtime_ns, sha256, file_type, analyzer, language, status,
            summary_zh, summary_en, structure_json, warnings_json, search_text,
            analyzed_at, reused_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(relative_path) DO UPDATE SET
            source_absolute=excluded.source_absolute,
            task_root_at_scan=excluded.task_root_at_scan,
            document_relative=excluded.document_relative,
            size_bytes=excluded.size_bytes,
            mtime_ns=excluded.mtime_ns,
            sha256=excluded.sha256,
            file_type=excluded.file_type,
            analyzer=excluded.analyzer,
            language=excluded.language,
            status=excluded.status,
            summary_zh=excluded.summary_zh,
            summary_en=excluded.summary_en,
            structure_json=excluded.structure_json,
            warnings_json=excluded.warnings_json,
            search_text=excluded.search_text,
            analyzed_at=excluded.analyzed_at,
            reused_from=excluded.reused_from
        """,
        (
            relative_path,
            str(source),
            str(task_root),
            document_relative,
            stat.st_size,
            stat.st_mtime_ns,
            digest,
            analysis.format_name,
            analysis.analyzer,
            analysis.language,
            analysis.status,
            analysis.summary_zh,
            analysis.summary_en,
            structure_json,
            warnings_json,
            search_text,
            utc_now(),
            reused_from,
        ),
    )


def render_index(connection: sqlite3.Connection, task_root: Path, index_path: Path) -> None:
    rows = connection.execute(
        """
        SELECT relative_path, file_type, status, document_relative, analyzed_at
        FROM files
        ORDER BY lower(relative_path)
        """
    ).fetchall()
    lines = [
        "# File Catalog Index",
        "",
        f"> Task root at generation: `{task_root}`",
        f"> Generated: {utc_now()}",
        "",
        "| Status | Task-relative path | Format | Explanation |",
        "|---|---|---|---|",
    ]
    for row in rows:
        relative = str(row["relative_path"]).replace("|", "\\|")
        file_type = str(row["file_type"]).replace("|", "\\|")
        link = Path(row["document_relative"]).as_posix()
        lines.append(
            f"| {row['status']} | `{relative}` | {file_type} | [document]({link}) |"
        )
    lines.extend(
        [
            "",
            "Use the skill's `search` command instead of loading this entire index into Agent context.",
            "",
        ]
    )
    atomic_write_text(index_path, "\n".join(lines))


def result_record(
    status: str,
    source: Path,
    relative_path: str,
    document: str,
    reused_from: Optional[str] = None,
) -> Dict[str, str]:
    return {
        "status": status,
        "source": str(source),
        "relative_path": relative_path,
        "document": document,
        "reused_from": reused_from or "",
    }


def catalog_files(
    task_root: Path,
    raw_paths: Sequence[str],
) -> List[Dict[str, str]]:
    catalog_root, documents_root, database_path = ensure_catalog(task_root)
    connection = connect_database(database_path, create=True)
    assert connection is not None
    candidates, issues, full_scan = gather_files(task_root, raw_paths)
    results = list(issues)
    seen: set[str] = set()

    try:
        for source in candidates:
            relative_path = relative_key(source, task_root)
            seen.add(relative_path)
            stat = source.stat()
            row = connection.execute(
                "SELECT * FROM files WHERE relative_path = ?",
                (relative_path,),
            ).fetchone()
            doc_relative = f"documents/{document_id(relative_path)}.md"
            doc_path = catalog_root / doc_relative

            if (
                row is not None
                and row["size_bytes"] == stat.st_size
                and row["mtime_ns"] == stat.st_mtime_ns
                and row["status"] != "missing"
                and doc_path.exists()
            ):
                if (
                    row["source_absolute"] != str(source)
                    or row["task_root_at_scan"] != str(task_root)
                ):
                    stored_analysis = analysis_from_row(row)
                    relocated_document = render_document(
                        task_root,
                        source,
                        relative_path,
                        row["sha256"],
                        stored_analysis,
                        row["reused_from"],
                    )
                    atomic_write_text(doc_path, relocated_document)
                    upsert_entry(
                        connection,
                        task_root,
                        source,
                        relative_path,
                        doc_relative,
                        row["sha256"],
                        stored_analysis,
                        row["reused_from"],
                    )
                results.append(
                    result_record(
                        row["status"],
                        source,
                        relative_path,
                        str(doc_path),
                        row["reused_from"],
                    )
                )
                continue

            digest = sha256_file(source)
            duplicate = connection.execute(
                """
                SELECT * FROM files
                WHERE sha256 = ?
                  AND relative_path <> ?
                  AND status IN ('fresh', 'unsupported')
                ORDER BY analyzed_at DESC
                LIMIT 1
                """,
                (digest, relative_path),
            ).fetchone()
            reused_from: Optional[str] = None
            if duplicate is not None:
                analysis = analysis_from_row(duplicate)
                reused_from = duplicate["relative_path"]
            else:
                analysis = analyze_file(source)

            document = render_document(
                task_root,
                source,
                relative_path,
                digest,
                analysis,
                reused_from,
            )
            atomic_write_text(doc_path, document)
            upsert_entry(
                connection,
                task_root,
                source,
                relative_path,
                doc_relative,
                digest,
                analysis,
                reused_from,
            )
            results.append(
                result_record(
                    analysis.status,
                    source,
                    relative_path,
                    str(doc_path),
                    reused_from,
                )
            )

        if full_scan:
            existing_rows = connection.execute(
                "SELECT relative_path FROM files"
            ).fetchall()
            missing_paths = [
                row["relative_path"]
                for row in existing_rows
                if row["relative_path"] not in seen
            ]
            connection.executemany(
                "UPDATE files SET status = 'missing' WHERE relative_path = ?",
                [(path,) for path in missing_paths],
            )

        connection.commit()
        render_index(connection, task_root, catalog_root / "INDEX.md")
    finally:
        connection.close()
    return results


def lookup_files(
    task_root: Path,
    raw_paths: Sequence[str],
) -> List[Dict[str, str]]:
    catalog_root, _, database_path = catalog_paths(task_root)
    candidates, issues, _ = gather_files(task_root, raw_paths)
    connection = connect_database(database_path, create=False)
    results = list(issues)
    if connection is None:
        for source in candidates:
            results.append(
                result_record(
                    "missing",
                    source,
                    relative_key(source, task_root),
                    "",
                )
            )
        return results

    try:
        for source in candidates:
            relative_path = relative_key(source, task_root)
            row = connection.execute(
                "SELECT * FROM files WHERE relative_path = ?",
                (relative_path,),
            ).fetchone()
            if row is None:
                results.append(
                    result_record("missing", source, relative_path, "")
                )
                continue
            stat = source.stat()
            document = str(catalog_root / row["document_relative"])
            if (
                row["size_bytes"] == stat.st_size
                and row["mtime_ns"] == stat.st_mtime_ns
                and Path(document).exists()
            ):
                status = row["status"]
            else:
                status = "stale"
            results.append(
                result_record(
                    status,
                    source,
                    relative_path,
                    document,
                    row["reused_from"],
                )
            )
    finally:
        connection.close()
    return results


def search_catalog(
    task_root: Path,
    query: str,
    limit: int,
) -> List[Dict[str, str]]:
    catalog_root, _, database_path = catalog_paths(task_root)
    connection = connect_database(database_path, create=False)
    if connection is None:
        return []
    try:
        pattern = f"%{query}%"
        rows = connection.execute(
            """
            SELECT relative_path, source_absolute, document_relative, status,
                   file_type, reused_from
            FROM files
            WHERE relative_path LIKE ? COLLATE NOCASE
               OR source_absolute LIKE ? COLLATE NOCASE
               OR search_text LIKE ? COLLATE NOCASE
            ORDER BY
                CASE WHEN relative_path LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,
                lower(relative_path)
            LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, limit),
        ).fetchall()
        return [
            {
                "status": row["status"],
                "source": row["source_absolute"],
                "relative_path": row["relative_path"],
                "document": str(catalog_root / row["document_relative"]),
                "format": row["file_type"],
                "reused_from": row["reused_from"] or "",
            }
            for row in rows
        ]
    finally:
        connection.close()


def print_results(records: Sequence[Dict[str, str]], limit: int) -> None:
    shown = list(records[:limit])
    for record in shown:
        parts = [
            f"status={record.get('status', '')}",
            f"path={record.get('relative_path', '')}",
        ]
        if record.get("format"):
            parts.append(f"format={record['format']}")
        if record.get("document"):
            parts.append(f"document={record['document']}")
        if record.get("reused_from"):
            parts.append(f"reused_from={record['reused_from']}")
        print(" | ".join(parts))
    if len(records) > len(shown):
        print(f"... {len(records) - len(shown)} additional result(s) omitted by --limit")
    print(f"summary total={len(records)} shown={len(shown)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and reuse per-task structural explanations for local files."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog_parser = subparsers.add_parser(
        "catalog", help="Catalog missing or stale task files."
    )
    catalog_parser.add_argument(
        "--task-root", help="Large-task root; defaults to the current directory."
    )
    catalog_parser.add_argument(
        "--limit", type=int, default=30, help="Maximum result rows printed."
    )
    catalog_parser.add_argument(
        "paths",
        nargs="*",
        help="In-root files/directories; omit to recursively catalog the task root.",
    )

    lookup_parser = subparsers.add_parser(
        "lookup", help="Check whether explanations are fresh."
    )
    lookup_parser.add_argument(
        "--task-root", help="Large-task root; defaults to the current directory."
    )
    lookup_parser.add_argument(
        "--limit", type=int, default=30, help="Maximum result rows printed."
    )
    lookup_parser.add_argument(
        "paths", nargs="+", help="In-root files or directories to check."
    )

    search_parser = subparsers.add_parser(
        "search", help="Search the current task's catalog."
    )
    search_parser.add_argument(
        "--task-root", help="Large-task root; defaults to the current directory."
    )
    search_parser.add_argument(
        "--limit", type=int, default=20, help="Maximum search results."
    )
    search_parser.add_argument("query", help="Path, name, format, field, or keyword.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows pipes inherit a locale-dependent encoding (often cp1252). Force
    # UTF-8 so paths and structural names remain portable across all terminals.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        task_root = resolve_task_root(args.task_root)
        limit = max(1, min(int(args.limit), 500))
        if args.command == "catalog":
            records = catalog_files(task_root, args.paths)
        elif args.command == "lookup":
            records = lookup_files(task_root, args.paths)
        else:
            records = search_catalog(task_root, args.query, limit)
        print_results(records, limit)
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"error={type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
