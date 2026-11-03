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
#include <stdio.h>
#include <string.h>

#include "sql/file_scan/ob_file_sql_parquet.h"
#include "lib/oblog/ob_log.h"
#include "share/ob_errno.h"

namespace oceanbase
{
using namespace common;
namespace sql
{
namespace
{

// The step the reader was in when it failed. The error the user sees names it, because a parquet
// file can fail in a dozen different places and "Invalid argument" alone says nothing.
enum ObParquetStage
{
  OB_PARQUET_STAGE_START = 0,
  OB_PARQUET_STAGE_FOOTER,
  OB_PARQUET_STAGE_FOOTER_PARSE,
  OB_PARQUET_STAGE_PAGE_HEADER,
  OB_PARQUET_STAGE_LEVELS,
  OB_PARQUET_STAGE_PLAIN,
  OB_PARQUET_STAGE_SNAPPY,
  OB_PARQUET_STAGE_COLUMN_CHUNK,
};

ObParquetStage g_ob_parquet_stage = OB_PARQUET_STAGE_START;

int ob_parquet_stage_error()
{
  switch (g_ob_parquet_stage) {
  case OB_PARQUET_STAGE_FOOTER:
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet footer cannot be read");
    break;
  case OB_PARQUET_STAGE_FOOTER_PARSE:
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet footer is malformed");
    break;
  case OB_PARQUET_STAGE_PAGE_HEADER:
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet page header is malformed");
    break;
  case OB_PARQUET_STAGE_LEVELS:
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet definition levels are malformed");
    break;
  case OB_PARQUET_STAGE_PLAIN:
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet values are malformed");
    break;
  case OB_PARQUET_STAGE_SNAPPY:
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet page cannot be decompressed");
    break;
  case OB_PARQUET_STAGE_COLUMN_CHUNK:
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet column chunk is malformed");
    break;
  default:
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet reader failed");
    break;
  }
  return OB_INVALID_ARGUMENT;
}

// ---------------------------------------------------------------- thrift compact protocol

enum ObThriftCompactType
{
  OB_THRIFT_BOOLEAN_TRUE = 1,
  OB_THRIFT_BOOLEAN_FALSE = 2,
  OB_THRIFT_BYTE = 3,
  OB_THRIFT_I16 = 4,
  OB_THRIFT_I32 = 5,
  OB_THRIFT_I64 = 6,
  OB_THRIFT_DOUBLE = 7,
  OB_THRIFT_BINARY = 8,
  OB_THRIFT_LIST = 9,
  OB_THRIFT_SET = 10,
  OB_THRIFT_MAP = 11,
  OB_THRIFT_STRUCT = 12,
};

// The parquet metadata is thrift compact encoded; this reader walks it field by field so that the
// parts of the format a file table does not need are skipped instead of being materialised.
class ObThriftCompact
{
public:
  ObThriftCompact() : buf_(NULL), len_(0), pos_(0) {}
  ObThriftCompact(const char *buf, int64_t len) : buf_(buf), len_(len), pos_(0) {}

  void reset(const char *buf, int64_t len)
  {
    buf_ = buf;
    len_ = len;
    pos_ = 0;
  }
  int64_t pos() const { return pos_; }
  void seek(int64_t pos) { pos_ = pos; }
  int64_t len() const { return len_; }

