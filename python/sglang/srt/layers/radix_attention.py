"""Radix attention."""

import torch
from flashinfer.cascade import merge_state
from torch import nn

from sglang.global_config import global_config
from sglang.srt.layers.extend_attention import extend_attention_fwd
from sglang.srt.layers.token_attention import token_attention_fwd
from sglang.srt.managers.controller.infer_batch import global_server_args_dict
from sglang.srt.managers.controller.model_runner import ForwardMode, InputMetadata


class RadixAttention(nn.Module):
    def __init__(
        self,
        num_heads: int,
        head_dim: int,
        scaling: float,
        num_kv_heads: int,
        layer_id: int,
        logit_cap: int = -1,
    ):
        super().__init__()
        self.tp_q_head_num = num_heads
        self.tp_k_head_num = num_kv_heads
        self.tp_v_head_num = num_kv_heads
        self.head_dim = head_dim
        self.scaling = scaling
        self.layer_id = layer_id

        if not global_server_args_dict.get("disable_flashinfer", False):
            self.extend_forward = self.extend_forward_flashinfer
            self.decode_forward = self.decode_forward_flashinfer
        else:
            self.extend_forward = self.extend_forward_triton
            self.decode_forward = self.decode_forward_triton

        self.logit_cap = logit_cap if logit_cap is not None and logit_cap > 0 else 0

    def extend_forward_triton(self, q, k, v, input_metadata: InputMetadata):
        o = torch.empty_like(q)
        self.store_kv_cache(k, v, input_metadata)
        extend_attention_fwd(
            q.view(-1, self.tp_q_head_num, self.head_dim),
            k.contiguous(),
            v.contiguous(),
            o.view(-1, self.tp_q_head_num, self.head_dim),
            input_metadata.token_to_kv_pool.get_key_buffer(self.layer_id),
            input_metadata.token_to_kv_pool.get_value_buffer(self.layer_id),
            input_metadata.req_to_token_pool.req_to_token,
            input_metadata.req_pool_indices,
            input_metadata.triton_start_loc,
            input_metadata.seq_lens,
            input_metadata.triton_prefix_lens,
            input_metadata.extend_start_loc,
            input_metadata.extend_seq_lens,
            input_metadata.triton_max_seq_len,
            input_metadata.triton_max_extend_len,
            sm_scale=self.scaling,
            logit_cap=self.logit_cap,
        )

        return o

    def decode_forward_triton(self, q, k, v, input_metadata: InputMetadata):
        o = torch.empty_like(q)
        self.store_kv_cache(k, v, input_metadata)

        token_attention_fwd(
            q.view(-1, self.tp_q_head_num, self.head_dim),
            input_metadata.token_to_kv_pool.get_key_buffer(self.layer_id),
            input_metadata.token_to_kv_pool.get_value_buffer(self.layer_id),
            o.view(-1, self.tp_q_head_num, self.head_dim),
            input_metadata.req_to_token_pool.req_to_token,
            input_metadata.req_pool_indices,
            input_metadata.triton_start_loc,
            input_metadata.seq_lens,
            input_metadata.triton_max_seq_len,
            input_metadata.total_num_tokens,
            sm_scale=self.scaling,
            logit_cap=self.logit_cap,
        )

        return o

    def extend_forward_flashinfer(self, q, k, v, input_metadata: InputMetadata):
        o1, s1 = input_metadata.flashinfer_prefill_wrapper_ragged.forward_return_lse(
            q.contiguous().view(-1, self.tp_q_head_num, self.head_dim),
            k.contiguous().view(-1, self.tp_k_head_num, self.head_dim),
            v.contiguous().view(-1, self.tp_v_head_num, self.head_dim),
            causal=True,
            sm_scale=self.scaling,
            logits_soft_cap=self.logit_cap,
        )

        if input_metadata.extend_no_prefix:
            o = o1
        else:
            o2, s2 = input_metadata.flashinfer_prefill_wrapper_paged.forward_return_lse(
                q.contiguous().view(-1, self.tp_q_head_num, self.head_dim),
                input_metadata.token_to_kv_pool.kv_data[self.layer_id],
                causal=False,
                sm_scale=self.scaling,
                logits_soft_cap=self.logit_cap,
            )

            o, _ = merge_state(o1, s1, o2, s2)

        self.store_kv_cache(k, v, input_metadata)

        if input_metadata.total_num_tokens >= global_config.layer_sync_threshold:
            torch.cuda.synchronize()

        return o.view(-1, self.tp_q_head_num * self.head_dim)

    def decode_forward_flashinfer(self, q, k, v, input_metadata: InputMetadata):
        self.store_kv_cache(k, v, input_metadata)

        o = input_metadata.flashinfer_decode_wrapper.forward(
            q.contiguous().view(-1, self.tp_q_head_num, self.head_dim),
            input_metadata.token_to_kv_pool.kv_data[self.layer_id],
            sm_scale=self.scaling,
            logits_soft_cap=self.logit_cap,
        )

        return o.view(-1, self.tp_q_head_num * self.head_dim)

    def forward(self, q, k, v, input_metadata: InputMetadata):
        k = k.view(-1, self.tp_k_head_num, self.head_dim)
        v = v.view(-1, self.tp_v_head_num, self.head_dim)

        if input_metadata.forward_mode == ForwardMode.EXTEND:
            return self.extend_forward(q, k, v, input_metadata)
        elif input_metadata.forward_mode == ForwardMode.DECODE:
            return self.decode_forward(q, k, v, input_metadata)

    def store_kv_cache(self, cache_k, cache_v, input_metadata: InputMetadata):
        key_buffer = input_metadata.token_to_kv_pool.get_key_buffer(self.layer_id)
        value_buffer = input_metadata.token_to_kv_pool.get_value_buffer(self.layer_id)
        if input_metadata.out_cache_loc is not None:
            key_buffer[input_metadata.out_cache_loc] = cache_k
            value_buffer[input_metadata.out_cache_loc] = cache_v
        elif input_metadata.out_cache_cont_start is not None:
            key_buffer[
                input_metadata.out_cache_cont_start : input_metadata.out_cache_cont_end
            ] = cache_k
            value_buffer[
                input_metadata.out_cache_cont_start : input_metadata.out_cache_cont_end
            ] = cache_v
        else:
            raise RuntimeError()
