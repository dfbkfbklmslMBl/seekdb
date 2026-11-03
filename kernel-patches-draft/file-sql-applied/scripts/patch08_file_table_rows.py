"""Patch 08: give the file table a row source.

Four byte-exact edits on the build box:
  1. src/sql/file_scan/ob_file_scan_utils.h   - column/row-set types, the loader, cell conversion
  2. src/sql/file_scan/ob_file_scan_utils.cpp - their implementation (CSV scan + glob listing)
  3. src/sql/resolver/dml/ob_dml_resolver.h    - the two new private helpers
  4. src/sql/resolver/dml/ob_dml_resolver.cpp  - resolve_file_table_item stops returning 1235 and
     instead builds a VALUES table: the engine's own, already-tested row source for a FROM item whose
     rows are known when the statement is resolved, with the file's headers and inferred types.

Run from the repo root: python3 ktools/patch08_file_table_rows.py
"""

H_PATH = "src/sql/file_scan/ob_file_scan_utils.h"
CPP_PATH = "src/sql/file_scan/ob_file_scan_utils.cpp"
RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"
RESH_PATH = "src/sql/resolver/dml/ob_dml_resolver.h"

MARKER = "ObFileSqlTableData"

NS_END = u"} // namespace sql\n} // namespace oceanbase"

H_NEW = u'''
// One column of a file table as the user sees it: the header name (de-duplicated), the runtime
// type the engine stores, the widest value length for text columns, and whether a NULL was seen.
struct ObFileSqlColumn
{
  ObFileSqlColumn() : name_(), type_(common::ObObjType::ObNullType), max_len_(0), nullable_(false) {}
  void reset()
  {
    name_.reset();
    type_ = common::ObObjType::ObNullType;
    max_len_ = 0;
    nullable_ = false;
  }
  common::ObString name_;
  common::ObObjType type_;
  int64_t max_len_;
  bool nullable_;
  TO_STRING_KV(K_(name), K_(type), K_(max_len), K_(nullable));
};

// The rows of a file table as raw field text, row major: row r column c is cells_.at(r * col_cnt + c).
// An empty field means NULL, which is what the CSV reader produces for `a,,b`.
struct ObFileSqlTableData
{
  ObFileSqlTableData() : cols_(), cells_(), row_cnt_(0), col_cnt_(0), bad_lines_(0) {}
  void reset()
  {
    cols_.reset();
    cells_.reset();
    row_cnt_ = 0;
    col_cnt_ = 0;
    bad_lines_ = 0;
  }
  common::ObSEArray<ObFileSqlColumn, 16> cols_;
  common::ObSEArray<common::ObString, 64> cells_;
  int64_t row_cnt_;
  int64_t col_cnt_;
  int64_t bad_lines_;
  TO_STRING_KV(K_(col_cnt), K_(row_cnt), K_(bad_lines));
};

// The type name DESCRIBE reports for an inferred runtime type. BOOLEAN is stored as tinyint(1) by
// the MySQL layer, so the storage type and the name a user expects are not the same thing.
const char *ob_file_sql_describe_type(const ObFileSqlColumn &col);

// Header de-duplication in the shape the suite expects: a repeated `id` becomes `id__2`, then `id__3`.
int ob_file_sql_dedup_column_names(common::ObIArray<ObFileSqlColumn> &cols,
                                   common::ObIAllocator &alloc);

// Turns one raw field into an ObObj of the column's inferred type. A field that does not fit the
// inferred type is an error rather than a silent 0: a wrong answer is worse than a rejected query.
int ob_file_sql_cell_to_obj(const common::ObString &text, const ObFileSqlColumn &col,
                            common::ObIAllocator &alloc, common::ObObj &obj);

// Reads a file table whole. The row source the engine can currently feed is a values table, whose
// rows must exist at compile time, so the loader is bounded: MAX_ROWS caps what one query may
// materialise and reports a clear error instead of exhausting memory.
class ObFileSqlLoader
{
public:
  static const int64_t MAX_ROWS = 100000;
  static int load(common::ObIAllocator &alloc, const ObFileSqlScanParam &param,
                  ObFileSqlTableData &data);
private:
  static int load_columns(common::ObIAllocator &alloc, const ObFileSqlScanParam &param,
                          ObFileSqlTableData &data);
  static int load_glob(common::ObIAllocator &alloc, const ObFileSqlScanParam &param,
                       ObFileSqlTableData &data);
  DISALLOW_COPY_AND_ASSIGN(ObFileSqlLoader);
};

'''