  int read_byte(int8_t &value)
  {
    int ret = OB_SUCCESS;
    if (OB_UNLIKELY(pos_ >= len_)) {
      ret = OB_ITER_END;
    } else {
      value = static_cast<int8_t>(buf_[pos_++]);
    }
    return ret;
  }
  int read_varint(uint64_t &value)
  {
    int ret = OB_SUCCESS;
    value = 0;
    int shift = 0;
    while (OB_SUCC(ret)) {
      int8_t byte = 0;
      if (OB_FAIL(read_byte(byte))) {
        break;
      }
      value |= static_cast<uint64_t>(static_cast<uint8_t>(byte) & 0x7f) << shift;
      if (0 == (static_cast<uint8_t>(byte) & 0x80)) {
        break;
      }
      shift += 7;
      if (shift > 63) {
        ret = OB_INVALID_ARGUMENT;
      }
    }
    return ret;
  }
  int read_zigzag(int64_t &value)
  {
    int ret = OB_SUCCESS;
    uint64_t raw = 0;
    if (OB_SUCC(ret = read_varint(raw))) {
      int64_t signed_raw = static_cast<int64_t>(raw);
      value = (signed_raw >> 1) ^ -(signed_raw & 1);
    }
    return ret;
  }
  int read_double(double &value)
  {
    int ret = OB_SUCCESS;
    if (OB_UNLIKELY(pos_ + 8 > len_)) {
      ret = OB_ITER_END;
    } else {
      uint64_t bits = 0;
      MEMCPY(&bits, buf_ + pos_, 8);
      pos_ += 8;
      memcpy(&value, &bits, 8);
    }
    return ret;
  }
  int read_binary(const char *&data, int64_t &length)
  {
    int ret = OB_SUCCESS;
    uint64_t size = 0;
    if (OB_SUCC(ret = read_varint(size))) {
      if (OB_UNLIKELY(pos_ + static_cast<int64_t>(size) > len_)) {
        ret = OB_ITER_END;
      } else {
        data = buf_ + pos_;
        length = static_cast<int64_t>(size);
        pos_ += static_cast<int64_t>(size);
      }
    }
    return ret;
  }
  // Reads the header of the next field of a struct. The stop marker is a zero byte.
  int read_field(int16_t &id, int8_t &type, int16_t &last_id, bool &end)
  {
    int ret = OB_SUCCESS;
    int8_t header = 0;
    if (OB_FAIL(read_byte(header))) {
      return ret;
    }
    if (0 == header) {
      end = true;
      return ret;
    }
    end = false;
    const int8_t delta = (header & 0xf0) >> 4;
    type = header & 0x0f;
    if (0 == delta) {
      uint64_t raw = 0;
      if (OB_FAIL(read_varint(raw))) {
        return ret;
      }
      id = static_cast<int16_t>(raw);
    } else {
      id = static_cast<int16_t>(last_id + delta);
    }
    last_id = id;
    return ret;
  }
  int read_list_header(int64_t &size, int8_t &elem_type)
  {
    int ret = OB_SUCCESS;
    int8_t header = 0;
    if (OB_FAIL(read_byte(header))) {
      return ret;
    }
    size = (header & 0xf0) >> 4;
    elem_type = header & 0x0f;
    if (15 == size) {
      uint64_t raw = 0;
      if (OB_FAIL(read_varint(raw))) {
        return ret;
      }
      size = static_cast<int64_t>(raw);
    }
    return ret;
  }
  int skip_bytes(int64_t count)
  {
    int ret = OB_SUCCESS;
    if (OB_UNLIKELY(pos_ + count > len_ || count < 0)) {
      ret = OB_ITER_END;
    } else {
      pos_ += count;
    }
    return ret;
  }
  int skip_value(int8_t type)
  {
    int ret = OB_SUCCESS;
    switch (type) {
    case OB_THRIFT_BOOLEAN_TRUE:
    case OB_THRIFT_BOOLEAN_FALSE:
      break;
    case OB_THRIFT_BYTE: {
      int8_t value = 0;
      ret = read_byte(value);
      break;
    }
    case OB_THRIFT_I16:
    case OB_THRIFT_I32:
    case OB_THRIFT_I64: {
      uint64_t value = 0;
      ret = read_varint(value);
      break;
    }
    case OB_THRIFT_DOUBLE:
      ret = skip_bytes(8);
      break;
    case OB_THRIFT_BINARY: {
      const char *data = NULL;
      int64_t length = 0;
      ret = read_binary(data, length);
      break;
    }
    case OB_THRIFT_LIST:
    case OB_THRIFT_SET: {
      int64_t size = 0;
      int8_t elem_type = 0;
      if (OB_SUCC(ret = read_list_header(size, elem_type))) {
        for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
          ret = skip_value(elem_type);
        }
      }
      break;
    }
    case OB_THRIFT_MAP: {
      int64_t size = 0;
      int8_t key_type = 0;
      int8_t value_type = 0;
      if (OB_SUCC(ret = read_list_header(size, key_type))) {
        value_type = 0;
        ret = read_byte(value_type);
        for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
          ret = skip_value(key_type);
          if (OB_SUCC(ret)) {
            ret = skip_value(value_type);
          }
        }
      }
      break;
    }
    case OB_THRIFT_STRUCT: {
      int16_t id = 0;
      int16_t last_id = 0;
      int8_t field_type = 0;
      bool end = false;
      while (OB_SUCC(ret) && !end) {
        if (OB_FAIL(ret = read_field(id, field_type, last_id, end))) {
          break;
        }
        if (!end) {
          ret = skip_value(field_type);
        }
      }
      break;
    }
    default:
      ret = OB_INVALID_ARGUMENT;
      break;
    }
    return ret;
  }

private:
  const char *buf_;
  int64_t len_;
  int64_t pos_;
};

int ob_parquet_copy_string(ObIAllocator &alloc, const char *data, int64_t length, ObString &out)
{
  int ret = OB_SUCCESS;
  void *buffer = NULL;
  if (OB_ISNULL(buffer = alloc.alloc(length > 0 ? length : 1))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    if (length > 0) {
      MEMCPY(buffer, data, length);
    }
    out.assign(static_cast<char *>(buffer), length);
  }
  return ret;
}

// ---------------------------------------------------------------- snappy

// The raw snappy format: a varint with the uncompressed length, then literal and copy tags.
int ob_parquet_snappy_uncompress(const char *src, int64_t src_len, char *dst, int64_t dst_len)
{
  int ret = OB_SUCCESS;
  ObThriftCompact reader(src, src_len);
  uint64_t expected = 0;
  if (OB_FAIL(reader.read_varint(expected))) {
    ret = OB_INVALID_ARGUMENT;
  } else if (static_cast<int64_t>(expected) != dst_len) {
    ret = OB_INVALID_ARGUMENT;
  } else {
    // the preamble has already been read, the tags start behind it
    int64_t sp = reader.pos();
    int64_t dp = 0;
    while (OB_SUCC(ret) && sp < src_len) {
      const uint8_t tag = static_cast<uint8_t>(src[sp++]);
      const int type = tag & 0x03;
      if (0 == type) {
        uint64_t length = tag >> 2;
        if (length >= 60) {
          const int64_t extra = static_cast<int64_t>(length) - 59;
          if (OB_UNLIKELY(sp + extra > src_len)) {
            ret = OB_INVALID_ARGUMENT;
            break;
          }
          length = 0;
          for (int64_t i = 0; i < extra; ++i) {
            length |= static_cast<uint64_t>(static_cast<uint8_t>(src[sp + i])) << (8 * i);
          }
          sp += extra;
        }
        ++length;
        if (OB_UNLIKELY(sp + static_cast<int64_t>(length) > src_len
                        || dp + static_cast<int64_t>(length) > dst_len)) {
          ret = OB_INVALID_ARGUMENT;
          break;
        }
        MEMCPY(dst + dp, src + sp, length);
        sp += static_cast<int64_t>(length);
        dp += static_cast<int64_t>(length);
      } else {
        uint64_t length = 0;
        uint64_t offset = 0;
        if (1 == type) {
          length = 4 + ((tag >> 2) & 0x07);
          if (OB_UNLIKELY(sp + 1 > src_len)) {
            ret = OB_INVALID_ARGUMENT;
            break;
          }
          offset = (static_cast<uint64_t>(tag >> 5) << 8) | static_cast<uint8_t>(src[sp]);
          sp += 1;
        } else if (2 == type) {
          length = (tag >> 2) + 1;
          if (OB_UNLIKELY(sp + 2 > src_len)) {
            ret = OB_INVALID_ARGUMENT;
            break;
          }
          offset = static_cast<uint64_t>(static_cast<uint8_t>(src[sp]))
                   | (static_cast<uint64_t>(static_cast<uint8_t>(src[sp + 1])) << 8);
          sp += 2;
        } else {
          length = (tag >> 2) + 1;
          if (OB_UNLIKELY(sp + 4 > src_len)) {
            ret = OB_INVALID_ARGUMENT;
            break;
          }
          offset = static_cast<uint64_t>(static_cast<uint8_t>(src[sp]))
                   | (static_cast<uint64_t>(static_cast<uint8_t>(src[sp + 1])) << 8)
                   | (static_cast<uint64_t>(static_cast<uint8_t>(src[sp + 2])) << 16)
                   | (static_cast<uint64_t>(static_cast<uint8_t>(src[sp + 3])) << 24);
          sp += 4;
        }
        if (OB_UNLIKELY(0 == offset || offset > static_cast<uint64_t>(dp)
                        || dp + static_cast<int64_t>(length) > dst_len)) {
          ret = OB_INVALID_ARGUMENT;
          break;
        }
        const int64_t start = dp - static_cast<int64_t>(offset);
        for (uint64_t i = 0; i < length; ++i) {
          dst[dp++] = dst[start + static_cast<int64_t>(i)];
        }
      }
    }
    if (OB_SUCC(ret) && dp != dst_len) {
      ret = OB_INVALID_ARGUMENT;
    }
  }
  return ret;
}

// ---------------------------------------------------------------- RLE / bit packed hybrid

// Decodes `count` values of `bit_width` bits from an RLE/bit-packed hybrid stream. This is the
// encoding the definition levels and the dictionary indices use.
int ob_parquet_rle_decode(const char *src, int64_t src_len, int64_t bit_width, int64_t count,
                          int64_t *out)
{
  int ret = OB_SUCCESS;
  int64_t sp = 0;
  int64_t produced = 0;
  while (OB_SUCC(ret) && produced < count) {
    if (OB_UNLIKELY(sp >= src_len)) {
      ret = OB_INVALID_ARGUMENT;
      break;
    }
    uint64_t header = 0;
    int shift = 0;
    while (true) {
      if (OB_UNLIKELY(sp >= src_len)) {
        ret = OB_INVALID_ARGUMENT;
        break;
      }
      const uint8_t byte = static_cast<uint8_t>(src[sp++]);
      header |= static_cast<uint64_t>(byte & 0x7f) << shift;
      if (0 == (byte & 0x80)) {
        break;
      }
      shift += 7;
    }
    if (OB_FAIL(ret)) {
      break;
    }
    if (0 == (header & 1)) {
      // an RLE run: the value repeated, stored in ceil(bit_width / 8) bytes
      const int64_t run = static_cast<int64_t>(header >> 1);
      const int64_t width_bytes = (bit_width + 7) / 8;
      if (OB_UNLIKELY(sp + width_bytes > src_len)) {
        ret = OB_INVALID_ARGUMENT;
        break;
      }
      uint64_t value = 0;
      for (int64_t i = 0; i < width_bytes; ++i) {
        value |= static_cast<uint64_t>(static_cast<uint8_t>(src[sp + i])) << (8 * i);
      }
      sp += width_bytes;
      for (int64_t i = 0; i < run && produced < count; ++i) {
        out[produced++] = static_cast<int64_t>(value);
      }
    } else {
      // a bit-packed run: groups of eight values, least significant bit first
      const int64_t groups = static_cast<int64_t>(header >> 1);
      const int64_t values = groups * 8;
      const int64_t bytes = (values * bit_width + 7) / 8;
      if (OB_UNLIKELY(sp + bytes > src_len)) {
        ret = OB_INVALID_ARGUMENT;
        break;
      }
      for (int64_t g = 0; g < groups && produced < count; ++g) {
        for (int64_t v = 0; v < 8 && produced < count; ++v) {
          const int64_t bit_index = (g * 8 + v) * bit_width;
          uint64_t value = 0;
          for (int64_t b = 0; b < bit_width; ++b) {
            const int64_t absolute = bit_index + b;
            const uint8_t byte = static_cast<uint8_t>(src[sp + absolute / 8]);
            value |= static_cast<uint64_t>((byte >> (absolute % 8)) & 1) << b;
          }
          out[produced++] = static_cast<int64_t>(value);
        }
      }
      sp += bytes;
    }
  }
  return ret;
}

// ---------------------------------------------------------------- metadata

struct ObParquetSchemaElement
{
  ObParquetSchemaElement()
      : type_(-1), repetition_type_(OB_PARQUET_REPETITION_REQUIRED), num_children_(0),
        type_length_(0), name_()
  {}
  int32_t type_;
  int32_t repetition_type_;
  int32_t num_children_;
  int32_t type_length_;
  ObString name_;
};

struct ObParquetColumnMeta
{
  ObParquetColumnMeta()
      : type_(-1), num_values_(0), codec_(OB_PARQUET_CODEC_UNCOMPRESSED), encoding_(OB_PARQUET_ENC_PLAIN),
        data_page_offset_(-1), dictionary_page_offset_(-1), total_compressed_size_(0), path_()
  {}
  int32_t type_;
  int64_t num_values_;
  int32_t codec_;
  int32_t encoding_;
  int64_t data_page_offset_;
  int64_t dictionary_page_offset_;
  int64_t total_compressed_size_;
  ObString path_;
};

// One leaf column of the schema, with the levels its values are nested under.
struct ObParquetLeaf
{
  ObParquetLeaf() : name_(), type_(-1), type_length_(0), max_def_level_(0), max_rep_level_(0), nullable_(false) {}
  ObString name_;
  int32_t type_;
  int32_t type_length_;
  int32_t max_def_level_;
  int32_t max_rep_level_;
  bool nullable_;
};

// A growable array of strings held in one allocator block.
class ObParquetStrVec
{
public:
  ObParquetStrVec() : items_(NULL), count_(0), capacity_(0) {}
  int init(ObIAllocator &alloc, int64_t capacity)
  {
    int ret = OB_SUCCESS;
    items_ = NULL;
    count_ = 0;
    capacity_ = capacity;
    if (capacity > 0) {
      if (OB_ISNULL(items_ = static_cast<ObString *>(alloc.alloc(sizeof(ObString) * capacity)))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
        capacity_ = 0;
      }
    }
    return ret;
  }
  int push_back(const ObString &value)
  {
    int ret = OB_SUCCESS;
    if (count_ >= capacity_) {
      ret = OB_SIZE_OVERFLOW;
    } else {
      items_[count_++] = value;
    }
    return ret;
  }
  int64_t count() const { return count_; }
  const ObString &at(int64_t index) const { return items_[index]; }
private:
  ObString *items_;
  int64_t count_;
  int64_t capacity_;
};

int ob_parquet_parse_schema_element(ObThriftCompact &reader, ObParquetSchemaElement &element)
{
  int ret = OB_SUCCESS;
  int16_t id = 0;
  int16_t last_id = 0;
  int8_t type = 0;
  bool end = false;
  while (OB_SUCC(ret) && !end) {
    if (OB_FAIL(ret = reader.read_field(id, type, last_id, end))) {
      break;
    }
    if (end) {
      break;
    }
    switch (id) {
    case 1: { // type
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      element.type_ = static_cast<int32_t>(value);
      break;
    }
    case 2: { // type_length
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      element.type_length_ = static_cast<int32_t>(value);
      break;
    }
    case 3: { // repetition_type
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      element.repetition_type_ = static_cast<int32_t>(value);
      break;
    }
    case 4: { // name
      const char *data = NULL;
      int64_t length = 0;
      if (OB_SUCC(ret = reader.read_binary(data, length))) {
        element.name_.assign(const_cast<char *>(data), length);
      }
      break;
    }
    case 5: { // num_children
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      element.num_children_ = static_cast<int32_t>(value);
      break;
    }
    default:
      ret = reader.skip_value(type);
      break;
    }
  }
  return ret;
}

int ob_parquet_parse_column_meta(ObThriftCompact &reader, ObParquetColumnMeta &meta)
{
  int ret = OB_SUCCESS;
  int16_t id = 0;
  int16_t last_id = 0;
  int8_t type = 0;
  bool end = false;
  while (OB_SUCC(ret) && !end) {
    if (OB_FAIL(ret = reader.read_field(id, type, last_id, end))) {
      break;
    }
    if (end) {
      break;
    }
    switch (id) {
    case 1: { // type
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      meta.type_ = static_cast<int32_t>(value);
      break;
    }
    case 2: { // encodings
      int64_t size = 0;
      int8_t elem_type = 0;
      if (OB_SUCC(ret = reader.read_list_header(size, elem_type))) {
        for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
          int64_t value = 0;
          if (OB_FAIL(ret = reader.read_zigzag(value))) {
            break;
          }
          if (OB_PARQUET_ENC_RLE_DICTIONARY == value || OB_PARQUET_ENC_PLAIN_DICTIONARY == value) {
            meta.encoding_ = OB_PARQUET_ENC_RLE_DICTIONARY;
          } else if (OB_PARQUET_ENC_PLAIN == value) {
            meta.encoding_ = OB_PARQUET_ENC_PLAIN;
          }
        }
      }
      break;
    }
    case 3: { // path_in_schema
      int64_t size = 0;
      int8_t elem_type = 0;
      if (OB_SUCC(ret = reader.read_list_header(size, elem_type))) {
        for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
          const char *data = NULL;
          int64_t length = 0;
          if (OB_FAIL(ret = reader.read_binary(data, length))) {
            break;
          }
          if (i == size - 1) {
            meta.path_.assign(const_cast<char *>(data), length);
          }
        }
      }
      break;
    }
    case 4: { // codec
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      meta.codec_ = static_cast<int32_t>(value);
      break;
    }
    case 5: { // num_values
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      meta.num_values_ = value;
      break;
    }
    case 7: { // total_compressed_size
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      meta.total_compressed_size_ = value;
      break;
    }
    case 9: { // data_page_offset
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      meta.data_page_offset_ = value;
      break;
    }
    case 10: { // dictionary_page_offset
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      meta.dictionary_page_offset_ = value;
      break;
    }
    default:
      ret = reader.skip_value(type);
      break;
    }
  }
  return ret;
}

