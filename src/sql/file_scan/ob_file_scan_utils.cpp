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

#define USING_LOG_PREFIX SQL

#include <fcntl.h>
#include <dirent.h>
#include <errno.h>
#include <unistd.h>
#include <sys/stat.h>
#include <stdlib.h>
#include <stdio.h>
#include <ctype.h>

#include "sql/file_scan/ob_file_scan_utils.h"
#include "common/timezone/ob_time_convert.h"
#include "common/json_type/ob_json_base.h"
#include "common/json_type/ob_json_tree.h"
#include <strings.h>
#include "lib/oblog/ob_log.h"
#include "share/ob_errno.h"

namespace oceanbase
{
using namespace common;
namespace sql
{

int ObFileSqlScanParam::assign(const ObFileSqlScanParam &other)
{
  int ret = OB_SUCCESS;
  path_ = other.path_;
  format_ = other.format_;
  header_ = other.header_;
  full_infer_ = other.full_infer_;
  max_file_size_ = other.max_file_size_;
  return ret;
}

ObFileSqlReader::ObFileSqlReader(ObIAllocator &alloc)
  : io_alloc_(alloc), path_buf_(nullptr), buf_(nullptr), data_len_(0), pos_(0), eof_(false),
    fd_(-1), file_size_(0), param_(), inited_(false), line_no_(0), bad_lines_(0), started_(false),
    in_quotes_(false), field_(nullptr), field_len_(0), field_cap_(0)
{
}

ObFileSqlReader::~ObFileSqlReader()
{
  destroy();
}

void ObFileSqlReader::destroy()
{
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
  if (nullptr != buf_) {
    io_alloc_.free(buf_);
    buf_ = nullptr;
  }
  if (nullptr != field_) {
    io_alloc_.free(field_);
    field_ = nullptr;
  }
  if (nullptr != path_buf_) {
    io_alloc_.free(path_buf_);
    path_buf_ = nullptr;
  }
  data_len_ = 0;
  pos_ = 0;
  eof_ = false;
  field_len_ = 0;
  field_cap_ = 0;
  param_.reset();
  inited_ = false;
}

int ObFileSqlReader::init(const ObFileSqlScanParam &param)
{
  int ret = OB_SUCCESS;
  struct stat st;
  if (OB_UNLIKELY(inited_)) {
    ret = OB_INIT_TWICE;
    LOG_WARN("init twice", K(ret));
  } else if (param.path_.empty()) {
    ret = OB_INVALID_ARGUMENT;
    LOG_WARN("empty path", K(ret), K(param));
  } else if (OB_ISNULL(path_buf_ = static_cast<char *>(
                 io_alloc_.alloc(param.path_.length() + 1)))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
    LOG_WARN("failed to alloc path", K(ret));
  } else if (OB_ISNULL(buf_ = static_cast<char *>(io_alloc_.alloc(CHUNK_SIZE)))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
    LOG_WARN("failed to alloc read buffer", K(ret));
  } else {
    // the syscalls need a NUL-terminated name; ObString carries a length, not a terminator
    MEMCPY(path_buf_, param.path_.ptr(), param.path_.length());
    path_buf_[param.path_.length()] = '\0';
    if (0 != ::stat(path_buf_, &st)) {
      ret = OB_FILE_NOT_EXIST;
      LOG_USER_ERROR(OB_FILE_NOT_EXIST);
      LOG_WARN("failed to stat", K(ret), K(param), K(errno));
    } else if (!S_ISREG(st.st_mode)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_USER_ERROR(OB_INVALID_ARGUMENT);
      LOG_WARN("not a regular file", K(ret), K(param));
    } else if (param.max_file_size_ > 0 && st.st_size > param.max_file_size_) {
      ret = OB_SIZE_OVERFLOW;
      LOG_USER_ERROR(OB_SIZE_OVERFLOW);
      LOG_WARN("file exceeds the configured limit", K(ret), "size", st.st_size, K(param));
    } else if (OB_UNLIKELY((fd_ = ::open(path_buf_, O_RDONLY)) < 0)) {
      ret = OB_IO_ERROR;
      LOG_WARN("failed to open", K(ret), K(param), K(errno));
    } else if (OB_FAIL(param_.assign(param))) {
      LOG_WARN("failed to copy param", K(ret));
    } else {
      // keep the stored path pointing at the NUL-terminated copy for later diagnostics
      param_.path_.assign_ptr(path_buf_, param.path_.length());
      data_len_ = 0;
      pos_ = 0;
      eof_ = false;
      file_size_ = st.st_size;
      line_no_ = 0;
      bad_lines_ = 0;
      started_ = false;
      in_quotes_ = false;
      field_len_ = 0;
      field_cap_ = 0;
      field_ = nullptr;
      inited_ = true;
    }
  }
  if (OB_FAIL(ret)) {
    destroy();
  }
  return ret;
}

int ObFileSqlReader::fill_buf(bool &eof_reached)
{
  int ret = OB_SUCCESS;
  eof_reached = false;
  const int64_t keep = data_len_ - pos_;
  if (keep > 0) {
    MEMMOVE(buf_, buf_ + pos_, keep);
  }
  data_len_ = keep;
  pos_ = 0;
  if (data_len_ >= CHUNK_SIZE) {
    // a single field longer than the chunk cannot be flushed: the caller must grow or reject
    ret = OB_SIZE_OVERFLOW;
    LOG_USER_ERROR(OB_SIZE_OVERFLOW);
    LOG_WARN("no room to refill while a record is open", K(ret), K(data_len_));
  } else {
    const ssize_t n = ::read(fd_, buf_ + data_len_, CHUNK_SIZE - data_len_);
    if (n < 0) {
      ret = OB_IO_ERROR;
      LOG_WARN("read failed", K(ret), K(errno));
    } else if (0 == n) {
      eof_ = true;
      eof_reached = true;
    } else {
      data_len_ += n;
    }
  }
  return ret;
}

int ObFileSqlReader::append_field_char(char c)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(field_len_ >= MAX_FIELD_BYTES)) {
    ret = OB_SIZE_OVERFLOW;
    LOG_USER_ERROR(OB_SIZE_OVERFLOW);
    LOG_WARN("field too long", K(ret), K(field_len_));
  } else if (field_len_ >= field_cap_) {
    const int64_t want = (0 == field_cap_) ? 1024 : (2 * field_cap_);
    char *grown = nullptr;
    if (OB_ISNULL(grown = static_cast<char *>(io_alloc_.alloc(want)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
      LOG_WARN("failed to grow field buffer", K(ret), K(want));
    } else {
      if (field_len_ > 0 && nullptr != field_) {
        MEMCPY(grown, field_, field_len_);
      }
      if (nullptr != field_) {
        io_alloc_.free(field_);
      }
      field_ = grown;
      field_cap_ = want;
    }
  }
  if (OB_SUCC(ret)) {
    field_[field_len_++] = c;
  }
  return ret;
}

// Copy the finished field into its own storage so the read buffer can be refilled freely. An
// unquoted empty field stays "empty" (the caller maps it to NULL); a quoted "" is a real empty
// string, which is why `quoted` has to be carried here.
int ObFileSqlReader::finish_field(bool quoted, ObFileRow &row)
{
  int ret = OB_SUCCESS;
  ObString value;
  if (field_len_ > 0) {
    char *copy = nullptr;
    if (OB_ISNULL(copy = static_cast<char *>(io_alloc_.alloc(field_len_)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
      LOG_WARN("failed to alloc field copy", K(ret), K(field_len_));
    } else {
      MEMCPY(copy, field_, field_len_);
      value.assign_ptr(copy, static_cast<int32_t>(field_len_));
    }
  } else if (quoted) {
    value.assign_ptr("", 0);
  }
  field_len_ = 0;
  if (OB_FAIL(ret)) {
  } else if (row.fields_.count() >= MAX_COLUMNS) {
    ret = OB_ERR_TOO_MANY_COLUMNS;
    LOG_WARN("too many columns", K(ret), "cnt", row.fields_.count());
  } else if (OB_FAIL(row.fields_.push_back(value))) {
    LOG_WARN("failed to push field", K(ret));
  }
  return ret;
}

// One RFC4180-style record. Carries state across chunk boundaries via started_/in_quotes_/field_.
int ObFileSqlReader::parse_record(ObFileRow &row, bool &have_record, const int64_t expect_fields)
{
  int ret = OB_SUCCESS;
  have_record = false;
  bool record_open = started_;
  bool saw_any = !row.fields_.empty();
  bool done = false;
  while (OB_SUCC(ret) && !done) {
    if (pos_ >= data_len_) {
      bool eof_reached = false;
      if (OB_FAIL(fill_buf(eof_reached))) {
        LOG_WARN("failed to refill", K(ret));
      } else if (eof_reached && pos_ >= data_len_) {
        done = true;   // EOF: flush whatever is open as the last record
        if (record_open || saw_any) {
          if (OB_FAIL(finish_field(in_quotes_, row))) {
            LOG_WARN("failed to flush last field", K(ret));
          } else {
            have_record = true;
          }
          started_ = false;
          in_quotes_ = false;
        }
      }
    } else {
      const char c = buf_[pos_];
      if (!started_ && !in_quotes_ && 0 == field_len_ && row.fields_.empty()
          && ('\n' == c || '\r' == c)) {
        // blank line
        ++pos_;
        if ('\r' == c && pos_ < data_len_ && '\n' == buf_[pos_]) {
          ++pos_;
        } else if ('\r' == c && pos_ >= data_len_) {
          bool er = false;
          if (pos_ >= data_len_ && OB_FAIL(fill_buf(er)) && !er) {
            LOG_WARN("failed to peek lf", K(ret));
          } else if (pos_ < data_len_ && '\n' == buf_[pos_]) {
            ++pos_;
          }
        }
        ++line_no_;
      } else {
        started_ = true;
        record_open = true;
        if (in_quotes_) {
          if ('"' == c) {
            ++pos_;
            if (pos_ >= data_len_) {
              bool er = false;
              if (OB_FAIL(fill_buf(er)) && !er) {
                LOG_WARN("failed to peek quote", K(ret));
              }
            }
            if (pos_ < data_len_ && '"' == buf_[pos_]) {
              ++pos_;
              if (OB_FAIL(append_field_char('"'))) {
                LOG_WARN("failed to append quote", K(ret));
              }
            } else {
              in_quotes_ = false;
            }
          } else if (OB_FAIL(append_field_char(c))) {
            LOG_WARN("failed to append", K(ret));
          } else {
            ++pos_;
          }
        } else if ('"' == c && 0 == field_len_) {
          in_quotes_ = true;
          ++pos_;
        } else if (',' == c) {
          ++pos_;
          if (OB_FAIL(finish_field(false, row))) {
            LOG_WARN("failed to finish field", K(ret));
          } else {
            saw_any = true;
          }
        } else if ('\n' == c || '\r' == c) {
          ++pos_;
          if ('\r' == c) {
            if (pos_ >= data_len_) {
              bool er = false;
              if (OB_FAIL(fill_buf(er)) && !er) {
                LOG_WARN("failed to peek crlf", K(ret));
              }
            }
            if (pos_ < data_len_ && '\n' == buf_[pos_]) {
              ++pos_;
            }
          }
          ++line_no_;
          if (OB_FAIL(finish_field(false, row))) {
            LOG_WARN("failed to finish last field", K(ret));
          } else {
            have_record = true;
            started_ = false;
            in_quotes_ = false;
            done = true;
          }
        } else if (OB_FAIL(append_field_char(c))) {
          LOG_WARN("failed to append char", K(ret), K(c));
        } else {
          ++pos_;
        }
      }
    }
  }
  if (OB_SUCC(ret) && have_record && expect_fields > 0
      && row.fields_.count() != expect_fields) {
    ++bad_lines_;   // ragged record: diagnostic for the caller, never a mis-aligned row
  }
  return ret;
}

bool ObFileSqlReader::looks_like_int(const ObString &s)
{
  bool valid = s.length() > 0;
  int64_t i = 0;
  char *end = nullptr;
  if (valid && ('-' == s[0] || '+' == s[0])) {
    i = 1;
  }
  if (valid && i >= s.length()) {
    valid = false;
  }
  for (; valid && i < s.length(); ++i) {
    if (s[i] < '0' || s[i] > '9') {
      valid = false;
    }
  }
  if (valid) {
    // must survive int64, otherwise the column is not an integer column
    errno = 0;
    (void)strtoll(s.ptr(), &end, 10);
    valid = (0 == errno && nullptr != end && 0 == *end);
  }
  return valid;
}

bool ObFileSqlReader::looks_like_double(const ObString &s)
{
  bool dot = false;
  bool digit = false;
  bool exp = false;
  bool valid = s.length() > 0;
  int64_t i = 0;
  if (valid && ('-' == s[0] || '+' == s[0])) {
    i = 1;
  }
  for (; valid && i < s.length(); ++i) {
    const char c = s[i];
    if (c >= '0' && c <= '9') {
      digit = true;
    } else if ('.' == c && !dot && !exp) {
      dot = true;
    } else if (('e' == c || 'E' == c) && digit && !exp) {
      exp = true;
      digit = false;
      if (i + 1 < s.length() && ('-' == s[i + 1] || '+' == s[i + 1])) {
        ++i;
      }
    } else {
      valid = false;
    }
  }
  return valid && digit && (dot || exp);
}

bool ObFileSqlReader::looks_like_bool(const ObString &s)
{
  static const char *WORDS[] = {"true", "false", "t", "f", "yes", "no", "y", "n", "on", "off"};
  bool matched = false;
  char lower[8] = {'\0'};
  if (s.length() > 0 && s.length() < static_cast<int64_t>(sizeof(lower))) {
    for (int64_t i = 0; i < s.length(); ++i) {
      lower[i] = static_cast<char>(tolower(s[i]));
    }
    for (size_t k = 0; !matched && k < sizeof(WORDS) / sizeof(WORDS[0]); ++k) {
      matched = (0 == strcmp(lower, WORDS[k]));
    }
  }
  return matched;
}

bool ObFileSqlReader::looks_like_date(const ObString &s, bool with_time)
{
  bool valid = s.length() >= (with_time ? 19 : 10);
  for (int64_t i = 0; valid && i < 10; ++i) {
    if (4 == i || 7 == i) {
      valid = ('-' == s[i]);
    } else {
      valid = (s[i] >= '0' && s[i] <= '9');
    }
  }
  if (valid && with_time) {
    valid = ('T' == s[10] || ' ' == s[10]);
    for (int64_t i = 11; valid && i < 19; ++i) {
      if (13 == i || 16 == i) {
        valid = (':' == s[i]);
      } else {
        valid = (s[i] >= '0' && s[i] <= '9');
      }
    }
    if (valid) {
      const int64_t hour = (s[11] - '0') * 10 + (s[12] - '0');
      const int64_t minute = (s[14] - '0') * 10 + (s[15] - '0');
      const int64_t second = (s[17] - '0') * 10 + (s[18] - '0');
      valid = hour <= 23 && minute <= 59 && second <= 59;
    }
  }
  if (valid) {
    const int64_t month = (s[5] - '0') * 10 + (s[6] - '0');
    const int64_t day = (s[8] - '0') * 10 + (s[9] - '0');
    valid = month >= 1 && month <= 12 && day >= 1 && day <= 31;
  }
  return valid;
}

int ObFileSqlReader::merge_value(const ObString &value, ObFileSqlInferCol &col)
{
  int ret = OB_SUCCESS;
  if (value.length() > col.max_len_) {
    col.max_len_ = value.length();
  }
  if (value.empty()) {
    col.has_null_ = true;
  } else if (looks_like_bool(value)) {
    col.has_bool_ = true;
  } else if (looks_like_int(value)) {
    col.has_int_ = true;
  } else if (looks_like_double(value)) {
    col.has_double_ = true;
  } else if (looks_like_date(value, true)) {
    col.has_datetime_ = true;
  } else if (looks_like_date(value, false)) {
    col.has_date_ = true;
  } else {
    col.has_varchar_ = true;
  }
  return ret;
}

// Deterministic widening: the most general observed family wins.
common::ObObjType ob_file_sql_infer_final_type(const ObFileSqlInferCol &col)
{
  common::ObObjType type = ObIntType;
  if (col.has_varchar_) {
    type = ObVarcharType;
  } else if (col.has_datetime_ || (col.has_date_ && (col.has_int_ || col.has_bool_))) {
    type = ObDateTimeType;
  } else if (col.has_date_) {
    type = ObDateType;
  } else if (col.has_double_) {
    type = ObDoubleType;
  } else if (col.has_int_) {
    type = ObIntType;
  } else if (col.has_bool_) {
    type = ObTinyIntType;   // BOOLEAN is stored as tinyint(1) in the MySQL layer
  } else {
    type = ObVarcharType;   // all-NULL column: keep it text so later rows can still be read
  }
  return type;
}

int ObFileSqlReader::infer_schema(ObIArray<ObFileSqlInferCol> &cols)
{
  int ret = OB_SUCCESS;
  cols.reset();
  if (OB_UNLIKELY(!inited_)) {
    ret = OB_NOT_INIT;
  } else {
    ObFileRow row;
    bool have = false;
    int64_t expect_fields = -1;
    bool is_header = param_.header_;
    while (OB_SUCC(ret)) {
      row.reset();
      have = false;
      if (OB_FAIL(parse_record(row, have, expect_fields))) {
        if (OB_ITER_END != ret) {
          LOG_WARN("failed to parse record for inference", K(ret));
        } else {
          ret = OB_SUCCESS;
        }
        break;
      } else if (!have) {
        break;
      } else if (is_header) {
        is_header = false;
        expect_fields = row.fields_.count();
        for (int64_t i = 0; OB_SUCC(ret) && i < row.fields_.count(); ++i) {
          ObFileSqlInferCol col;
          col.name_ = row.fields_.at(i);
          // duplicate headers get a DuckDB-style suffix instead of overwriting: the second `id`
          // becomes `id__2`, so the suffix counts how many times the header was already seen
          {
            int64_t occurrences = 0;
            for (int64_t k = 0; k < cols.count(); ++k) {
              const ObString &earlier = cols.at(k).name_;
              bool same_base = false;
              if (earlier.length() == col.name_.length()) {
                same_base = (0 == earlier.case_compare(col.name_));
              } else if (earlier.length() > col.name_.length()
                         && '_' == earlier[col.name_.length()]
                         && '_' == earlier[col.name_.length() + 1]) {
                // an already renamed column (id__2) is still an occurrence of its base name
                same_base = (0 == strncasecmp(earlier.ptr(), col.name_.ptr(), col.name_.length()));
              }
              if (same_base) {
                ++occurrences;
              }
            }
            if (occurrences > 0) {
              char *buf = nullptr;
              int64_t len = 0;
              if (OB_ISNULL(buf = static_cast<char *>(io_alloc_.alloc(col.name_.length() + 16)))) {
                ret = OB_ALLOCATE_MEMORY_FAILED;
              } else {
                len = snprintf(buf, col.name_.length() + 16, "%.*s__%ld",
                               col.name_.length(), col.name_.ptr(),
                               static_cast<long>(occurrences + 1));
                col.name_.assign_ptr(buf, static_cast<int32_t>(len));
              }
            }
          }
          if (OB_FAIL(ret)) {
          } else if (OB_FAIL(cols.push_back(col))) {
            LOG_WARN("failed to push col", K(ret));
          }
        }
      } else if (row.fields_.count() != expect_fields) {
        ++bad_lines_;
      } else {
        for (int64_t i = 0; OB_SUCC(ret) && i < row.fields_.count(); ++i) {
          if (OB_FAIL(merge_value(row.fields_.at(i), cols.at(i)))) {
            LOG_WARN("failed to merge value", K(ret), K(i));
          }
        }
      }
    }
    if (OB_SUCC(ret) && cols.empty()) {
      ret = OB_ERR_TOO_MANY_VALUES;
      LOG_USER_ERROR(OB_ERR_TOO_MANY_VALUES);
      LOG_WARN("no column could be inferred", K(ret), K_(param));
    }
    if (OB_SUCC(ret) && 0 != lseek(fd_, 0, SEEK_SET)) {
      ret = OB_IO_ERROR;
      LOG_WARN("failed to rewind after inference", K(ret), K(errno));
    } else if (OB_SUCC(ret)) {
      data_len_ = 0;
      pos_ = 0;
      eof_ = false;
      line_no_ = 0;
      started_ = false;
      in_quotes_ = false;
      field_len_ = 0;
    }
  }
  return ret;
}

int ObFileSqlReader::open_for_scan()
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(!inited_)) {
    ret = OB_NOT_INIT;
  } else {
    data_len_ = 0;
    pos_ = 0;
    eof_ = false;
    line_no_ = 0;
    started_ = false;
    in_quotes_ = false;
    field_len_ = 0;
    if (param_.header_) {
      // consume the header line so the scan starts at the first data record
      ObFileRow skip;
      bool have = false;
      if (OB_FAIL(parse_record(skip, have, -1))) {
        LOG_WARN("failed to skip header", K(ret));
      }
    }
  }
  return ret;
}

int ObFileSqlReader::next_row(ObFileRow &row)
{
  int ret = OB_SUCCESS;
  bool have = false;
  row.reset();
  const int64_t expect = 0;  // the caller validates the width against its schema
  while (OB_SUCC(ret) && !have) {
    if (OB_FAIL(parse_record(row, have, expect))) {
      LOG_WARN("failed to read record", K(ret));
    } else if (!have) {
      ret = OB_ITER_END;
    } else if (0 == row.fields_.count()) {
      row.reset();
    }
  }
  if (OB_SUCC(ret)) {
    row.line_no_ = line_no_;
  }
  return ret;
}


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
      // the parser caps an identifier at 64 bytes; the __N suffix has to fit inside that
      char buf[64] = {'\0'};
      ObString base = cols.at(i).name_;
      if (base.length() > 56) {
        base.assign_ptr(base.ptr(), 56);
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
          num_buf[text.length()] = '\0';
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
          num_buf[text.length()] = '\0';
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
        char lower[8] = {'\0'};
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
  } else if (OB_FILE_SQL_FMT_JSON == param.format_ || OB_FILE_SQL_FMT_JSONL == param.format_) {
    ret = load_jsonl(alloc, param, data);
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
    dir_buf[slash] = '\0';
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
      prefix_buf[prefix_len] = '\0';
      if (suffix_len > 0) {
        MEMCPY(suffix_buf, pattern.ptr() + pattern.length() - suffix_len, suffix_len);
      }
      suffix_buf[suffix_len] = '\0';
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
            const int64_t dname_len = static_cast<int64_t>(strlen(entry->d_name));
            const int64_t dir_len = static_cast<int64_t>(strlen(dir_buf));
            const int64_t need = dir_len + dname_len + 2;
            char *path_buf = NULL;
            char *full_buf = NULL;
            int written = 0;
            if (OB_ISNULL(path_buf = static_cast<char *>(alloc.alloc(need)))) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else if (FALSE_IT(written = snprintf(path_buf, need, "%s/%s", dir_buf, entry->d_name))) {
            } else if (written <= 0 || written >= need) {
              ret = OB_SIZE_OVERFLOW;
              LOG_WARN("failed to build a glob path", K(ret), K(dir_len), K(dname_len));
            } else if (OB_ISNULL(full_buf = static_cast<char *>(alloc.alloc(written)))) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            }
            if (OB_SUCC(ret)) {
              MEMCPY(full_buf, path_buf, written);
              ObString full;
              full.assign_ptr(full_buf, static_cast<int32_t>(written));
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


// Newline-delimited JSON: one object per line, the union of the keys in first-seen order, and a
// key a line does not carry becomes NULL. Values are kept in their JSON text form so the same
// widening rules that read CSV also decide the column type here.
int ObFileSqlLoader::load_jsonl(ObIAllocator &alloc, const ObFileSqlScanParam &param,
                                ObFileSqlTableData &data)
{
  int ret = OB_SUCCESS;
  const int64_t MAX_JSONL_BYTES = 128LL * 1024 * 1024;
  ObSEArray<ObString, 32> keys;
  ObSEArray<ObFileSqlInferCol, 32> inferred;
  ObFileSqlReader opener(alloc);
  int64_t file_size = 0;
  if (OB_FAIL(opener.init(param))) {
    LOG_WARN("failed to open the json lines file", K(ret), K(param));
  } else {
    file_size = opener.get_file_size();
    opener.destroy();
    char *whole = NULL;
    int64_t read_len = 0;
    if (file_size > MAX_JSONL_BYTES) {
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "a json lines file larger than 128MB as a table");
    } else if (file_size <= 0) {
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "an empty json lines file as a table");
    } else if (OB_ISNULL(whole = static_cast<char *>(alloc.alloc(file_size + 1)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      int fd = -1;
      // the path needs a NUL-terminated copy for the syscalls; the buffer above holds the content
      char *path_buf = NULL;
      if (OB_ISNULL(path_buf = static_cast<char *>(alloc.alloc(param.path_.length() + 1)))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else {
        MEMCPY(path_buf, param.path_.ptr(), param.path_.length());
        path_buf[param.path_.length()] = '\0';
        fd = ::open(path_buf, O_RDONLY);
        if (fd < 0) {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the file cannot be opened");
        }
      }
      while (OB_SUCC(ret) && read_len < file_size) {
        const ssize_t got = ::read(fd, whole + read_len, file_size - read_len);
        if (got < 0) {
          ret = OB_IO_ERROR;
          LOG_WARN("failed to read the json lines file", K(ret), K(read_len));
        } else if (0 == got) {
          break;
        } else {
          read_len += got;
        }
      }
      if (fd >= 0) {
        ::close(fd);
      }
      if (OB_FAIL(ret)) {
      } else {
        whole[read_len] = '\0';
        const ObString content(static_cast<int32_t>(read_len), whole);
        // pass one builds the key union, pass two fills the cells: a column only exists once some
        // line mentioned it, and the inference has to see every line either way
        ObString rest = content;
        while (OB_SUCC(ret) && !rest.empty()) {
          ObString line;
          const char *nl = static_cast<const char *>(memchr(rest.ptr(), '\n', rest.length()));
          if (NULL == nl) {
            line = rest;
            rest = ObString();
          } else {
            line.assign_ptr(rest.ptr(), static_cast<int32_t>(nl - rest.ptr()));
            rest.assign_ptr(const_cast<char *>(nl) + 1,
                            static_cast<int32_t>(rest.length() - line.length() - 1));
          }
          while (!line.empty() && ('\r' == line[line.length() - 1])) {
            line.assign_ptr(line.ptr(), static_cast<int32_t>(line.length() - 1));
          }
          if (line.empty()) {
            // a blank line carries no record
          } else {
            ObIJsonBase *base = NULL;
            if (OB_FAIL(ObJsonBaseFactory::get_json_base(&alloc, line, ObJsonInType::JSON_TREE,
                                                           ObJsonInType::JSON_TREE, base))) {
              LOG_WARN("failed to parse a json line", K(ret), "len", line.length());
            } else if (OB_ISNULL(base) || ObJsonNodeType::J_OBJECT != base->json_type()) {
              ret = OB_INVALID_ARGUMENT;
              LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, a json line is not an object");
            } else {
              ObJsonObject *obj = static_cast<ObJsonObject *>(base);
              const uint64_t member_cnt = obj->member_count();
              for (uint64_t m = 0; OB_SUCC(ret) && m < member_cnt; ++m) {
                ObString key;
                ObJsonNode *value = NULL;
                if (OB_FAIL(obj->get_value_by_idx(m, key, value))) {
                  LOG_WARN("failed to read a json member", K(ret), K(m));
                } else if (OB_ISNULL(value) || key.empty()) {
                  ret = OB_INVALID_ARGUMENT;
                  LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, a json member is unusable");
                } else {
                  bool known = false;
                  for (int64_t k = 0; k < keys.count() && !known; ++k) {
                    known = (0 == keys.at(k).case_compare(key));
                  }
                  if (!known) {
                    ObString copied;
                    ObFileSqlInferCol col;
                    if (OB_FAIL(ob_write_string(alloc, key, copied, true))) {
                      LOG_WARN("failed to copy a json key", K(ret), K(key));
                    } else if (FALSE_IT(col.name_ = copied)) {
                    } else if (OB_FAIL(keys.push_back(copied))) {
                      LOG_WARN("failed to collect a json key", K(ret));
                    } else if (OB_FAIL(inferred.push_back(col))) {
                      LOG_WARN("failed to collect a json column", K(ret));
                    }
                  }
                }
              }
            }
          }
        }
        if (OB_SUCC(ret) && keys.empty()) {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the json lines file has no fields");
        }
        if (OB_SUCC(ret)) {
          data.col_cnt_ = keys.count();
          for (int64_t k = 0; OB_SUCC(ret) && k < keys.count(); ++k) {
            ObFileSqlColumn col;
            col.name_ = keys.at(k);
            col.type_ = ObVarcharType;
            if (OB_FAIL(data.cols_.push_back(col))) {
              LOG_WARN("failed to publish a json column", K(ret), K(k));
            }
          }
        }
        ObString rest2 = content;
        while (OB_SUCC(ret) && !rest2.empty()) {
          ObString line2;
          const char *nl2 = static_cast<const char *>(memchr(rest2.ptr(), '\n', rest2.length()));
          if (NULL == nl2) {
            line2 = rest2;
            rest2 = ObString();
          } else {
            line2.assign_ptr(rest2.ptr(), static_cast<int32_t>(nl2 - rest2.ptr()));
            rest2.assign_ptr(const_cast<char *>(nl2) + 1,
                             static_cast<int32_t>(rest2.length() - line2.length() - 1));
          }
          while (!line2.empty() && ('\r' == line2[line2.length() - 1])) {
            line2.assign_ptr(line2.ptr(), static_cast<int32_t>(line2.length() - 1));
          }
          if (line2.empty()) {
            // a blank line carries no record
          } else {
            ObIJsonBase *base2 = NULL;
            if (OB_FAIL(ObJsonBaseFactory::get_json_base(&alloc, line2, ObJsonInType::JSON_TREE,
                                                           ObJsonInType::JSON_TREE, base2))) {
              LOG_WARN("failed to reparse a json line", K(ret));
            } else if (OB_ISNULL(base2) || ObJsonNodeType::J_OBJECT != base2->json_type()) {
              ret = OB_INVALID_ARGUMENT;
            } else if (data.row_cnt_ >= MAX_ROWS) {
              ret = OB_NOT_SUPPORTED;
              LOG_USER_ERROR(OB_NOT_SUPPORTED, "a file with more than 100000 rows as a table");
            } else {
              ObJsonObject *obj2 = static_cast<ObJsonObject *>(base2);
              for (int64_t k = 0; OB_SUCC(ret) && k < data.col_cnt_; ++k) {
                ObString text;
                ObJsonNode *member = obj2->get_value(keys.at(k));
                if (OB_NOT_NULL(member) && ObJsonNodeType::J_NULL != member->json_type()) {
                  ObStringBuffer buf(&alloc);
                  if (OB_FAIL(member->print(buf, 0))) {
                    LOG_WARN("failed to render a json value", K(ret), K(k));
                  } else {
                    text = buf.string();
                    // print() keeps the JSON quoting of a string, the table holds the value itself
                    if (ObJsonNodeType::J_STRING == member->json_type()
                        && text.length() >= 2 && '"' == text[0] && '"' == text[text.length() - 1]) {
                      text.assign_ptr(text.ptr() + 1, static_cast<int32_t>(text.length() - 2));
                    }
                  }
                }
                ObString stored;
                if (OB_FAIL(ret)) {
                } else if (OB_FAIL(ob_write_string(alloc, text, stored, true))) {
                  LOG_WARN("failed to keep a json value", K(ret), K(k));
                } else if (OB_FAIL(data.cells_.push_back(stored))) {
                  LOG_WARN("failed to collect a json cell", K(ret), K(k));
                } else if (OB_FAIL(ObFileSqlReader::merge_value(text, inferred.at(k)))) {
                  LOG_WARN("failed to widen a json column", K(ret), K(k), K(text));
                }
              }
              if (OB_SUCC(ret)) {
                ++data.row_cnt_;
              }
            }
          }
        }
        if (OB_SUCC(ret)) {
          for (int64_t k = 0; k < data.col_cnt_; ++k) {
            data.cols_.at(k).type_ = ob_file_sql_infer_final_type(inferred.at(k));
            data.cols_.at(k).max_len_ = inferred.at(k).max_len_;
            data.cols_.at(k).nullable_ = inferred.at(k).has_null_;
          }
          if (0 == data.row_cnt_) {
            ret = OB_NOT_SUPPORTED;
            LOG_USER_ERROR(OB_NOT_SUPPORTED, "a file table with no rows");
          }
        }
      }
    }
  }
  return ret;
}

} // namespace sql
} // namespace oceanbase