CPP_NEW = u'''
const char *ob_file_sql_describe_type(const ObFileSqlColumn &col)
{
  const char *name = "VARCHAR";
  switch (col.type_) {
    case ObIntType:
      name = "BIGINT";
      break;
    case ObTinyIntType:
      name = "BOOLEAN";
      break;
    case ObDoubleType:
      name = "DOUBLE";
      break;
    case ObDateType:
      name = "DATE";
      break;
    case ObDateTimeType:
      name = "DATETIME";
      break;
    default:
      // an all-NULL column stays text so that a later row can still be read
      name = "VARCHAR";
      break;
  }
  return name;
}

int ob_file_sql_dedup_column_names(ObIArray<ObFileSqlColumn> &cols, ObIAllocator &alloc)
{
  int ret = OB_SUCCESS;
  for (int64_t i = 0; OB_SUCC(ret) && i < cols.count(); ++i) {
    int64_t seen = 0;
    for (int64_t j = 0; j < i; ++j) {
      if (cols.at(j).name_ == cols.at(i).name_) {
        ++seen;
      }
    }
    if (0 == seen) {
      // the first use of a header keeps its name
    } else {
      char buf[OB_MAX_IDENTIFIER_LENGTH] = {'\\0'};
      ObString base = cols.at(i).name_;
      if (base.length() > OB_MAX_IDENTIFIER_LENGTH - 8) {
        base.assign_ptr(base.ptr(), OB_MAX_IDENTIFIER_LENGTH - 8);
      }
      const int n = snprintf(buf, sizeof(buf), "%.*s__%ld", base.length(), base.ptr(), seen + 1);
      char *name_buf = NULL;
      if (n <= 0 || n >= static_cast<int>(sizeof(buf))) {
        ret = OB_SIZE_OVERFLOW;
        LOG_WARN("failed to build a de-duplicated column name", K(ret), K(i), K(seen));
      } else if (OB_ISNULL(name_buf = static_cast<char *>(alloc.alloc(n)))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else {
        MEMCPY(name_buf, buf, n);
        cols.at(i).name_.assign_ptr(name_buf, static_cast<int32_t>(n));
      }
    }
  }
  return ret;
}

int ob_file_sql_cell_to_obj(const ObString &text, const ObFileSqlColumn &col,
                            ObIAllocator &alloc, ObObj &obj)
{
  int ret = OB_SUCCESS;
  obj.reset();
  if (text.empty()) {
    // an empty field is NULL for every inferred type; the column type comes from the schema, not
    // from this one value
    obj.set_null();
  } else {
    switch (col.type_) {
      case ObIntType: {
        char *num_buf = NULL;
        int64_t value = 0;
        if (OB_ISNULL(num_buf = static_cast<char *>(alloc.alloc(text.length() + 1)))) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
        } else {
          MEMCPY(num_buf, text.ptr(), text.length());
          num_buf[text.length()] = '\\0';
          errno = 0;
          value = strtoll(num_buf, NULL, 10);
          if (0 != errno) {
            ret = OB_INVALID_ARGUMENT;
            LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, integer value out of range");
          } else {
            obj.set_int(value);
          }
        }
        break;
      }
      case ObDoubleType: {
        char *num_buf = NULL;
        double value = 0.0;
        if (OB_ISNULL(num_buf = static_cast<char *>(alloc.alloc(text.length() + 1)))) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
        } else {
          MEMCPY(num_buf, text.ptr(), text.length());
          num_buf[text.length()] = '\\0';
          errno = 0;
          value = strtod(num_buf, NULL);
          if (0 != errno) {
            ret = OB_INVALID_ARGUMENT;
            LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, double value out of range");
          } else {
            obj.set_double(value);
          }
        }
        break;
      }
      case ObTinyIntType: {
        char lower[8] = {'\\0'};
        for (int64_t i = 0; i < text.length() && i < 7; ++i) {
          lower[i] = static_cast<char>(tolower(text[i]));
        }
        const bool is_true = (0 == strcmp(lower, "true") || 0 == strcmp(lower, "t")
                              || 0 == strcmp(lower, "yes") || 0 == strcmp(lower, "y")
                              || 0 == strcmp(lower, "on") || 0 == strcmp(lower, "1"));
        obj.set_tinyint(is_true ? 1 : 0);
        break;
      }
      case ObDateType: {
        int32_t value = 0;
        if (OB_FAIL(ObTimeConverter::str_to_date(text, value))) {
          LOG_WARN("failed to read a date field", K(ret), K(text));
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, invalid DATE value");
        } else {
          obj.set_date(value);
        }
        break;
      }
      case ObDateTimeType: {
        // the reader accepts both separators, the engine's parser takes the space form only
        ObString normalized;
        char *time_buf = NULL;
        int64_t value = 0;
        ObTimeConvertCtx cvrt_ctx(NULL /*tz_info*/, false /*is_timestamp*/);
        if (text.length() >= 19 && 'T' == text[10]) {
          if (OB_ISNULL(time_buf = static_cast<char *>(alloc.alloc(text.length())))) {
            ret = OB_ALLOCATE_MEMORY_FAILED;
          } else {
            MEMCPY(time_buf, text.ptr(), text.length());
            time_buf[10] = ' ';
            normalized.assign_ptr(time_buf, static_cast<int32_t>(text.length()));
          }
        } else {
          normalized = text;
        }
        if (OB_FAIL(ret)) {
        } else if (OB_FAIL(ObTimeConverter::str_to_datetime(normalized, cvrt_ctx, value, NULL))) {
          LOG_WARN("failed to read a datetime field", K(ret), K(text));
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, invalid DATETIME value");
        } else {
          obj.set_datetime(value);
        }
        break;
      }
      default: {
        ObString copied;
        if (OB_FAIL(ob_write_string(alloc, text, copied, true))) {
          LOG_WARN("failed to copy a text field", K(ret));
        } else {
          obj.set_varchar(copied);
          obj.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
        }
        break;
      }
    }
  }
  return ret;
}

int ObFileSqlLoader::load(ObIAllocator &alloc, const ObFileSqlScanParam &param,
                          ObFileSqlTableData &data)
{
  int ret = OB_SUCCESS;
  data.reset();
  if (OB_FILE_SQL_FMT_GLOB == param.format_) {
    ret = load_glob(alloc, param, data);
  } else if (OB_FILE_SQL_FMT_PARQUET == param.format_) {
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "reading a parquet file as a table");
  } else if (OB_FAIL(load_columns(alloc, param, data))) {
    LOG_WARN("failed to read the file table", K(ret), K(param));
  }
  return ret;
}

// One pass infers the types, the second keeps the text of every cell. Two passes keep the inference
// honest: a column's type is decided by the whole file, not by its first row.
int ObFileSqlLoader::load_columns(ObIAllocator &alloc, const ObFileSqlScanParam &param,
                                  ObFileSqlTableData &data)
{
  int ret = OB_SUCCESS;
  ObFileSqlReader reader(alloc);
  ObSEArray<ObFileSqlInferCol, 32> inferred;
  if (OB_FAIL(reader.init(param))) {
    LOG_WARN("failed to open the file", K(ret), K(param));
  } else if (OB_FAIL(reader.infer_schema(inferred))) {
    LOG_WARN("failed to infer the schema", K(ret), K(param));
  } else if (inferred.empty()) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the file has no columns");
  } else {
    for (int64_t i = 0; OB_SUCC(ret) && i < inferred.count(); ++i) {
      ObFileSqlColumn col;
      col.name_ = inferred.at(i).name_;
      col.type_ = ob_file_sql_infer_final_type(inferred.at(i));
      col.max_len_ = inferred.at(i).max_len_;
      col.nullable_ = inferred.at(i).has_null_;
      if (OB_FAIL(data.cols_.push_back(col))) {
        LOG_WARN("failed to collect a column", K(ret), K(i));
      }
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(ob_file_sql_dedup_column_names(data.cols_, alloc))) {
      LOG_WARN("failed to de-duplicate column names", K(ret));
    } else if (OB_FAIL(reader.open_for_scan())) {
      LOG_WARN("failed to rewind the file for the scan pass", K(ret), K(param));
    } else {
      data.col_cnt_ = data.cols_.count();
      ObFileRow row;
      while (OB_SUCC(ret)) {
        row.reset();
        if (OB_FAIL(reader.next_row(row))) {
          if (OB_ITER_END == ret) {
            ret = OB_SUCCESS;
            break;
          } else {
            LOG_WARN("failed to read a record", K(ret), K(param));
          }
        } else if (row.fields_.count() != data.col_cnt_) {
          ++data.bad_lines_;
        } else if (data.row_cnt_ >= ObFileSqlLoader::MAX_ROWS) {
          ret = OB_NOT_SUPPORTED;
          LOG_USER_ERROR(OB_NOT_SUPPORTED, "a file with more than 100000 rows as a table");
        } else {
          for (int64_t c = 0; OB_SUCC(ret) && c < data.col_cnt_; ++c) {
            ObString stored;
            if (OB_FAIL(ob_write_string(alloc, row.fields_.at(c), stored, true))) {
              LOG_WARN("failed to keep a field", K(ret), K(c));
            } else if (OB_FAIL(data.cells_.push_back(stored))) {
              LOG_WARN("failed to collect a cell", K(ret), K(c));
            }
          }
          if (OB_SUCC(ret)) {
            ++data.row_cnt_;
          }
        }
      }
      data.bad_lines_ += reader.get_bad_line_count();
      if (OB_SUCC(ret) && 0 == data.row_cnt_) {
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "a file table with no rows");
      }
    }
  }
  reader.destroy();
  return ret;
}

// glob() lists the file system, so its rows are paths and its schema is one text column.
int ObFileSqlLoader::load_glob(ObIAllocator &alloc, const ObFileSqlScanParam &param,
                               ObFileSqlTableData &data)
{
  int ret = OB_SUCCESS;
  const ObString &pattern = param.path_;
  int64_t slash = -1;
  for (int64_t i = pattern.length() - 1; i >= 0 && slash < 0; --i) {
    if ('/' == pattern[i]) {
      slash = i;
    }
  }
  char *dir_buf = NULL;
  if (slash < 0) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table glob, the pattern has no directory");
  } else if (pattern.length() - 1 == slash) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table glob, the pattern has no name part");
  } else if (OB_ISNULL(dir_buf = static_cast<char *>(alloc.alloc(slash + 1)))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    MEMCPY(dir_buf, pattern.ptr(), slash);
    dir_buf[slash] = '\\0';
    const int64_t tail_off = slash + 1;
    const int64_t tail_len = pattern.length() - tail_off;
    int64_t star = -1;
    for (int64_t i = 0; i < tail_len && star < 0; ++i) {
      if ('*' == pattern[tail_off + i]) {
        star = i;
      }
    }
    const int64_t prefix_len = (star < 0) ? tail_len : star;
    const int64_t suffix_len = (star < 0) ? 0 : (tail_len - star - 1);
    char *prefix_buf = NULL;
    char *suffix_buf = NULL;
    if (OB_ISNULL(prefix_buf = static_cast<char *>(alloc.alloc(prefix_len + 1)))
        || OB_ISNULL(suffix_buf = static_cast<char *>(alloc.alloc(suffix_len + 1)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      MEMCPY(prefix_buf, pattern.ptr() + tail_off, prefix_len);
      prefix_buf[prefix_len] = '\\0';
      if (suffix_len > 0) {
        MEMCPY(suffix_buf, pattern.ptr() + pattern.length() - suffix_len, suffix_len);
      }
      suffix_buf[suffix_len] = '\\0';
      DIR *dir = opendir(dir_buf);
      if (NULL == dir) {
        ret = OB_INVALID_ARGUMENT;
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table glob, the directory cannot be opened");
      } else {
        ObSEArray<ObString, 16> found;
        dirent *entry = NULL;
        while (NULL != (entry = readdir(dir))) {
          const int64_t name_len = static_cast<int64_t>(strlen(entry->d_name));
          if ('.' == entry->d_name[0]
              || name_len < prefix_len + suffix_len
              || 0 != strncmp(entry->d_name, prefix_buf, prefix_len)
              || 0 != strncmp(entry->d_name + name_len - suffix_len, suffix_buf, suffix_len)) {
            // not a match
          } else {
            ObSqlString path_str;
            char *path_buf = NULL;
            if (OB_FAIL(path_str.append_fmt("%s/%s", dir_buf, entry->d_name))) {
              LOG_WARN("failed to build a path", K(ret));
            } else if (OB_ISNULL(path_buf = static_cast<char *>(alloc.alloc(path_str.length())))) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              MEMCPY(path_buf, path_str.ptr(), path_str.length());
              ObString full;
              full.assign_ptr(path_buf, static_cast<int32_t>(path_str.length()));
              if (OB_FAIL(found.push_back(full))) {
                LOG_WARN("failed to keep a matched path", K(ret));
              } else if (found.count() > ObFileSqlLoader::MAX_ROWS) {
                ret = OB_NOT_SUPPORTED;
                LOG_USER_ERROR(OB_NOT_SUPPORTED, "a directory with more than 100000 entries");
              }
            }
          }
        }
        closedir(dir);
        if (OB_SUCC(ret)) {
          // sorted, so that a query without ORDER BY is still deterministic
          for (int64_t i = 1; i < found.count(); ++i) {
            for (int64_t j = i; j > 0; --j) {
              const ObString &a = found.at(j);
              const ObString &b = found.at(j - 1);
              const int64_t n = (a.length() < b.length()) ? a.length() : b.length();
              const int cmp = memcmp(a.ptr(), b.ptr(), n);
              if (cmp < 0 || (0 == cmp && a.length() < b.length())) {
                const ObString tmp = found.at(j);
                found.at(j) = found.at(j - 1);
                found.at(j - 1) = tmp;
              } else {
                break;
              }
            }
          }
          static char file_col_name[] = "file";
          ObFileSqlColumn col;
          col.name_.assign_ptr(file_col_name, 4);
          col.type_ = ObVarcharType;
          col.max_len_ = 1024;
          col.nullable_ = false;
          data.col_cnt_ = 1;
          data.row_cnt_ = found.count();
          if (OB_FAIL(data.cols_.push_back(col))) {
            LOG_WARN("failed to build the file column", K(ret));
          }
          for (int64_t i = 0; OB_SUCC(ret) && i < found.count(); ++i) {
            if (OB_FAIL(data.cells_.push_back(found.at(i)))) {
              LOG_WARN("failed to keep a path", K(ret), K(i));
            }
          }
        }
      }
    }
  }
  return ret;
}

'''