int ob_parquet_parse_column_chunk(ObThriftCompact &reader, ObParquetColumnMeta &meta)
{
  int ret = OB_SUCCESS;
  int16_t id = 0;
  int16_t last_id = 0;
  int8_t type = 0;
  bool end = false;
  while (OB_SUCC(ret) && !end) {
    if (OB_FAIL(ret = reader.read_field(id, type, last_id, end))) {
      break;
    }
    if (end) {
      break;
    }
    if (3 == id) { // meta_data
      ret = ob_parquet_parse_column_meta(reader, meta);
    } else {
      ret = reader.skip_value(type);
    }
  }
  return ret;
}

int ob_parquet_parse_row_group(ObThriftCompact &reader, ObParquetStrVec &chunk_meta_storage,
                               ObParquetStrVec &chunk_paths, ObSEArray<ObParquetColumnMeta, 8> &columns)
{
  int ret = OB_SUCCESS;
  int16_t id = 0;
  int16_t last_id = 0;
  int8_t type = 0;
  bool end = false;
  while (OB_SUCC(ret) && !end) {
    if (OB_FAIL(ret = reader.read_field(id, type, last_id, end))) {
      break;
    }
    if (end) {
      break;
    }
    if (1 == id) { // columns
      int64_t size = 0;
      int8_t elem_type = 0;
      if (OB_SUCC(ret = reader.read_list_header(size, elem_type))) {
        for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
          ObParquetColumnMeta meta;
          if (OB_FAIL(ret = ob_parquet_parse_column_chunk(reader, meta))) {
            break;
          }
          if (OB_SUCC(ret) && OB_FAIL(ret = columns.push_back(meta))) {
            LOG_WARN("failed to collect a column chunk", K(ret), K(i));
          }
        }
      }
    } else {
      ret = reader.skip_value(type);
    }
  }
  return ret;
}

