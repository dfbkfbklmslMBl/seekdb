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
#include <unistd.h>
#include <sys/stat.h>
#include <stdlib.h>
#include <stdio.h>
#include <ctype.h>

#include "sql/file_scan/ob_file_scan_utils.h"
#include "lib/oblog/ob_log.h"
#include "share/ob_errno.h"

namespace oceanbase
{
using namespace common;
namespace sql
{

int ObFileScanParam::assign(const ObFileScanParam &other)
{
  int ret = OB_SUCCESS;
  path_ = other.path_;
  format_ = other.format_;
  header_ = other.header_;
  full_infer_ = other.full_infer_;
  max_file_size_ = other.max_file_size_;
  return ret;
}

ObFileReader::ObFileReader(ObIAllocator &alloc)
  : io_alloc_(alloc), path_buf_(nullptr), buf_(nullptr), data_len_(0), pos_(0), eof_(false),
    fd_(-1), file_size_(0), param_(), inited_(false), line_no_(0), bad_lines_(0), started_(false),
    in_quotes_(false), field_(nullptr), field_len_(0), field_cap_(0)
{
}

ObFileReader::~ObFileReader()
{
  destroy();
}

void ObFileReader::destroy()
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

int ObFileReader::init(const ObFileScanParam &param)
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

int ObFileReader::fill_buf(bool &eof_reached)
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

int ObFileReader::append_field_char(char c)
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
int ObFileReader::finish_field(bool quoted, ObFileRow &row)
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
int ObFileReader::parse_record(ObFileRow &row, bool &have_record, const int64_t expect_fields)
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

bool ObFileReader::looks_like_int(const ObString &s)
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

bool ObFileReader::looks_like_double(const ObString &s)
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

bool ObFileReader::looks_like_bool(const ObString &s)
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

bool ObFileReader::looks_like_date(const ObString &s, bool with_time)
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

int ObFileReader::merge_value(const ObString &value, ObFileInferCol &col)
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
common::ObObjType ob_file_infer_final_type(const ObFileInferCol &col)
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

int ObFileReader::infer_schema(ObIArray<ObFileInferCol> &cols)
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
          ObFileInferCol col;
          col.name_ = row.fields_.at(i);
          // duplicate headers get a DuckDB-style suffix instead of overwriting
          for (int64_t k = 0; k < cols.count(); ++k) {
            if (0 == cols.at(k).name_.case_compare(col.name_)) {
              char *buf = nullptr;
              int64_t len = 0;
              if (OB_ISNULL(buf = static_cast<char *>(io_alloc_.alloc(col.name_.length() + 16)))) {
                ret = OB_ALLOCATE_MEMORY_FAILED;
              } else {
                len = snprintf(buf, col.name_.length() + 16, "%.*s__%ld",
                               col.name_.length(), col.name_.ptr(), static_cast<long>(i + 1));
                col.name_.assign_ptr(buf, static_cast<int32_t>(len));
              }
              break;
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

int ObFileReader::open_for_scan()
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

int ObFileReader::next_row(ObFileRow &row)
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

} // namespace sql
} // namespace oceanbase
