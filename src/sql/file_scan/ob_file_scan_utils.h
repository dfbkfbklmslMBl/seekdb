/**
 * Copyright (c) 2026 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#ifndef OCEANBASE_SQL_FILE_SQL_OB_FILE_SQL_UTILS_H_
#define OCEANBASE_SQL_FILE_SQL_OB_FILE_SQL_UTILS_H_

#include "lib/container/ob_se_array.h"
#include "lib/string/ob_string.h"
#include "lib/allocator/page_arena.h"
#include "lib/utility/ob_print_utils.h"
#include "common/object/ob_object.h"

namespace oceanbase
{
namespace sql
{

// These numbers are the value_ the grammar stores for each table function, so the two lists have
// to stay in the same order: READ_CSV, READ_JSON, READ_NDJSON, READ_PARQUET, GLOB.
enum ObFileSqlFormat
{
  OB_FILE_SQL_FMT_CSV = 0,
  OB_FILE_SQL_FMT_JSON = 1,
  OB_FILE_SQL_FMT_JSONL = 2,
  OB_FILE_SQL_FMT_PARQUET = 3,
  OB_FILE_SQL_FMT_GLOB = 4,
};

// Widened column type state. Kept per column, O(1) in file size, so full-file inference never
// materializes rows.
struct ObFileSqlInferCol
{
  ObFileSqlInferCol() : name_(), kind_(common::ObObjType::ObMaxType), max_len_(0), has_null_(false),
                     has_bool_(false), has_int_(false), has_double_(false), has_date_(false),
                     has_datetime_(false), has_varchar_(false) {}
  void reset() { *this = ObFileSqlInferCol(); }
  common::ObString name_;
  common::ObObjType kind_;   // final or in-progress type
  int64_t max_len_;
  bool has_null_;
  bool has_bool_;
  bool has_int_;
  bool has_double_;
  bool has_date_;
  bool has_datetime_;
  bool has_varchar_;
  TO_STRING_KV(K_(name), K_(kind), K_(max_len), K_(has_null), K_(has_bool), K_(has_int),
               K_(has_double), K_(has_date), K_(has_datetime), K_(has_varchar));
};

// Deterministic widening of the observed families: the most general type seen wins.
common::ObObjType ob_file_sql_infer_final_type(const ObFileSqlInferCol &col);

// One parsed record. Fields are copied out of the read buffer as they complete, so refilling the
// buffer can never leave a stale pointer behind.
struct ObFileRow
{
  ObFileRow() : fields_(), line_no_(0) {}
  void reset() { fields_.reset(); line_no_ = 0; }
  bool is_empty() const { return fields_.empty(); }
  common::ObSEArray<common::ObString, 16> fields_;
  int64_t line_no_;
  TO_STRING_KV(K_(line_no), "field_cnt", fields_.count());
};

struct ObFileSqlScanParam
{
  ObFileSqlScanParam() : path_(), format_(OB_FILE_SQL_FMT_CSV), header_(true), full_infer_(true),
                      max_file_size_(0) {}
  int assign(const ObFileSqlScanParam &other);
  void reset()
  {
    path_.reset();
    format_ = OB_FILE_SQL_FMT_CSV;
    header_ = true;
    full_infer_ = true;
    max_file_size_ = 0;
  }
  common::ObString path_;
  ObFileSqlFormat format_;
  bool header_;
  bool full_infer_;
  int64_t max_file_size_;
  TO_STRING_KV(K_(path), K_(format), K_(header), K_(full_infer), K_(max_file_size));
};

// Streaming CSV reader + deterministic schema inference.
// Chunked: at most one record plus the unread tail of the file is resident.
class ObFileSqlReader
{
public:
  explicit ObFileSqlReader(common::ObIAllocator &alloc);
  ~ObFileSqlReader();
  int init(const ObFileSqlScanParam &param);
  void destroy();

  // Whole-file inference pass (sample_size only supports -1). Leaves the reader rewound.
  int infer_schema(common::ObIArray<ObFileSqlInferCol> &cols);
  // Rewind for the scan pass.
  int open_for_scan();
  // Returns OB_ITER_END at EOF. Skips blank lines; a ragged record is counted in bad_lines_ and
  // reported by the caller as a diagnostic, never as a silently wrong row.
  int next_row(ObFileRow &row);
  int64_t get_bad_line_count() const { return bad_lines_; }
  int64_t get_file_size() const { return file_size_; }
  const ObFileSqlScanParam &get_param() const { return param_; }

  static bool looks_like_int(const common::ObString &s);
  static bool looks_like_double(const common::ObString &s);
  static bool looks_like_bool(const common::ObString &s);
  static bool looks_like_date(const common::ObString &s, bool with_time);
  // Merge one observed value into a column's type state.
  static int merge_value(const common::ObString &value, ObFileSqlInferCol &col);

private:
  int fill_buf(bool &eof_reached);
  int parse_record(ObFileRow &row, bool &have_record, const int64_t expect_fields);
  int append_field_char(char c);
  int finish_field(bool quoted, ObFileRow &row);

private:
  static const int64_t CHUNK_SIZE = 64 * 1024;
  static const int64_t MAX_FIELD_BYTES = 32 * 1024 * 1024;
  static const int64_t MAX_COLUMNS = 4096;

  common::ObIAllocator &io_alloc_;   // owns buf_
  char *path_buf_;                   // NUL-terminated copy of param_.path_ for the syscalls
  char *buf_;
  int64_t data_len_;
  int64_t pos_;
  bool eof_;
  int fd_;
  int64_t file_size_;
  ObFileSqlScanParam param_;
  bool inited_;
  int64_t line_no_;
  int64_t bad_lines_;
  // record state carried across chunk boundaries
  bool started_;
  bool in_quotes_;
  char *field_;
  int64_t field_len_;
  int64_t field_cap_;
  DISALLOW_COPY_AND_ASSIGN(ObFileSqlReader);
};


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
  static int load_jsonl(common::ObIAllocator &alloc, const ObFileSqlScanParam &param,
                        ObFileSqlTableData &data);
  DISALLOW_COPY_AND_ASSIGN(ObFileSqlLoader);
};

} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_FILE_SQL_OB_FILE_SQL_UTILS_H_