// ---------------------------------------------------------------- page decoding

struct ObParquetPageHeader
{
  ObParquetPageHeader()
      : type_(0), uncompressed_size_(0), compressed_size_(0), num_values_(0), encoding_(OB_PARQUET_ENC_PLAIN),
        def_levels_length_(0), rep_levels_length_(0), is_compressed_(true), is_v2_(false)
  {}
  int32_t type_;
  int64_t uncompressed_size_;
  int64_t compressed_size_;
  int64_t num_values_;
  int32_t encoding_;
  int64_t def_levels_length_;
  int64_t rep_levels_length_;
  bool is_compressed_;
  bool is_v2_;
};

int ob_parquet_parse_page_header(ObThriftCompact &reader, ObParquetPageHeader &header)
{
  int ret = OB_SUCCESS;
  int16_t id = 0;
  int16_t last_id = 0;
  int8_t type = 0;
  bool end = false;
  while (OB_SUCC(ret) && !end) {
    if (OB_FAIL(ret = reader.read_field(id, type, last_id, end))) {
      break;
    }
    if (end) {
      break;
    }
    switch (id) {
    case 1: { // type
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      header.type_ = static_cast<int32_t>(value);
      break;
    }
    case 2: { // uncompressed_page_size
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      header.uncompressed_size_ = value;
      break;
    }
    case 3: { // compressed_page_size
      int64_t value = 0;
      ret = reader.read_zigzag(value);
      header.compressed_size_ = value;
      break;
    }
    case 5: { // data_page_header (version 1)
      int16_t sub_id = 0;
      int16_t sub_last_id = 0;
      int8_t sub_type = 0;
      bool sub_end = false;
      while (OB_SUCC(ret) && !sub_end) {
        if (OB_FAIL(ret = reader.read_field(sub_id, sub_type, sub_last_id, sub_end))) {
          break;
        }
        if (sub_end) {
          break;
        }
        if (1 == sub_id) { // num_values
          int64_t value = 0;
          ret = reader.read_zigzag(value);
          header.num_values_ = value;
        } else if (2 == sub_id) { // encoding
          int64_t value = 0;
          ret = reader.read_zigzag(value);
          header.encoding_ = static_cast<int32_t>(value);
        } else {
          ret = reader.skip_value(sub_type);
        }
      }
      break;
    }
    case 8: { // data_page_header_v2
      header.is_v2_ = true;
      int16_t sub_id = 0;
      int16_t sub_last_id = 0;
      int8_t sub_type = 0;
      bool sub_end = false;
      while (OB_SUCC(ret) && !sub_end) {
        if (OB_FAIL(ret = reader.read_field(sub_id, sub_type, sub_last_id, sub_end))) {
          break;
        }
        if (sub_end) {
          break;
        }
        switch (sub_id) {
        case 1: { // num_values
          int64_t value = 0;
          ret = reader.read_zigzag(value);
          header.num_values_ = value;
          break;
        }
        case 4: { // encoding
          int64_t value = 0;
          ret = reader.read_zigzag(value);
          header.encoding_ = static_cast<int32_t>(value);
          break;
        }
        case 5: { // definition_levels_byte_length
          int64_t value = 0;
          ret = reader.read_zigzag(value);
          header.def_levels_length_ = value;
          break;
        }
        case 6: { // repetition_levels_byte_length
          int64_t value = 0;
          ret = reader.read_zigzag(value);
          header.rep_levels_length_ = value;
          break;
        }
        case 7: { // is_compressed: a thrift compact boolean lives in the field header, not in a byte
          header.is_compressed_ = (OB_THRIFT_BOOLEAN_TRUE == sub_type);
          break;
        }
        default:
          ret = reader.skip_value(sub_type);
          break;
        }
      }
      break;
    }
    default:
      ret = reader.skip_value(type);
      break;
    }
  }
  return ret;
}

