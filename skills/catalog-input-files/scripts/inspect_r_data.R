#!/usr/bin/env Rscript
#
# =============================================================================
# 代码介绍
# =============================================================================
# 输入：
#   1. 命令行第一个参数：一个 .rds、.rda 或 .RData 文件的绝对路径。
#   2. 文件内容必须能够由 base R 的 readRDS() 或 load() 读取。
#
# 输出：
#   标准输出为 UTF-8 JSON。顶层包含 status、format、objects 和 warnings。
#   objects 只描述对象名称、class、typeof、维度、列名、缺失量、近似唯一值数量、
#   列表成员名、函数参数名等结构信息；不会输出数据行、单元格值、因子水平或文本内容。
#   失败时仍输出 status="error" 的 JSON，并以非零状态退出。
#
# 作用：
#   为 catalog-input-files 技能提供稳定的 R 数据结构探查能力，使 Agent 不必在每个
#   分析任务中重复编写 readRDS()/load()/str() 等一次性检查代码。
#
# 设计逻辑：
#   - 根据扩展名选择 readRDS() 或隔离环境中的 load()。
#   - 递归描述对象，但限制最大深度、成员数量和统计样本量，避免巨大对象产生巨大输出。
#   - 数据框按列输出结构统计；数组输出维度；列表输出成员结构；其他对象输出通用元数据。
#   - 所有 JSON 由 jsonlite 生成，保证 Python 调用方可稳定解析。
#
# 主要函数：
#   scalar_text()          将 class/typeof 等结构标签压缩为单个字符串。
#   approximate_unique()   在最多 10,000 个元素上计算近似唯一值数量。
#   summarize_column()     描述数据框的一列，不泄露实际值。
#   summarize_object()     递归描述任意 R 对象。
#   inspect_r_file()       读取文件并构造最终结构结果。
#
# 调用方式：
#   Rscript --vanilla inspect_r_data.R "/path/to/data.rds"
# =============================================================================

suppressWarnings(suppressMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("The jsonlite package is required.")
  }
}))

MAX_DEPTH <- 3L
MAX_CHILDREN <- 100L
MAX_UNIQUE_SAMPLE <- 10000L

scalar_text <- function(value) {
  if (length(value) == 0L) {
    return("")
  }
  paste(as.character(value), collapse = ", ")
}

approximate_unique <- function(value) {
  if (length(value) == 0L) {
    return(0L)
  }
  sampled <- head(value, MAX_UNIQUE_SAMPLE)
  sampled <- sampled[!is.na(sampled)]
  length(unique(sampled))
}

summarize_column <- function(value) {
  list(
    class = scalar_text(class(value)),
    typeof = typeof(value),
    length = length(value),
    missing = sum(is.na(value)),
    approximate_unique = approximate_unique(value),
    unique_sample_limit = min(length(value), MAX_UNIQUE_SAMPLE)
  )
}

summarize_object <- function(value, depth = 0L) {
  base <- list(
    class = scalar_text(class(value)),
    typeof = typeof(value),
    length = length(value),
    object_size_bytes = as.numeric(utils::object.size(value))
  )

  dims <- dim(value)
  if (!is.null(dims)) {
    base$dimensions <- as.integer(dims)
  }

  if (is.data.frame(value)) {
    column_names <- names(value)
    selected <- head(seq_along(value), MAX_CHILDREN)
    columns <- lapply(selected, function(index) summarize_column(value[[index]]))
    names(columns) <- column_names[selected]
    base$column_count <- ncol(value)
    base$row_count <- nrow(value)
    base$columns <- columns
    base$truncated_columns <- ncol(value) > MAX_CHILDREN
    return(base)
  }

  if (is.matrix(value) || is.array(value)) {
    base$missing <- sum(is.na(value))
    return(base)
  }

  if (is.function(value)) {
    base$parameters <- names(formals(value))
    return(base)
  }

  if (isS4(value)) {
    slot_names <- methods::slotNames(value)
    base$slot_names <- head(slot_names, MAX_CHILDREN)
    if (depth < MAX_DEPTH) {
      chosen <- head(slot_names, MAX_CHILDREN)
      slots <- lapply(chosen, function(slot_name) {
        summarize_object(methods::slot(value, slot_name), depth + 1L)
      })
      names(slots) <- chosen
      base$slots <- slots
    }
    base$truncated_slots <- length(slot_names) > MAX_CHILDREN
    return(base)
  }

  if (is.list(value)) {
    item_names <- names(value)
    if (is.null(item_names)) {
      item_names <- paste0("[[", seq_along(value), "]]")
    } else {
      empty <- !nzchar(item_names)
      item_names[empty] <- paste0("[[", which(empty), "]]")
    }
    chosen_indices <- head(seq_along(value), MAX_CHILDREN)
    base$member_names <- item_names[chosen_indices]
    if (depth < MAX_DEPTH) {
      children <- lapply(chosen_indices, function(index) {
        summarize_object(value[[index]], depth + 1L)
      })
      names(children) <- item_names[chosen_indices]
      base$members <- children
    }
    base$truncated_members <- length(value) > MAX_CHILDREN
    return(base)
  }

  if (is.atomic(value)) {
    base$missing <- sum(is.na(value))
    base$approximate_unique <- approximate_unique(value)
    base$unique_sample_limit <- min(length(value), MAX_UNIQUE_SAMPLE)
  }

  base
}

inspect_r_file <- function(path) {
  extension <- tolower(tools::file_ext(path))
  warnings <- character()

  if (extension == "rds") {
    value <- readRDS(path)
    objects <- list(value = summarize_object(value))
    return(list(
      status = "ok",
      format = "RDS",
      objects = objects,
      warnings = warnings
    ))
  }

  if (extension %in% c("rda", "rdata")) {
    environment <- new.env(parent = emptyenv())
    object_names <- load(path, envir = environment)
    selected_names <- head(object_names, MAX_CHILDREN)
    objects <- lapply(selected_names, function(object_name) {
      summarize_object(get(object_name, envir = environment, inherits = FALSE))
    })
    names(objects) <- selected_names
    if (length(object_names) > MAX_CHILDREN) {
      warnings <- c(warnings, sprintf(
        "Only the first %d of %d objects were described.",
        MAX_CHILDREN,
        length(object_names)
      ))
    }
    return(list(
      status = "ok",
      format = "RData",
      object_count = length(object_names),
      objects = objects,
      warnings = warnings
    ))
  }

  stop(sprintf("Unsupported R data extension: %s", extension))
}

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  cat(jsonlite::toJSON(
    list(status = "error", message = "Expected exactly one input path."),
    auto_unbox = TRUE,
    null = "null"
  ))
  quit(status = 2L)
}

input_path <- normalizePath(args[[1L]], winslash = "\\", mustWork = FALSE)

tryCatch(
  {
    if (!file.exists(input_path)) {
      stop(sprintf("Input file does not exist: %s", input_path))
    }
    result <- inspect_r_file(input_path)
    cat(jsonlite::toJSON(
      result,
      auto_unbox = TRUE,
      null = "null",
      digits = NA
    ))
  },
  error = function(error) {
    cat(jsonlite::toJSON(
      list(status = "error", message = conditionMessage(error)),
      auto_unbox = TRUE,
      null = "null"
    ))
    quit(status = 2L)
  }
)
