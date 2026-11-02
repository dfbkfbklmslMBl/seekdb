/*
 * Copyright (c) 2025 OceanBase.
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

#define USING_LOG_PREFIX SQL_ENG
#include "ob_expr_ai_embed.h"
#include "query/ai/ob_ai_endpoint_resolver.h"
#include "share/rc/ob_server_runtime.h"

using namespace oceanbase::common;
using namespace oceanbase::sql;

namespace oceanbase 
{
namespace sql 
{
// Inputs a single embedding request may carry. Bounded so the request body and the temporary
// vectors of one chunk stay small whatever the runtime batch size is.
static const int64_t MAX_EMBED_BATCH_ROWS = 64;

ObExprAIEmbed::ObExprAIEmbed(common::ObIAllocator &alloc)
    : ObFuncExprOperator(alloc, 
                        T_FUN_SYS_AI_EMBED, 
                        N_AI_EMBED, 
                        MORE_THAN_ZERO,
                        NOT_VALID_FOR_GENERATED_COL, 
                        NOT_ROW_DIMENSION) 
{
}

ObExprAIEmbed::~ObExprAIEmbed() 
{
}

int ObExprAIEmbed::calc_result_typeN(ObExprResType &type,
                                     ObExprResType *types_stack,
                                     int64_t param_num,
                                     common::ObExprTypeCtx &type_ctx) const 
{
  UNUSED(type_ctx);
  UNUSED(types_stack);
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(param_num > 3 || param_num < 2)) {
    ObString func_name_(get_name());
    ret = OB_ERR_PARAM_SIZE;
    LOG_USER_ERROR(OB_ERR_PARAM_SIZE, func_name_.length(), func_name_.ptr());
  } else {
    types_stack[MODEL_IDX].set_calc_type(ObVarcharType);
    types_stack[MODEL_IDX].set_calc_collation_type(CS_TYPE_UTF8MB4_BIN);
    types_stack[CONTENT_IDX].set_calc_type(ObVarcharType);
    types_stack[CONTENT_IDX].set_calc_collation_type(CS_TYPE_UTF8MB4_BIN);
    if (param_num == 3) {
      if (ob_is_integer_type(types_stack[DIM_IDX].get_type())) {
        types_stack[DIM_IDX].set_calc_type(ObIntType);
        types_stack[DIM_IDX].set_precision(10);
        types_stack[DIM_IDX].set_scale(0);
      } else {
        ret = OB_INVALID_ARGUMENT;
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, dimension parameter must be an integer, not a decimal or float");
      }
    }
    type.set_varchar();
    type.set_collation_type(CS_TYPE_UTF8MB4_BIN);
    type.set_collation_level(CS_LEVEL_COERCIBLE);
  }
  return ret;
}

int ObExprAIEmbed::eval_ai_embed(const ObExpr &expr, ObEvalCtx &ctx, ObDatum &res) 
{
  INIT_SUCC(ret);
  ObDatum *arg_model_id = nullptr;
  ObDatum *arg_content = nullptr;
  ObDatum *arg_dim = nullptr;
  if (expr.arg_cnt_ == 3 ? OB_FAIL(expr.eval_param_value(ctx, arg_model_id, arg_content, arg_dim))
                         : OB_FAIL(expr.eval_param_value(ctx, arg_model_id, arg_content))) {
  } else if (OB_ISNULL(arg_model_id) || OB_ISNULL(arg_content)) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    ObEvalCtx::TempAllocGuard tmp_alloc_g(ctx);
    MultimodeAlloctor temp_allocator(tmp_alloc_g.get_allocator());
    ret = eval_one_row(expr, ctx, res, *arg_model_id, *arg_content, arg_dim, temp_allocator);
  }
  return ret;
}

int ObExprAIEmbed::eval_one_row(const ObExpr &expr, ObEvalCtx &ctx, ObDatum &res,
                                ObDatum &model_id_datum, ObDatum &content_datum,
                                ObDatum *dim_datum, MultimodeAlloctor &temp_allocator)
{
  INIT_SUCC(ret);
  lib::ObMallocHookAttrGuard malloc_guard(lib::ObMemAttr(N_AI_EMBED));
  ObAIFuncExprInfo *info = nullptr;
  share::ObAiModelEndpointInfo resolved_endpoint;
  const share::ObAiModelEndpointInfo *endpoint_info = &resolved_endpoint;
  query::ObIAiEndpointResolver *endpoint_resolver =
      ::oceanbase::share::server_service<::oceanbase::query::ObIAiEndpointResolver>();
  if (model_id_datum.is_null()) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, model id is null");
    res.set_null();
  } else if (content_datum.is_null()) {
    // NULL content means NULL result: no request is built, no row is shifted
    res.set_null();
  } else {
    ObString model_id = model_id_datum.get_string();
    ObString content = content_datum.get_string();
    if (model_id.empty() || content.empty()) {
      ret = OB_INVALID_ARGUMENT;
      LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, model id or input is empty");
      res.set_null();
    }
    int64_t dim = 0;
    ObJsonInt *dim_json = nullptr;
    ObJsonObject *config = nullptr;
    if (OB_FAIL(ret)) {
    } else if (OB_NOT_NULL(dim_datum) && !dim_datum->is_null()) {
      dim = dim_datum->get_int();
      if (dim <= 0) {
        ret = OB_INVALID_ARGUMENT;
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, dimension parameter must be a positive integer");
        res.set_null();
      } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_object(temp_allocator, config))) {
      } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_int(temp_allocator, dim, dim_json))) {
      } else if (OB_FAIL(config->add("dimensions", dim_json))) {
      }
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(temp_allocator, model_id, info))) {
    } else if (OB_ISNULL(endpoint_resolver)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(endpoint_resolver->resolve_by_model_name(
                   model_id, temp_allocator, resolved_endpoint))) {
    } else {
      ObAIFuncModel model(temp_allocator, *info, *endpoint_info);
      ObString result;
      if (OB_FAIL(model.call_dense_embedding(content, config, result))) {
      } else if (OB_FAIL(ObAIFuncUtils::set_string_result(expr, ctx, res, result))) {
      }
    }
  }
  return ret;
}

int64_t ObExprAIEmbed::row_dim(const ObDatum *dim_datum)
{
  int64_t dim = 0;
  if (OB_NOT_NULL(dim_datum) && !dim_datum->is_null()) {
    dim = dim_datum->get_int();
  }
  return dim;
}

bool ObExprAIEmbed::is_batchable_row(const ObDatum &model_id_datum,
                                     const ObDatum &content_datum,
                                     const ObDatum *dim_datum)
{
  bool batchable = false;
  const int64_t dim = row_dim(dim_datum);
  if (!model_id_datum.is_null() && !content_datum.is_null()) {
    const ObString model_id = model_id_datum.get_string();
    const ObString content = content_datum.get_string();
    batchable = !model_id.empty() && !content.empty() && dim >= 0;
  }
  return batchable;
}

int ObExprAIEmbed::request_batch(const ObString &model_id, ObArray<ObString> &contents,
                                 const int64_t dim, MultimodeAlloctor &temp_allocator,
                                 ObArray<ObString> &vectors)
{
  INIT_SUCC(ret);
  ObAIFuncExprInfo *info = nullptr;
  share::ObAiModelEndpointInfo resolved_endpoint;
  query::ObIAiEndpointResolver *endpoint_resolver =
      ::oceanbase::share::server_service<::oceanbase::query::ObIAiEndpointResolver>();
  ObJsonObject *config = nullptr;
  ObJsonInt *dim_json = nullptr;
  if (OB_ISNULL(endpoint_resolver)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (dim > 0 && OB_FAIL(ObAIFuncJsonUtils::get_json_object(temp_allocator, config))) {
    LOG_WARN("failed to build the config object for an embedding batch", K(ret), K(dim));
  } else if (dim > 0 && OB_FAIL(ObAIFuncJsonUtils::get_json_int(temp_allocator, dim, dim_json))) {
    LOG_WARN("failed to build the dimensions entry for an embedding batch", K(ret), K(dim));
  } else if (dim > 0 && OB_FAIL(config->add("dimensions", dim_json))) {
    LOG_WARN("failed to add dimensions to an embedding batch config", K(ret), K(dim));
  } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(temp_allocator, model_id, info))) {
  } else if (OB_ISNULL(info)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(endpoint_resolver->resolve_by_model_name(model_id, temp_allocator,
                                                              resolved_endpoint))) {
  } else {
    ObAIFuncModel model(temp_allocator, *info, resolved_endpoint);
    if (OB_FAIL(model.call_dense_embedding_vector_v2(contents, config, vectors))) {
      LOG_WARN("failed to call dense embedding with a batch of inputs", K(ret),
               "input_cnt", contents.count(), K(model_id));
    }
  }
  return ret;
}

int ObExprAIEmbed::eval_ai_embed_batch(const ObExpr &expr, ObEvalCtx &ctx,
                                       const ObBitVector &skip, const int64_t batch_size)
{
  INIT_SUCC(ret);
  const ObExpr *model_expr = nullptr;
  const ObExpr *content_expr = nullptr;
  const ObExpr *dim_expr = nullptr;
  ObDatum *results = nullptr;
  ObBitVector *evaluated = nullptr;
  if (OB_ISNULL(expr.args_) || batch_size <= 0) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_ISNULL(model_expr = expr.args_[MODEL_IDX])
             || OB_ISNULL(content_expr = expr.args_[CONTENT_IDX])) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(model_expr->eval_batch(ctx, skip, batch_size))) {
    LOG_WARN("failed to evaluate model id argument in batch", K(ret));
  } else if (OB_FAIL(content_expr->eval_batch(ctx, skip, batch_size))) {
    LOG_WARN("failed to evaluate content argument in batch", K(ret));
  } else if (expr.arg_cnt_ > DIM_IDX && OB_NOT_NULL(expr.args_[DIM_IDX])
             && OB_FAIL(expr.args_[DIM_IDX]->eval_batch(ctx, skip, batch_size))) {
    LOG_WARN("failed to evaluate dimension argument in batch", K(ret));
  } else if (OB_ISNULL(results = expr.locate_batch_datums(ctx))) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    if (expr.arg_cnt_ > DIM_IDX) {
      dim_expr = expr.args_[DIM_IDX];
    }
    evaluated = &expr.get_evaluated_flags(ctx);
    ObEvalCtx::BatchInfoScopeGuard batch_info_guard(ctx);
    batch_info_guard.set_batch_size(batch_size);
    int64_t row = 0;
    while (OB_SUCC(ret) && row < batch_size) {
      if (skip.at(row) || evaluated->at(row)) {
        ++row;
      } else {
        ObDatum &model_datum = model_expr->locate_expr_datum(ctx, row);
        ObDatum &content_datum = content_expr->locate_expr_datum(ctx, row);
        ObDatum *dim_datum = OB_ISNULL(dim_expr) ? nullptr
                                                 : &dim_expr->locate_expr_datum(ctx, row);
        batch_info_guard.set_batch_idx(row);
        if (!is_batchable_row(model_datum, content_datum, dim_datum)) {
          // NULL and invalid inputs keep going through the single row path, which reports exactly
          // the same value or the same user error as it did before batching existed
          ObEvalCtx::TempAllocGuard one_alloc_g(ctx);
          MultimodeAlloctor one_allocator(one_alloc_g.get_allocator());
          if (OB_FAIL(eval_one_row(expr, ctx, results[row], model_datum, content_datum,
                                   dim_datum, one_allocator))) {
            LOG_WARN("failed to evaluate ai_embed for one row", K(ret), K(row));
          } else {
            evaluated->set(row);
            ++row;
          }
        } else {
          const ObString model_id = model_datum.get_string();
          const int64_t dim = row_dim(dim_datum);
          ObSEArray<int64_t, 64> chunk_rows;
          ObArray<ObString> contents;
          int64_t next = row;
          int64_t resume = -1;
          // a chunk only holds rows that share the model and the requested dimension, so one
          // request can never mix rows that would have been sent to different endpoints
          while (OB_SUCC(ret) && next < batch_size && contents.count() < MAX_EMBED_BATCH_ROWS) {
            if (skip.at(next) || evaluated->at(next)) {
              ++next;
            } else {
              ObDatum &next_model = model_expr->locate_expr_datum(ctx, next);
              ObDatum &next_content = content_expr->locate_expr_datum(ctx, next);
              ObDatum *next_dim = OB_ISNULL(dim_expr) ? nullptr
                                                      : &dim_expr->locate_expr_datum(ctx, next);
              if (!is_batchable_row(next_model, next_content, next_dim)) {
                // a NULL or invalid row never belongs in a request, but it must not split the
                // valid rows around it into separate requests either: note where the outer loop
                // has to pick it up and keep filling this chunk past it
                if (resume < 0) {
                  resume = next;
                }
                ++next;
              } else if (next_model.get_string() != model_id || row_dim(next_dim) != dim) {
                break;
              } else if (OB_FAIL(chunk_rows.push_back(next))) {
                LOG_WARN("failed to push chunk row", K(ret), K(next));
              } else if (OB_FAIL(contents.push_back(next_content.get_string()))) {
                LOG_WARN("failed to push chunk content", K(ret), K(next));
              } else {
                ++next;
              }
            }
          }
          if (OB_SUCC(ret) && !contents.empty()) {
            ObEvalCtx::TempAllocGuard chunk_alloc_g(ctx);
            MultimodeAlloctor chunk_allocator(chunk_alloc_g.get_allocator());
            ObArray<ObString> vectors;
            bool answered = false;
            if (OB_FAIL(request_batch(model_id, contents, dim, chunk_allocator, vectors))) {
              // an endpoint or a service that cannot serve a multi input request must not lose the
              // rows, so the failure is not fatal here; every row is retried on its own below
              ret = OB_SUCCESS;
            } else if (vectors.count() != contents.count()) {
              // a truncated or over-long answer cannot be mapped back to rows
              LOG_WARN("embedding batch answer does not line up with its inputs",
                       "input_cnt", contents.count(), "answer_cnt", vectors.count());
            } else {
              answered = true;
            }
            if (answered) {
              for (int64_t i = 0; OB_SUCC(ret) && i < chunk_rows.count(); ++i) {
                const int64_t chunk_row = chunk_rows.at(i);
                ObString vector = vectors.at(i);
                batch_info_guard.set_batch_idx(chunk_row);
                if (OB_FAIL(ObAIFuncUtils::set_string_result(expr, ctx, results[chunk_row],
                                                              vector))) {
                  LOG_WARN("failed to store embedded vector", K(ret), K(chunk_row));
                } else {
                  evaluated->set(chunk_row);
                }
              }
            } else {
              for (int64_t i = 0; OB_SUCC(ret) && i < chunk_rows.count(); ++i) {
                const int64_t chunk_row = chunk_rows.at(i);
                ObDatum &one_model = model_expr->locate_expr_datum(ctx, chunk_row);
                ObDatum &one_content = content_expr->locate_expr_datum(ctx, chunk_row);
                ObDatum *one_dim = OB_ISNULL(dim_expr) ? nullptr
                                                       : &dim_expr->locate_expr_datum(ctx, chunk_row);
                batch_info_guard.set_batch_idx(chunk_row);
                if (OB_FAIL(eval_one_row(expr, ctx, results[chunk_row], one_model, one_content,
                                         one_dim, chunk_allocator))) {
                  LOG_WARN("failed to evaluate ai_embed for one row after a batch failure",
                           K(ret), K(chunk_row));
                } else {
                  evaluated->set(chunk_row);
                }
              }
            }
          }
          row = (resume >= 0) ? resume : next;
        }
      }
    }
  }
  return ret;
}

int ObExprAIEmbed::cg_expr(ObExprCGCtx &expr_cg_ctx, 
                           const ObRawExpr &raw_expr,
                           ObExpr &rt_expr) const 
{
  int ret = OB_SUCCESS;
  // TODO: support schema version match in plan cache for ai func
  // const ObRawExpr *model_key = raw_expr.get_param_expr(0);
  // if (OB_NOT_NULL(model_key)
  //     && (model_key->is_static_scalar_const_expr() || model_key->is_const_expr())
  //     && model_key->get_expr_type() != T_OP_GET_USER_VAR &&
  //     OB_NOT_NULL(expr_cg_ctx.schema_guard_)) {
  //   ObIAllocator *allocator = expr_cg_ctx.allocator_;
  //   ObExecContext *exec_ctx = expr_cg_ctx.session_->get_cur_exec_ctx();
  //   ObObj const_data;
  //   bool got_data = false;
  //   ObAIFuncExprInfo *info = nullptr;
  //   if (OB_ISNULL(allocator)) {
  //     ret = OB_ERR_UNEXPECTED;
  //     LOG_WARN("allocator is null", K(ret));
  //   } else if (OB_FAIL(ObSQLUtils::calc_const_or_calculable_expr(exec_ctx,
  //                                                         model_key,
  //                                                         const_data,
  //                                                         got_data,
  //                                                         *allocator))) {
  //     LOG_WARN("failed to calc offset expr", K(ret));
  //   } else if (!got_data || const_data.is_null()) {
  //   } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(*allocator, const_data.get_string(), *expr_cg_ctx.schema_guard_, info))) {
  //     LOG_WARN("failed to get ai func info", K(ret), K(const_data.get_string()));
  //   } else {
  //     rt_expr.extra_info_ = info;
  //   }
  // }

  if (OB_SUCC(ret)) {
    rt_expr.eval_func_ = ObExprAIEmbed::eval_ai_embed;
    // the content drives the request, so a row-varying content is what makes batching worth doing
    if (OB_NOT_NULL(rt_expr.args_) && rt_expr.arg_cnt_ > CONTENT_IDX
        && OB_NOT_NULL(rt_expr.args_[CONTENT_IDX])
        && rt_expr.args_[CONTENT_IDX]->is_batch_result()) {
      rt_expr.eval_batch_func_ = ObExprAIEmbed::eval_ai_embed_batch;
    }
  }
  return ret;
}

} // namespace sql
} // namespace oceanbase