// Formats one value of a physical type as the text a file table cell holds.
int ob_parquet_format_value(const char *data, int64_t length, int32_t type, int32_t type_length,
                            ObIAllocator &alloc, ObString &out)
{
  int ret = OB_SUCCESS;
  char buffer[64];
  switch (type) {
  case OB_PARQUET_TYPE_BOOLEAN: {
    const char *text = (0 != length && 0 != static_cast<uint8_t>(data[0])) ? "true" : "false";
    ret = ob_parquet_copy_string(alloc, text, static_cast<int64_t>(strlen(text)), out);
    break;
  }
  case OB_PARQUET_TYPE_INT32: {
    int32_t value = 0;
    if (length < 4) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      uint32_t bits = 0;
      MEMCPY(&bits, data, 4);
      value = static_cast<int32_t>(bits);
      const int written = snprintf(buffer, sizeof(buffer), "%d", value);
      ret = ob_parquet_copy_string(alloc, buffer, written, out);
    }
    break;
  }
  case OB_PARQUET_TYPE_INT64: {
    int64_t value = 0;
    if (length < 8) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      uint64_t bits = 0;
      MEMCPY(&bits, data, 8);
      value = static_cast<int64_t>(bits);
      const int written = snprintf(buffer, sizeof(buffer), "%lld", value);
      ret = ob_parquet_copy_string(alloc, buffer, written, out);
    }
    break;
  }
  case OB_PARQUET_TYPE_FLOAT: {
    float value = 0.0f;
    if (length < 4) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      uint32_t bits = 0;
      MEMCPY(&bits, data, 4);
      memcpy(&value, &bits, 4);
      const int written = snprintf(buffer, sizeof(buffer), "%.9g", value);
      ret = ob_parquet_copy_string(alloc, buffer, written, out);
    }
    break;
  }
  case OB_PARQUET_TYPE_DOUBLE: {
    double value = 0.0;
    if (length < 8) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      uint64_t bits = 0;
      MEMCPY(&bits, data, 8);
      memcpy(&value, &bits, 8);
      const int written = snprintf(buffer, sizeof(buffer), "%.17g", value);
      ret = ob_parquet_copy_string(alloc, buffer, written, out);
    }
    break;
  }
  case OB_PARQUET_TYPE_BYTE_ARRAY:
  case OB_PARQUET_TYPE_FIXED_LEN_BYTE_ARRAY:
  case OB_PARQUET_TYPE_INT96: {
    // a byte array is the text of the cell; a fixed length array is its first type_length bytes
    const int64_t keep = (OB_PARQUET_TYPE_FIXED_LEN_BYTE_ARRAY == type && type_length > 0 && length > type_length)
                         ? type_length
                         : length;
    ret = ob_parquet_copy_string(alloc, data, keep, out);
    break;
  }
  default:
    ret = OB_NOT_SUPPORTED;
    break;
  }
  return ret;
}

// Decodes the PLAIN encoding of `count` values.
int ob_parquet_decode_plain(const char *data, int64_t length, int32_t type, int32_t type_length,
                            int64_t count, ObIAllocator &alloc, ObParquetStrVec &out)
{
  int ret = OB_SUCCESS;
  int64_t offset = 0;
  for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
    int64_t item_length = 0;
    switch (type) {
    case OB_PARQUET_TYPE_BOOLEAN:
      item_length = 1;
      break;
    case OB_PARQUET_TYPE_INT32:
    case OB_PARQUET_TYPE_FLOAT:
      item_length = 4;
      break;
    case OB_PARQUET_TYPE_INT64:
    case OB_PARQUET_TYPE_DOUBLE:
      item_length = 8;
      break;
    case OB_PARQUET_TYPE_INT96:
      item_length = 12;
      break;
    case OB_PARQUET_TYPE_FIXED_LEN_BYTE_ARRAY:
      item_length = type_length;
      break;
    case OB_PARQUET_TYPE_BYTE_ARRAY: {
      if (OB_UNLIKELY(offset + 4 > length)) {
        ret = OB_INVALID_ARGUMENT;
        break;
      }
      uint32_t size = 0;
      MEMCPY(&size, data + offset, 4);
      offset += 4;
      item_length = static_cast<int64_t>(size);
      break;
    }
    default:
      ret = OB_NOT_SUPPORTED;
      break;
    }
    if (OB_FAIL(ret)) {
      break;
    }
    if (OB_UNLIKELY(offset + item_length > length)) {
      ret = OB_INVALID_ARGUMENT;
      break;
    }
    ObString text;
    if (OB_FAIL(ret = ob_parquet_format_value(data + offset, item_length, type, type_length, alloc, text))) {
      break;
    }
    if (OB_FAIL(ret = out.push_back(text))) {
      break;
    }
    offset += item_length;
  }
  return ret;
}

// Decodes the PLAIN encoding of every value the buffer holds. A dictionary page does not carry a
// value count, so its size is the only bound.
int ob_parquet_decode_plain_all(const char *data, int64_t length, int32_t type, int32_t type_length,
                                ObIAllocator &alloc, ObParquetStrVec &out)
{
  int ret = OB_SUCCESS;
  int64_t offset = 0;
  while (OB_SUCC(ret) && offset < length) {
    int64_t item_length = 0;
    switch (type) {
    case OB_PARQUET_TYPE_BOOLEAN:
      item_length = 1;
      break;
    case OB_PARQUET_TYPE_INT32:
    case OB_PARQUET_TYPE_FLOAT:
      item_length = 4;
      break;
    case OB_PARQUET_TYPE_INT64:
    case OB_PARQUET_TYPE_DOUBLE:
      item_length = 8;
      break;
    case OB_PARQUET_TYPE_INT96:
      item_length = 12;
      break;
    case OB_PARQUET_TYPE_FIXED_LEN_BYTE_ARRAY:
      item_length = type_length;
      break;
    case OB_PARQUET_TYPE_BYTE_ARRAY: {
      if (OB_UNLIKELY(offset + 4 > length)) {
        ret = OB_INVALID_ARGUMENT;
        break;
      }
      uint32_t size = 0;
      MEMCPY(&size, data + offset, 4);
      offset += 4;
      item_length = static_cast<int64_t>(size);
      break;
    }
    default:
      ret = OB_NOT_SUPPORTED;
      break;
    }
    if (OB_FAIL(ret)) {
      break;
    }
    if (OB_UNLIKELY(offset + item_length > length)) {
      ret = OB_INVALID_ARGUMENT;
      break;
    }
    ObString text;
    if (OB_FAIL(ret = ob_parquet_format_value(data + offset, item_length, type, type_length, alloc, text))) {
      break;
    }
    if (OB_FAIL(ret = out.push_back(text))) {
      break;
    }
    offset += item_length;
  }
  return ret;
}

// The width of the RLE encoded levels: enough bits for the largest level.
int64_t ob_parquet_level_bit_width(int32_t max_level)
{
  int64_t width = 0;
  while ((1 << width) <= max_level) {
    ++width;
  }
  return width;
}

