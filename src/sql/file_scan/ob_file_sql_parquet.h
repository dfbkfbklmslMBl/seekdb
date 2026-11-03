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

#ifndef OCEANBASE_SQL_FILE_SQL_OB_FILE_SQL_PARQUET_H_
#define OCEANBASE_SQL_FILE_SQL_OB_FILE_SQL_PARQUET_H_

#include "lib/container/ob_se_array.h"
#include "lib/string/ob_string.h"
#include "lib/utility/ob_print_utils.h"
#include "sql/file_scan/ob_file_scan_utils.h"

namespace oceanbase
{
namespace sql
{

// The physical types of the parquet format (parquet.thrift, Type).
enum ObParquetPhysicalType
{
  OB_PARQUET_TYPE_BOOLEAN = 0,
  OB_PARQUET_TYPE_INT32 = 1,
  OB_PARQUET_TYPE_INT64 = 2,
  OB_PARQUET_TYPE_INT96 = 3,
  OB_PARQUET_TYPE_FLOAT = 4,
  OB_PARQUET_TYPE_DOUBLE = 5,
  OB_PARQUET_TYPE_BYTE_ARRAY = 6,
  OB_PARQUET_TYPE_FIXED_LEN_BYTE_ARRAY = 7,
};

// The encodings a page's values can use (parquet.thrift, Encoding).
enum ObParquetEncoding
{
  OB_PARQUET_ENC_PLAIN = 0,
  OB_PARQUET_ENC_PLAIN_DICTIONARY = 2,
  OB_PARQUET_ENC_RLE = 3,
  OB_PARQUET_ENC_BIT_PACKED = 4,
  OB_PARQUET_ENC_DELTA_BINARY_PACKED = 5,
  OB_PARQUET_ENC_DELTA_LENGTH_BYTE_ARRAY = 6,
  OB_PARQUET_ENC_RLE_DICTIONARY = 7,
};

// The compression codecs a column chunk can use (parquet.thrift, CompressionCodec).
enum ObParquetCodec
{
  OB_PARQUET_CODEC_UNCOMPRESSED = 0,
  OB_PARQUET_CODEC_SNAPPY = 1,
  OB_PARQUET_CODEC_GZIP = 2,
  OB_PARQUET_CODEC_LZO = 3,
  OB_PARQUET_CODEC_BROTLI = 4,
  OB_PARQUET_CODEC_LZ4 = 5,
  OB_PARQUET_CODEC_ZSTD = 6,
  OB_PARQUET_CODEC_LZ4_RAW = 7,
};

// The page kinds (parquet.thrift, PageType).
enum ObParquetPageType
{
  OB_PARQUET_PAGE_DATA = 0,
  OB_PARQUET_PAGE_INDEX = 1,
  OB_PARQUET_PAGE_DICTIONARY = 2,
  OB_PARQUET_PAGE_DATA_V2 = 3,
};

// How a schema element repeats (parquet.thrift, FieldRepetitionType).
enum ObParquetRepetitionType
{
  OB_PARQUET_REPETITION_REQUIRED = 0,
  OB_PARQUET_REPETITION_OPTIONAL = 1,
  OB_PARQUET_REPETITION_REPEATED = 2,
};

// Reads a parquet file as a file table: the footer gives the schema, every row group gives the
// values. The reader keeps one column chunk in memory at a time, so the peak memory is one chunk
// rather than the whole file.
int ob_file_sql_load_parquet(common::ObIAllocator &alloc, const ObFileSqlScanParam &param,
                             ObFileSqlTableData &data);

} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_FILE_SQL_OB_FILE_SQL_PARQUET_H_
