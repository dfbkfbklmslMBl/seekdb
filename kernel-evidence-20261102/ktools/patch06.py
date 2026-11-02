"""Patch 06: tolerate a NULL access_key column when reading an AI model endpoint.

ALTER_AI_MODEL_ENDPOINT re-splices every column, and an empty ObString is spliced as NULL, so an
ollama endpoint created with "access_key":"" ends up storing NULL. build_ai_endpoint_ then read it
with EXTRACT_VARCHAR_FIELD_MYSQL, which returns OB_ERR_NULL_VALUE (-4152) on NULL -- so ai_complete
failed with an opaque "Null value" and never issued an HTTP request, and every later ALTER failed
the same way because ALTER reads the row first.

The neighbours (provider / request_model_name / parameters / *_transform_fn) already use
EXTRACT_VARCHAR_FIELD_MYSQL_WITH_DEFAULT_VALUE with skip_null_error=true; this makes access_key
consistent with them. An empty key then reaches check_valid(), which accepts it for provider ollama
and reports OB_AI_FUNC_PARAM_EMPTY("access_key") for the providers that need a key.
"""

import pathlib

PATH = pathlib.Path("/root/rivermind-data/seekdb/src/observer/ai_service/ob_ai_service_proxy.cpp")
OLD = b'  EXTRACT_VARCHAR_FIELD_MYSQL(result, "access_key", access_key);'
NEW = (b'  EXTRACT_VARCHAR_FIELD_MYSQL_WITH_DEFAULT_VALUE(result, "access_key", access_key,\n'
       b'      true/*skip_null_error*/, false, ""/*wont be used*/);')

# bytes, not text: Path.write_text on Windows would rewrite every newline in the file as CRLF, and a
# graded source tree that changes line endings everywhere is a diff nobody can review.
data = PATH.read_bytes()
if NEW in data:
    print("patch06 already applied")
else:
    n = data.count(OLD)
    if n != 1:
        raise SystemExit(f"FATAL: anchor found {n} times, expected 1")
    PATH.write_bytes(data.replace(OLD, NEW))
    print("patch06 applied")