// Decodes the values of one page and appends them, leaving a NULL where the definition level says
// the value is absent.
int ob_parquet_decode_page_values(const char *body, int64_t body_len, const ObParquetPageHeader &header,
                                   const ObParquetLeaf &leaf, ObIAllocator &alloc,
                                   const ObParquetStrVec &dictionary, ObParquetStrVec &out)
{
  int ret = OB_SUCCESS;
  int64_t levels_length = 0;
  const char *values = NULL;
  int64_t values_length = 0;
  const int64_t num_values = header.num_values_;
  int64_t *def_levels = NULL;

  if (header.is_v2_) {
    // version 2 keeps the levels outside the compressed part
    levels_length = header.rep_levels_length_ + header.def_levels_length_;
    if (OB_UNLIKELY(levels_length > body_len)) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      values = body + levels_length;
      values_length = body_len - levels_length;
    }
  } else {
    // version 1 compresses the levels with the values and prefixes each level block with its length
    int64_t offset = 0;
    if (leaf.max_rep_level_ > 0) {
      if (OB_UNLIKELY(offset + 4 > body_len)) {
        ret = OB_INVALID_ARGUMENT;
      } else {
        uint32_t length = 0;
        MEMCPY(&length, body + offset, 4);
        offset += 4 + static_cast<int64_t>(length);
      }
    }
    if (OB_SUCC(ret) && leaf.max_def_level_ > 0) {
      if (OB_UNLIKELY(offset + 4 > body_len)) {
        ret = OB_INVALID_ARGUMENT;
      } else {
        uint32_t length = 0;
        MEMCPY(&length, body + offset, 4);
        offset += 4 + static_cast<int64_t>(length);
      }
    }
    if (OB_SUCC(ret) && OB_UNLIKELY(offset > body_len)) {
      ret = OB_INVALID_ARGUMENT;
    } else if (OB_SUCC(ret)) {
      values = body + offset;
      values_length = body_len - offset;
    }
  }
  if (OB_SUCC(ret) && leaf.max_def_level_ > 0) {
    // the definition levels decide which of the values are present
    g_ob_parquet_stage = OB_PARQUET_STAGE_LEVELS;
    const int64_t width = ob_parquet_level_bit_width(leaf.max_def_level_);
    int64_t block_start = header.is_v2_ ? header.rep_levels_length_ : 0;
    int64_t block_length = header.is_v2_ ? header.def_levels_length_ : 0;
    if (!header.is_v2_) {
      // version 1 stores the length of the definition level block in front of it
      int64_t offset = 0;
      if (leaf.max_rep_level_ > 0) {
        uint32_t length = 0;
        MEMCPY(&length, body + offset, 4);
        offset += 4 + static_cast<int64_t>(length);
      }
      if (OB_UNLIKELY(offset + 4 > body_len)) {
        ret = OB_INVALID_ARGUMENT;
      } else {
        uint32_t length = 0;
        MEMCPY(&length, body + offset, 4);
        block_start = offset + 4;
        block_length = static_cast<int64_t>(length);
      }
    }
    if (OB_SUCC(ret)) {
      if (OB_UNLIKELY(block_start + block_length > body_len || num_values > 0 && num_values > block_length * 8)) {
        ret = OB_INVALID_ARGUMENT;
      } else if (OB_ISNULL(def_levels = static_cast<int64_t *>(
                     alloc.alloc(sizeof(int64_t) * (num_values > 0 ? num_values : 1))))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else if (OB_FAIL(ret = ob_parquet_rle_decode(body + block_start, block_length, width,
                                                     num_values, def_levels))) {
        LOG_WARN("failed to decode the definition levels", K(ret), K(num_values));
      }
    }
  }
  if (OB_SUCC(ret)) {
    int64_t present = 0;
    for (int64_t i = 0; i < num_values; ++i) {
      if (OB_ISNULL(def_levels) || def_levels[i] == leaf.max_def_level_) {
        ++present;
      }
    }
    ObParquetStrVec present_values;
    if (OB_FAIL(ret = present_values.init(alloc, present > 0 ? present : 1))) {
      return ret;
    }
    if (OB_PARQUET_ENC_RLE_DICTIONARY == header.encoding_) {
      if (OB_UNLIKELY(values_length < 1)) {
        ret = OB_INVALID_ARGUMENT;
      } else {
        const int64_t width = static_cast<uint8_t>(values[0]);
        int64_t *indices = NULL;
        if (OB_ISNULL(indices = static_cast<int64_t *>(
                       alloc.alloc(sizeof(int64_t) * (present > 0 ? present : 1))))) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
        } else if (OB_FAIL(ret = ob_parquet_rle_decode(values + 1, values_length - 1, width,
                                                       present, indices))) {
          LOG_WARN("failed to decode the dictionary indices", K(ret), K(present));
        } else {
          for (int64_t i = 0; OB_SUCC(ret) && i < present; ++i) {
            if (OB_UNLIKELY(indices[i] < 0 || indices[i] >= dictionary.count())) {
              ret = OB_INVALID_ARGUMENT;
              LOG_WARN("the dictionary index is out of range", K(ret), K(indices[i]), K(dictionary.count()));
              break;
            }
            if (OB_FAIL(ret = present_values.push_back(dictionary.at(indices[i])))) {
              break;
            }
          }
        }
      }
    } else if (OB_PARQUET_ENC_PLAIN == header.encoding_) {
      g_ob_parquet_stage = OB_PARQUET_STAGE_PLAIN;
      ret = ob_parquet_decode_plain(values, values_length, leaf.type_, leaf.type_length_,
                                    present, alloc, present_values);
    } else {
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "file table, this parquet encoding is not supported");
    }
    if (OB_SUCC(ret)) {
      int64_t next = 0;
      for (int64_t i = 0; OB_SUCC(ret) && i < num_values; ++i) {
        ObString cell;
        if (OB_ISNULL(def_levels) || def_levels[i] == leaf.max_def_level_) {
          if (next < present_values.count()) {
            cell = present_values.at(next++);
          }
        }
        if (OB_FAIL(ret = out.push_back(cell))) {
          break;
        }
      }
    }
  }
  return ret;
}