RES_OLD = u'''  } else if (OB_FILE_SQL_FMT_GLOB == scan_param.format_) {
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "file table row source");
  } else {'''

RES_TAIL_MARKER = u'''      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "file table row source");
    }
  }
  return ret;
}
'''

RES_REPLACED = u'''  } else if (OB_FAIL(build_file_table_values_item(scan_param, table_item))) {
    LOG_WARN("failed to build a file table row source", K(ret), K(scan_param));
  }
  return ret;
}
'''

RES_HELPER = u'''
// The row source a file table feeds is the engine's own values table: its rows are known once the
// file has been read during resolution, it already streams through the standard operator, and its
// columns can be named and typed exactly as the file's header and inferred types say.
int ObDMLResolver::build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                                TableItem *&table_item)
{
  int ret = OB_SUCCESS;
  ObDMLStmt *dml_stmt = get_stmt();
  TableItem *new_table_item = NULL;
  ObFileSqlTableData data;
  common::ObArenaAllocator file_alloc(ObModIds::OB_SQL_COMPILE);
  if (OB_ISNULL(dml_stmt) || OB_ISNULL(allocator_) || OB_ISNULL(params_.expr_factory_)
      || OB_ISNULL(session_info_)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("missing resolver context for a file table", K(ret));
  } else if (OB_FAIL(ObFileSqlLoader::load(file_alloc, scan_param, data))) {
    LOG_WARN("failed to read the file table", K(ret), K(scan_param));
  } else if (OB_ISNULL(new_table_item = dml_stmt->create_table_item(*allocator_))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    void *buf = NULL;
    ObValuesTableDef *table_def = NULL;
    if (OB_ISNULL(buf = allocator_->alloc(sizeof(ObValuesTableDef)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else if (FALSE_IT(table_def = new (buf) ObValuesTableDef())) {
    } else {
      table_def->column_cnt_ = data.col_cnt_;
      table_def->row_cnt_ = data.row_cnt_;
      table_def->access_type_ = ObValuesTableDef::ACCESS_EXPR;
      table_def->is_const_ = false;
      new_table_item->values_table_def_ = table_def;
      new_table_item->table_id_ = generate_table_id();
      new_table_item->type_ = TableItem::VALUES_TABLE;
      new_table_item->is_view_table_ = false;
      if (OB_FAIL(dml_stmt->generate_values_table_name(*allocator_, new_table_item->table_name_))) {
        LOG_WARN("failed to name the file table", K(ret));
      } else if (FALSE_IT(new_table_item->alias_name_ = new_table_item->table_name_)) {
      } else if (OB_FAIL(dml_stmt->add_table_item(session_info_, new_table_item))) {
        LOG_WARN("failed to add the file table", K(ret));
      }
      // one column type per column, taken from the whole file rather than from its first row
      for (int64_t c = 0; OB_SUCC(ret) && c < data.col_cnt_; ++c) {
        const ObFileSqlColumn &col = data.cols_.at(c);
        ObRawExprResType res_type;
        res_type.set_type(col.type_);
        if (ObVarcharType == col.type_) {
          res_type.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
          res_type.set_collation_level(CS_LEVEL_IMPLICIT);
          res_type.set_length(static_cast<int32_t>((col.max_len_ > 1) ? col.max_len_ : 1));
        } else {
          res_type.set_collation_type(CS_TYPE_BINARY);
          res_type.set_collation_level(CS_LEVEL_IGNORABLE);
        }
        if (OB_FAIL(table_def->column_types_.push_back(res_type))) {
          LOG_WARN("failed to record a file column type", K(ret), K(c), K(col));
        } else if (OB_FAIL(table_def->column_ndvs_.push_back(data.row_cnt_))) {
          LOG_WARN("failed to record column statistics", K(ret), K(c));
        } else if (OB_FAIL(table_def->column_nnvs_.push_back(col.nullable_ ? 1 : 0))) {
          LOG_WARN("failed to record column statistics", K(ret), K(c));
        }
      }
      for (int64_t r = 0; OB_SUCC(ret) && r < data.row_cnt_; ++r) {
        for (int64_t c = 0; OB_SUCC(ret) && c < data.col_cnt_; ++c) {
          const ObString &text = data.cells_.at(r * data.col_cnt_ + c);
          ObObj obj;
          ObConstRawExpr *value_expr = NULL;
          if (OB_FAIL(ob_file_sql_cell_to_obj(text, data.cols_.at(c), *allocator_, obj))) {
            LOG_WARN("failed to convert a file field", K(ret), K(r), K(c), K(text));
          } else if (OB_FAIL(params_.expr_factory_->create_raw_expr(T_LIT, value_expr))) {
            LOG_WARN("failed to create a literal for a file field", K(ret), K(r), K(c));
          } else if (OB_ISNULL(value_expr)) {
            ret = OB_ERR_UNEXPECTED;
          } else if (FALSE_IT(value_expr->set_value(obj))) {
          } else if (FALSE_IT(value_expr->set_result_type(table_def->column_types_.at(c)))) {
          } else if (OB_FAIL(table_def->access_exprs_.push_back(value_expr))) {
            LOG_WARN("failed to collect a file row", K(ret), K(r), K(c));
          }
        }
      }
      if (OB_FAIL(ret)) {
      } else if (OB_FAIL(gen_values_table_column_items(table_def->column_cnt_,
                                                       table_def->column_types_,
                                                       *new_table_item))) {
        LOG_WARN("failed to build the file table columns", K(ret), K(data));
      } else if (OB_FAIL(rename_file_table_columns(*new_table_item, data))) {
        LOG_WARN("failed to name the file table columns", K(ret), K(data));
      } else {
        table_item = new_table_item;
        LOG_TRACE("file table materialised", K(data), K(scan_param));
      }
    }
  }
  return ret;
}

// gen_values_table_column_items names the columns column_0, column_1, ... for MySQL compatibility;
// a file table has real headers, so the names are replaced before any column reference is built.
int ObDMLResolver::rename_file_table_columns(TableItem &table_item, const ObFileSqlTableData &data)
{
  int ret = OB_SUCCESS;
  ObDMLStmt *dml_stmt = get_stmt();
  if (OB_ISNULL(dml_stmt) || OB_ISNULL(allocator_)) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    for (int64_t i = 0; OB_SUCC(ret) && i < data.col_cnt_; ++i) {
      ColumnItem *column_item = dml_stmt->get_column_item_by_id(table_item.table_id_,
                                                               OB_APP_MIN_COLUMN_ID + i);
      ObString copied;
      if (OB_ISNULL(column_item) || OB_ISNULL(column_item->expr_)) {
        ret = OB_ERR_UNEXPECTED;
        LOG_WARN("missing column item of a file table", K(ret), K(i));
      } else if (OB_FAIL(ob_write_string(*allocator_, data.cols_.at(i).name_, copied, true))) {
        LOG_WARN("failed to copy a column name", K(ret), K(i));
      } else {
        column_item->column_name_ = copied;
        ObColumnRefRawExpr *column_expr = static_cast<ObColumnRefRawExpr *>(column_item->expr_);
        column_expr->set_column_attr(table_item.table_name_, copied);
      }
    }
  }
  return ret;
}
'''