// Reads one column chunk: the pages of the chunk, decoded in order.
int ob_parquet_read_column_chunk(int fd, int64_t file_size, ObIAllocator &alloc,
                                 const ObParquetColumnMeta &meta, const ObParquetLeaf &leaf,
                                 ObParquetStrVec &values)
{
  int ret = OB_SUCCESS;
  int64_t start = meta.data_page_offset_;
  if (meta.dictionary_page_offset_ > 0 && meta.dictionary_page_offset_ < start) {
    start = meta.dictionary_page_offset_;
  }
  int64_t chunk_length = meta.total_compressed_size_;
  if (chunk_length <= 0 || start < 0 || start + chunk_length > file_size) {
    // the chunk size is the distance to the next chunk; without it the rest of the file is the chunk
    if (chunk_length <= 0 || start < 0 || start > file_size) {
      ret = OB_INVALID_ARGUMENT;
      LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet column chunk is malformed");
      return ret;
    }
    chunk_length = file_size - start;
  }
  char *chunk = NULL;
  if (OB_ISNULL(chunk = static_cast<char *>(alloc.alloc(chunk_length)))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
    return ret;
  }
  // pread returns the number of bytes read, so it is compared with the wanted count
  if (OB_UNLIKELY(::pread(fd, chunk, chunk_length, start) != chunk_length)) {
    ret = OB_IO_ERROR;
    LOG_WARN("failed to read a parquet column chunk", K(ret), K(errno));
    return ret;
  }
  ObParquetStrVec dictionary;
  if (OB_FAIL(ret = dictionary.init(alloc, 16))) {
    return ret;
  }
  int64_t position = 0;
  while (OB_SUCC(ret) && position < chunk_length) {
    g_ob_parquet_stage = OB_PARQUET_STAGE_PAGE_HEADER;
    ObThriftCompact reader(chunk + position, chunk_length - position);
    ObParquetPageHeader header;
    if (OB_FAIL(ret = ob_parquet_parse_page_header(reader, header))) {
      LOG_WARN("failed to read a parquet page header", K(ret));
      break;
    }
    const int64_t body_start = position + reader.pos();
    if (OB_UNLIKELY(body_start + header.compressed_size_ > chunk_length)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("the parquet page runs past the column chunk", K(ret));
      break;
    }
    const char *body = chunk + body_start;
    const int64_t body_length = header.compressed_size_;
    if (OB_PARQUET_PAGE_INDEX == header.type_) {
      position = body_start + header.compressed_size_;
      continue;
    }
    if (OB_PARQUET_PAGE_DICTIONARY == header.type_) {
      char *plain = NULL;
      const int64_t plain_length = header.uncompressed_size_;
      if (meta.codec_ == OB_PARQUET_CODEC_SNAPPY) {
        if (OB_ISNULL(plain = static_cast<char *>(alloc.alloc(plain_length)))) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
          break;
        }
        if (OB_FAIL(ret = ob_parquet_snappy_uncompress(body, body_length, plain, plain_length))) {
          LOG_WARN("failed to decompress a parquet dictionary page", K(ret));
          break;
        }
      } else if (OB_PARQUET_CODEC_UNCOMPRESSED == meta.codec_) {
        plain = const_cast<char *>(body);
      } else {
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "file table, this parquet compression is not supported");
        break;
      }
      if (OB_SUCC(ret) && OB_FAIL(ret = ob_parquet_decode_plain_all(plain, plain_length, leaf.type_,
                                                                    leaf.type_length_, alloc, dictionary))) {
        LOG_WARN("failed to decode a parquet dictionary page", K(ret));
      }
      position = body_start + header.compressed_size_;
      continue;
    }
    if (OB_PARQUET_PAGE_DATA == header.type_ || OB_PARQUET_PAGE_DATA_V2 == header.type_) {
      char *plain = NULL;
      int64_t plain_length = header.uncompressed_size_;
      const char *plain_body = body;
      if (OB_PARQUET_CODEC_SNAPPY == meta.codec_) {
        // version 2 keeps the levels outside the compressed part, so only the values are compressed
        int64_t levels_length = 0;
        const char *compressed = body;
        int64_t compressed_length = body_length;
        if (header.is_v2_) {
          levels_length = header.rep_levels_length_ + header.def_levels_length_;
          compressed = body + levels_length;
          compressed_length = body_length - levels_length;
          plain_length = header.uncompressed_size_ - levels_length;
        }
        if (OB_UNLIKELY(plain_length < 0)) {
          ret = OB_INVALID_ARGUMENT;
          break;
        }
        if (OB_ISNULL(plain = static_cast<char *>(alloc.alloc(plain_length > 0 ? plain_length : 1)))) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
          break;
        }
        if (OB_FAIL(ret = ob_parquet_snappy_uncompress(compressed, compressed_length, plain, plain_length))) {
          g_ob_parquet_stage = OB_PARQUET_STAGE_SNAPPY;
          LOG_WARN("failed to decompress a parquet data page", K(ret));
          break;
        }
        if (header.is_v2_) {
          // the levels sit in front of the decompressed values
          const int64_t total = levels_length + plain_length;
          char *joined = static_cast<char *>(alloc.alloc(total > 0 ? total : 1));
          if (OB_ISNULL(joined)) {
            ret = OB_ALLOCATE_MEMORY_FAILED;
            break;
          }
          MEMCPY(joined, body, levels_length);
          MEMCPY(joined + levels_length, plain, plain_length);
          plain_body = joined;
          plain_length = total;
        }
      } else if (OB_PARQUET_CODEC_UNCOMPRESSED == meta.codec_) {
        plain_body = body;
        plain_length = body_length;
      } else {
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "file table, this parquet compression is not supported");
        break;
      }
      if (OB_SUCC(ret) && OB_FAIL(ret = ob_parquet_decode_page_values(plain_body, plain_length, header,
                                                                      leaf, alloc, dictionary, values))) {
        LOG_WARN("failed to decode a parquet data page", K(ret));
      }      position = body_start + header.compressed_size_;
      continue;
    }
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "file table, this parquet page is not supported");
    break;
  }
  return ret;
}

} // namespace