RES_ANCHOR = u"  int resolve_file_table_options("
RES_DECL = (u"  int build_file_table_values_item(const ObFileSqlScanParam &scan_param,\n"
            u"                                     TableItem *&table_item);\n"
            u"  int rename_file_table_columns(TableItem &table_item,\n"
            u"                                const ObFileSqlTableData &data);\n")

INC_OLD = u'#include "sql/file_scan/ob_file_scan_utils.h"'


def main():
    files = {}
    for path in (H_PATH, CPP_PATH, RES_PATH, RESH_PATH):
        with open(path, "rb") as handle:
            raw = handle.read()
        crlf = raw.count(b"\r\n")
        text = raw.decode("utf-8").replace("\r\n", "\n")
        files[path] = [text, crlf]
        if MARKER in text:
            raise SystemExit("[ABORT] patch 08 already applied in %s" % path)
        if 0 != text.count(NS_END if path in (H_PATH, CPP_PATH) else ""):
            pass

    # 1 + 2: header and implementation, appended inside the sql namespace
    for path in (H_PATH, CPP_PATH):
        text = files[path][0]
        if 1 != text.count(NS_END):
            raise SystemExit("[ABORT] namespace end is not unique in %s" % path)
        files[path][0] = text.replace(NS_END, (H_NEW if path == H_PATH else CPP_NEW) + NS_END, 1)

    # the loader needs directory listing, errno and the time-conversion helpers
    cpp = files[CPP_PATH][0]
    sys_anchor = u"#include <fcntl.h>"
    inc_anchor = u'#include "sql/file_scan/ob_file_scan_utils.h"'
    if 1 != cpp.count(sys_anchor) or 1 != cpp.count(inc_anchor):
        raise SystemExit("[ABORT] include anchors missing in %s" % CPP_PATH)
    cpp = cpp.replace(sys_anchor, sys_anchor + u"\n#include <dirent.h>\n#include <errno.h>", 1)
    cpp = cpp.replace(inc_anchor, inc_anchor +
                      u'\n#include "common/timezone/ob_time_convert.h"', 1)
    files[CPP_PATH][0] = cpp

    # 3: resolver header declarations
    text = files[RESH_PATH][0]
    if 1 != text.count(RES_ANCHOR):
        raise SystemExit("[ABORT] resolver header anchor is not unique")
    files[RESH_PATH][0] = text.replace(RES_ANCHOR, RES_DECL + RES_ANCHOR, 1)

    # 4: resolver, the gate becomes the builder
    text = files[RES_PATH][0]
    start = text.find(RES_OLD)
    if start < 0:
        raise SystemExit("[ABORT] resolver gate not found")
    tail = text.find(RES_TAIL_MARKER, start)
    if tail < 0:
        raise SystemExit("[ABORT] resolver tail marker not found after the gate")
    text = text[:start] + RES_REPLACED + RES_HELPER + text[tail + len(RES_TAIL_MARKER):]
    files[RES_PATH][0] = text

    for path, (text, crlf) in files.items():
        out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(out)
        print("[OK] %-52s crlf=%d bytes=%d" % (path, crlf, len(out)))
    for probe in (u"ObFileSqlLoader::load", u"build_file_table_values_item",
                  u"rename_file_table_columns"):
        print("  resolver contains %-32s -> %s" % (probe, probe in files[RES_PATH][0]))


if "__main__" == __name__:
    main()