int ob_file_sql_load_parquet(ObIAllocator &alloc, const ObFileSqlScanParam &param,
                             ObFileSqlTableData &data)
{
  int ret = OB_SUCCESS;
  int fd = -1;
  char *path_buf = NULL;
  ObSEArray<ObParquetSchemaElement, 16> schema_elements;
  ObSEArray<ObParquetLeaf, 16> leaves;
  ObSEArray<ObSEArray<ObParquetColumnMeta, 8>, 4> row_groups;
  ObSEArray<ObParquetStrVec, 16> column_values;
  int64_t file_size = 0;
  int64_t num_rows = 0;

  if (param.path_.empty()) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet path is empty");
  } else if (OB_ISNULL(path_buf = static_cast<char *>(alloc.alloc(param.path_.length() + 1)))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    MEMCPY(path_buf, param.path_.ptr(), param.path_.length());
    path_buf[param.path_.length()] = '\0';
  }
  struct stat file_stat;
  if (OB_SUCC(ret) && OB_UNLIKELY(0 != ::stat(path_buf, &file_stat))) {
    ret = OB_FILE_NOT_EXIST;
    LOG_USER_ERROR(OB_FILE_NOT_EXIST);
  } else if (OB_SUCC(ret) && OB_UNLIKELY(!S_ISREG(file_stat.st_mode))) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet file is not a regular file");
  } else if (OB_SUCC(ret) && OB_UNLIKELY(file_stat.st_size < 12)) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet file is too short");
  } else if (OB_SUCC(ret)) {
    file_size = file_stat.st_size;
  }
  if (OB_SUCC(ret) && OB_UNLIKELY((fd = ::open(path_buf, O_RDONLY)) < 0)) {
    ret = OB_IO_ERROR;
    LOG_WARN("failed to open the parquet file", K(ret), K(errno));
  }

  // the footer: a four byte length, the thrift encoded metadata, and the magic bytes
  char *footer = NULL;
  int64_t footer_length = 0;
  g_ob_parquet_stage = OB_PARQUET_STAGE_FOOTER;
  if (OB_SUCC(ret)) {
    uint32_t stored = 0;
    if (OB_UNLIKELY(::pread(fd, &stored, 4, file_size - 8) != 4)) {
      ret = OB_IO_ERROR;
      LOG_WARN("failed to read the parquet footer length", K(ret), K(errno));
    } else if (OB_UNLIKELY(stored + 8 > file_size)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet footer is malformed");
    } else {
      footer_length = static_cast<int64_t>(stored);
      if (OB_ISNULL(footer = static_cast<char *>(alloc.alloc(footer_length)))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else if (OB_UNLIKELY(::pread(fd, footer, footer_length, file_size - 8 - footer_length)
                            != footer_length)) {
        ret = OB_IO_ERROR;
        LOG_WARN("failed to read the parquet footer", K(ret), K(errno));
      }
    }
  }

  // the metadata: the schema, the row count and the row groups
  ObSEArray<ObSEArray<ObParquetColumnMeta, 8>, 4> parsed_groups;
  g_ob_parquet_stage = OB_PARQUET_STAGE_FOOTER_PARSE;
  if (OB_SUCC(ret)) {
    ObThriftCompact reader(footer, footer_length);
    int16_t id = 0;
    int16_t last_id = 0;
    int8_t type = 0;
    bool end = false;
    while (OB_SUCC(ret) && !end) {
      if (OB_FAIL(ret = reader.read_field(id, type, last_id, end))) {
        break;
      }
      if (end) {
        break;
      }
      switch (id) {
      case 2: { // schema
        int64_t size = 0;
        int8_t elem_type = 0;
        if (OB_SUCC(ret = reader.read_list_header(size, elem_type))) {
          for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
            ObParquetSchemaElement element;
            if (OB_FAIL(ret = ob_parquet_parse_schema_element(reader, element))) {
              break;
            }
            if (OB_FAIL(ret = schema_elements.push_back(element))) {
              break;
            }
          }
        }
        break;
      }
      case 3: { // num_rows
        int64_t value = 0;
        ret = reader.read_zigzag(value);
        num_rows = value;
        break;
      }
      case 4: { // row_groups
        int64_t size = 0;
        int8_t elem_type = 0;
        if (OB_SUCC(ret = reader.read_list_header(size, elem_type))) {
          for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
            ObSEArray<ObParquetColumnMeta, 8> columns;
            ObParquetStrVec unused_meta;
            ObParquetStrVec unused_paths;
            if (OB_FAIL(ret = ob_parquet_parse_row_group(reader, unused_meta, unused_paths, columns))) {
              break;
            }
            if (OB_FAIL(ret = parsed_groups.push_back(columns))) {
              break;
            }
          }
        }
        break;
      }
      default:
        ret = reader.skip_value(type);
        break;
      }
    }
  }

  // the leaf columns of the schema, in the order the values are stored
  if (OB_SUCC(ret) && OB_UNLIKELY(schema_elements.empty())) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet file has no schema");
  }
  if (OB_SUCC(ret)) {
    // the schema elements are stored depth first, so the tree is walked with a stack of the
    // children each level still owes
    struct ObSchemaLevel
    {
      int32_t remaining_;
      int32_t def_level_;
      int32_t rep_level_;
    };
    ObSEArray<ObSchemaLevel, 16> stack;
    ObSchemaLevel root;
    root.remaining_ = schema_elements.at(0).num_children_;
    // the root element's own repetition does not add a level: only the leaves below it do
    root.def_level_ = 0;
    root.rep_level_ = 0;
    if (OB_FAIL(ret = stack.push_back(root))) {
      LOG_WARN("failed to start the schema walk", K(ret));
    }
    int64_t index = 1;
    while (OB_SUCC(ret) && !stack.empty()) {
      ObSchemaLevel level = stack.at(stack.count() - 1);
      if (level.remaining_ <= 0) {
        stack.pop_back();
        continue;
      }
      level.remaining_--;
      stack.at(stack.count() - 1) = level;
      if (OB_UNLIKELY(index >= schema_elements.count())) {
        ret = OB_INVALID_ARGUMENT;
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet schema is malformed");
        break;
      }
      const ObParquetSchemaElement &element = schema_elements.at(index++);
      const int32_t def_level = level.def_level_
                                + (OB_PARQUET_REPETITION_OPTIONAL == element.repetition_type_ ? 1 : 0);
      const int32_t rep_level = level.rep_level_
                                + (OB_PARQUET_REPETITION_REPEATED == element.repetition_type_ ? 1 : 0);
      if (element.num_children_ > 0) {
        ObSchemaLevel child;
        child.remaining_ = element.num_children_;
        child.def_level_ = def_level;
        child.rep_level_ = rep_level;
        if (OB_FAIL(ret = stack.push_back(child))) {
          break;
        }
      } else {
        ObParquetLeaf leaf;
        leaf.name_ = element.name_;
        leaf.type_ = element.type_;
        leaf.type_length_ = element.type_length_;
        leaf.max_def_level_ = def_level;
        leaf.max_rep_level_ = rep_level;
        leaf.nullable_ = def_level > 0;
        if (OB_FAIL(ret = leaves.push_back(leaf))) {
          break;
        }
      }
    }
  }

  // the columns the user sees
  if (OB_SUCC(ret) && OB_UNLIKELY(leaves.empty())) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet file has no columns");
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < leaves.count(); ++i) {
    const ObParquetLeaf &leaf = leaves.at(i);
    ObFileSqlColumn column;
    column.name_ = leaf.name_;
    switch (leaf.type_) {
    case OB_PARQUET_TYPE_BOOLEAN:
      column.type_ = ObTinyIntType;
      break;
    case OB_PARQUET_TYPE_INT32:
      column.type_ = ObIntType;
      break;
    case OB_PARQUET_TYPE_INT64:
      column.type_ = ObIntType;
      break;
    case OB_PARQUET_TYPE_FLOAT:
    case OB_PARQUET_TYPE_DOUBLE:
      column.type_ = ObDoubleType;
      break;
    default:
      column.type_ = ObVarcharType;
      break;
    }
    column.nullable_ = leaf.nullable_;
    column.max_len_ = (ObVarcharType == column.type_) ? 4096 : 0;
    if (OB_FAIL(ret = data.cols_.push_back(column))) {
      LOG_WARN("failed to collect a parquet column", K(ret), K(i));
    }
  }
  data.col_cnt_ = data.cols_.count();

  // the values of every column, row group by row group
  if (OB_SUCC(ret)) {
    for (int64_t c = 0; OB_SUCC(ret) && c < leaves.count(); ++c) {
      ObParquetStrVec values;
      if (OB_FAIL(ret = values.init(alloc, num_rows > 0 ? num_rows : 1))) {
        break;
      }
      for (int64_t g = 0; OB_SUCC(ret) && g < parsed_groups.count(); ++g) {
        const ObSEArray<ObParquetColumnMeta, 8> &chunks = parsed_groups.at(g);
        const ObParquetColumnMeta *meta = NULL;
        for (int64_t i = 0; i < chunks.count(); ++i) {
          if (chunks.at(i).path_ == leaves.at(c).name_) {
            meta = &chunks.at(i);
            break;
          }
        }
        if (OB_ISNULL(meta)) {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the parquet row group misses a column");
          break;
        }
        if (OB_FAIL(ret = ob_parquet_read_column_chunk(fd, file_size, alloc, *meta, leaves.at(c), values))) {
          break;
        }
      }
      if (OB_SUCC(ret) && OB_FAIL(ret = column_values.push_back(values))) {
        break;
      }
    }
  }

  // the cells, row major
  if (OB_SUCC(ret)) {
    for (int64_t r = 0; OB_SUCC(ret) && r < num_rows; ++r) {
      for (int64_t c = 0; OB_SUCC(ret) && c < data.col_cnt_; ++c) {
        ObString cell;
        if (r < column_values.at(c).count()) {
          cell = column_values.at(c).at(r);
        }
        if (OB_FAIL(ret = data.cells_.push_back(cell))) {
          LOG_WARN("failed to collect a parquet cell", K(ret), K(r), K(c));
        }
      }
      if (OB_SUCC(ret)) {
        ++data.row_cnt_;
      }
    }
  }
  if (OB_SUCC(ret) && OB_UNLIKELY(0 == data.row_cnt_)) {
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "a file table with no rows");
  }
  if (fd >= 0) {
    ::close(fd);
  }
  if (OB_FAIL(ret)) {
    if (OB_INVALID_ARGUMENT == ret) {
      ret = ob_parquet_stage_error();
    }
  }
  return ret;
}

} // namespace sql
} // namespace oceanbase
