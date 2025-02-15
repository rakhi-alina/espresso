# Copyright (c) Yiming Wang
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from argparse import Namespace
import logging
from typing import Dict, List, Optional

import torch
from torch import Tensor
import torch.nn.functional as F

from fairseq import utils
from fairseq.distributed import fsdp_wrap
from fairseq.models import (
    FairseqEncoderModel,
    register_model,
    register_model_architecture,
)
from fairseq.models.transformer import Linear
from omegaconf import DictConfig

from espresso.models.speech_lstm import ConvBNReLU
from espresso.models.speech_transformer import SpeechTransformerEncoder
import espresso.tools.utils as speech_utils


DEFAULT_MAX_SOURCE_POSITIONS = 10240


DEFAULT_MIN_PARAMS_TO_WRAP = int(1e8)


logger = logging.getLogger(__name__)


@register_model("speech_transformer_encoder_model")
class SpeechTransformerEncoderModel(FairseqEncoderModel):
    def __init__(self, args, encoder, state_prior: Optional[torch.FloatTensor] = None):
        super().__init__(encoder)
        self.args = args
        self.state_prior = state_prior
        self.num_updates = 0

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        # fmt: off
        parser.add_argument("--activation-fn",
                            choices=utils.get_available_activation_fns(),
                            help="activation function to use")
        parser.add_argument("--dropout", type=float, metavar="D",
                            help="dropout probability")
        parser.add_argument("--encoder-conv-channels", type=str, metavar="EXPR",
                            help="list of encoder convolution's out channels")
        parser.add_argument("--encoder-conv-kernel-sizes", type=str, metavar="EXPR",
                            help="list of encoder convolution's kernel sizes")
        parser.add_argument("--encoder-conv-strides", type=str, metavar="EXPR",
                            help="list of encoder convolution's strides")
        parser.add_argument("--attention-dropout", type=float, metavar="D",
                            help="dropout probability for attention weights")
        parser.add_argument("--activation-dropout", "--relu-dropout", type=float, metavar="D",
                            help="dropout probability after activation in FFN.")
        parser.add_argument("--encoder-ffn-embed-dim", type=int, metavar="N",
                            help="encoder embedding dimension for FFN")
        parser.add_argument("--encoder-layers", type=int, metavar="N",
                            help="num encoder layers")
        parser.add_argument("--encoder-attention-heads", type=int, metavar="N",
                            help="num encoder attention heads")
        parser.add_argument("--encoder-normalize-before", action="store_true",
                            help="apply layernorm before each encoder block")
        parser.add_argument("--encoder-transformer-context", type=str, metavar="EXPR",
                            help="left/right context for time-restricted self-attention; "
                                 "can be None or a tuple of two non-negative integers/None")
        parser.add_argument("--no-token-positional-embeddings", action="store_true",
                            help="if set, disables positional embeddings (outside self attention)")
        parser.add_argument("--layernorm-embedding", action="store_true",
                            help="add layernorm to embedding")
        parser.add_argument("--checkpoint-activations", action="store_true",
                            help="checkpoint activations at each layer, which saves GPU "
                                 "memory usage at the cost of some additional compute")
        parser.add_argument("--offload-activations", action="store_true",
                             help="checkpoint activations at each layer, then save to gpu. Sets --checkpoint-activations.")
        # args for "Reducing Transformer Depth on Demand with Structured Dropout" (Fan et al., 2019)
        parser.add_argument("--encoder-layerdrop", type=float, metavar="D", default=0,
                            help="LayerDrop probability for encoder")
        parser.add_argument("--encoder-layers-to-keep", default=None,
                            help="which layers to *keep* when pruning as a comma-separated list")
        # args for Training with Quantization Noise for Extreme Model Compression ({Fan*, Stock*} et al., 2020)
        parser.add_argument("--quant-noise-pq", type=float, metavar="D", default=0,
                            help="iterative PQ quantization noise at training time")
        parser.add_argument("--quant-noise-pq-block-size", type=int, metavar="D", default=8,
                            help="block size of quantization noise at training time")
        parser.add_argument("--quant-noise-scalar", type=float, metavar="D", default=0,
                            help="scalar quantization noise and scalar quantization at training time")
        # args for Fully Sharded Data Parallel (FSDP) training
        parser.add_argument(
            "--min-params-to-wrap", type=int, metavar="D", default=DEFAULT_MIN_PARAMS_TO_WRAP,
            help=(
                "minimum number of params for a layer to be wrapped with FSDP() when "
                "training with --ddp-backend=fully_sharded. Smaller values will "
                "improve memory efficiency, but may make torch.distributed "
                "communication less efficient due to smaller input sizes. This option "
                "is set to 0 (i.e., always wrap) when --checkpoint-activations or "
                "--offload-activations are passed."
            )
        )
        # fmt: on

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""

        # make sure that all args are properly defaulted (in case there are any new ones)
        base_architecture(args)

        if args.encoder_layers_to_keep:
            args.encoder_layers = len(args.encoder_layers_to_keep.split(","))

        if getattr(args, "max_source_positions", None) is None:
            args.max_source_positions = DEFAULT_MAX_SOURCE_POSITIONS

        if getattr(args, "offload_activations", False):
            args.checkpoint_activations = True  # offloading implies checkpointing

        out_channels = speech_utils.eval_str_nested_list_or_tuple(args.encoder_conv_channels, type=int)
        kernel_sizes = speech_utils.eval_str_nested_list_or_tuple(args.encoder_conv_kernel_sizes, type=int)
        strides = speech_utils.eval_str_nested_list_or_tuple(args.encoder_conv_strides, type=int)
        logger.info("input feature dimension: {}, channels: {}".format(task.feat_dim, task.feat_in_channels))
        assert task.feat_dim % task.feat_in_channels == 0
        conv_layers = ConvBNReLU(
            out_channels, kernel_sizes, strides, in_channels=task.feat_in_channels,
        ) if out_channels is not None else None

        transformer_encoder_input_size = task.feat_dim // task.feat_in_channels
        if conv_layers is not None:
            for stride in strides:
                if isinstance(stride, (list, tuple)):
                    assert len(stride) > 0
                    s = stride[1] if len(stride) > 1 else stride[0]
                else:
                    assert isinstance(stride, int)
                    s = stride
                transformer_encoder_input_size = (transformer_encoder_input_size + s - 1) // s
            transformer_encoder_input_size *= out_channels[-1]
        else:
            transformer_encoder_input_size = task.feat_dim

        encoder_transformer_context = speech_utils.eval_str_nested_list_or_tuple(
            args.encoder_transformer_context, type=int,
        )
        if encoder_transformer_context is not None:
            assert len(encoder_transformer_context) == 2
            for i in range(2):
                assert (
                    encoder_transformer_context[i] is None
                    or (
                        isinstance(encoder_transformer_context[i], int)
                        and encoder_transformer_context[i] >= 0
                    )
                )

        encoder = cls.build_encoder(
            args,
            conv_layers_before=conv_layers,
            input_size=transformer_encoder_input_size,
            transformer_context=encoder_transformer_context,
            num_targets=getattr(task, "num_targets", None),  # targets for encoder-only model
            chunk_width=getattr(task, "chunk_width", None),
            chunk_left_context=getattr(task, "chunk_left_context", 0),
            training_stage=getattr(task, "training_stage", True),
        )
        # fsdp_wrap is a no-op when --ddp-backend != fully_sharded
        encoder = fsdp_wrap(encoder, min_num_params=1e8)
        return cls(args, encoder, state_prior=getattr(task, "initial_state_prior", None))

    def set_num_updates(self, num_updates):
        self.num_updates = num_updates
        super().set_num_updates(num_updates)

    @classmethod
    def build_encoder(
        cls, args, conv_layers_before=None, input_size=83, transformer_context=None,
        num_targets=None, chunk_width=None, chunk_left_context=0, training_stage=True,
    ):
        return SpeechChunkTransformerEncoder(
            args,
            conv_layers_before=conv_layers_before,
            input_size=input_size,
            transformer_context=transformer_context,
            num_targets=num_targets,
            chunk_width=chunk_width,
            chunk_left_context=chunk_left_context,
            training_stage=training_stage,
        )

    def output_lengths(self, in_lengths):
        return self.encoder.output_lengths(in_lengths)

    def get_normalized_probs(self, net_output, log_probs, sample=None):
        """Get normalized probabilities (or log probs) from a net's output."""
        encoder_out = net_output.encoder_out
        if torch.is_tensor(encoder_out):
            logits = encoder_out.float()
            if log_probs:
                return F.log_softmax(logits, dim=-1)
            else:
                return F.softmax(logits, dim=-1)
        raise NotImplementedError

    def update_state_prior(self, new_state_prior, factor=0.1):
        assert self.state_prior is not None
        self.state_prior = self.state_prior.to(new_state_prior)
        self.state_prior = (1. - factor) * self.state_prior + factor * new_state_prior
        self.state_prior = self.state_prior / self.state_prior.sum()  # re-normalize

    def state_dict(self):
        state_dict = super().state_dict()
        state_dict["state_prior"] = self.state_prior
        return state_dict

    def load_state_dict(
        self,
        state_dict,
        strict=True,
        model_cfg: Optional[DictConfig] = None,
        args: Optional[Namespace] = None,
    ):
        state_dict_subset = state_dict.copy()
        self.state_prior = state_dict.get("state_prior", None)
        if "state_prior" in state_dict:
            self.state_prior = state_dict["state_prior"]
            del state_dict_subset["state_prior"]
        super().load_state_dict(
            state_dict_subset, strict=strict, model_cfg=model_cfg, args=args
        )


class SpeechChunkTransformerEncoder(SpeechTransformerEncoder):
    """Transformer encoder for speech (possibly chunk) data."""
    def __init__(
        self, args, conv_layers_before=None, input_size=83, transformer_context=None,
        num_targets=None, chunk_width=None, chunk_left_context=0, training_stage=True,
    ):
        super().__init__(
            args, conv_layers_before=conv_layers_before, input_size=input_size,
            transformer_context=transformer_context,
        )
        receptive_field_radius = (
            sum(conv.padding[0] for conv in conv_layers_before.convolutions)
            if conv_layers_before is not None
            else 0
        )
        assert chunk_width is None or chunk_width > 0
        assert (
            (conv_layers_before is None and chunk_left_context >= 0)
            or (conv_layers_before is not None and chunk_left_context >= receptive_field_radius)
        )
        self.out_chunk_begin = self.output_lengths(chunk_left_context + 1) - 1
        self.out_chunk_end = (
            self.output_lengths(chunk_left_context + chunk_width)
            if chunk_width is not None
            else None
        )
        self.training_stage = training_stage

        # only for encoder-only model
        self.fc_out = (
            Linear(args.encoder_embed_dim, num_targets, dropout=self.dropout_module.p)
            if num_targets is not None
            else None
        )

    def forward(
        self,
        src_tokens,
        src_lengths,
        return_all_hiddens: bool = False,
    ):
        """
        Args:
            src_tokens (LongTensor): tokens in the source language of shape
                `(batch, src_len)`
            src_lengths (LongTensor): lengths of each source sentence of
                shape `(batch)`
            return_all_hiddens (bool, optional): also return all of the
                intermediate hidden states (default: False).

        Returns:
            dict:
                - **encoder_out** (Tensor): the last encoder layer's output of
                  shape `(src_len, batch, embed_dim)`
                - **encoder_padding_mask** (ByteTensor): the positions of
                  padding elements of shape `(batch, src_len)`
                - **encoder_embedding** (Tensor): the (scaled) embedding lookup
                  of shape `(batch, src_len, embed_dim)`
                - **encoder_states** (List[Tensor]): all intermediate
                  hidden states of shape `(src_len, batch, embed_dim)`.
                  Only populated if *return_all_hiddens* is True.
        """
        out = super().forward(src_tokens, src_lengths, return_all_hiddens=return_all_hiddens)
        x, x_lengths = out["encoder_out"][0], out["src_lengths"][0]

        # determine which output frame to select for loss evaluation/test, assuming
        # all examples in a batch are of the same length for chunk-wise training/test
        if (
            self.out_chunk_end is not None
            and (self.training or not self.training_stage)
        ):
            x = x[self.out_chunk_begin: self.out_chunk_end]  # T x B x C -> W x B x C
            x_lengths = x_lengths.fill_(x.size(0))

        if self.fc_out is not None:
            x = self.fc_out(x)  # T x B x C -> T x B x V

        # The Pytorch Mobile lite interpreter does not supports returning NamedTuple in
        # `foward` so we use a dictionary instead.
        # TorchScript does not support mixed values so the values are all lists.
        # The empty list is equivalent to None.
        return {
            "encoder_out": [x],  # T x B x C
            "encoder_padding_mask": [out["encoder_padding_mask"][0].transpose(0, 1)],  # T x B
            "encoder_embedding": out["encoder_embedding"],  # None
            "encoder_states": out["encoder_states"],  # List[T x B x C]
            "src_tokens": out["src_tokens"],  # None
            "src_lengths": [x_lengths],  # B
        }

    @torch.jit.export
    def reorder_encoder_out(self, encoder_out: Dict[str, List[Tensor]], new_order):
        """
        Reorder encoder output according to *new_order*.

        Args:
            encoder_out: output from the ``forward()`` method
            new_order (LongTensor): desired order

        Returns:
            *encoder_out* rearranged according to *new_order*
        """
        if len(encoder_out["encoder_out"]) == 0:
            new_encoder_out = []
        else:
            new_encoder_out = [encoder_out["encoder_out"][0].index_select(1, new_order)]
        if len(encoder_out["encoder_padding_mask"]) == 0:
            new_encoder_padding_mask = []
        else:
            new_encoder_padding_mask = [
                encoder_out["encoder_padding_mask"][0].index_select(1, new_order)  # note: transposed
            ]
        if len(encoder_out["encoder_embedding"]) == 0:
            new_encoder_embedding = []
        else:
            new_encoder_embedding = [
                encoder_out["encoder_embedding"][0].index_select(0, new_order)
            ]
        if len(encoder_out["src_tokens"]) == 0:
            new_src_tokens = []
        else:
            new_src_tokens = [(encoder_out["src_tokens"][0]).index_select(0, new_order)]

        if len(encoder_out["src_lengths"]) == 0:
            new_src_lengths = []
        else:
            new_src_lengths = [(encoder_out["src_lengths"][0]).index_select(0, new_order)]

        encoder_states = encoder_out["encoder_states"]
        if len(encoder_states) > 0:
            for idx, state in enumerate(encoder_states):
                encoder_states[idx] = state.index_select(1, new_order)

        return {
            "encoder_out": new_encoder_out,  # T x B x C
            "encoder_padding_mask": new_encoder_padding_mask,  # B x T
            "encoder_embedding": new_encoder_embedding,  # B x T x C
            "encoder_states": encoder_states,  # List[T x B x C]
            "src_tokens": new_src_tokens,  # B x T
            "src_lengths": new_src_lengths,  # B x 1
        }


@register_model_architecture("speech_transformer_encoder_model", "speech_transformer_encoder_model")
def base_architecture(args):
    args.encoder_conv_channels = getattr(
        args, "encoder_conv_channels", "[64, 64, 128, 128]",
    )
    args.encoder_conv_kernel_sizes = getattr(
        args, "encoder_conv_kernel_sizes", "[(3, 3), (3, 3), (3, 3), (3, 3)]",
    )
    args.encoder_conv_strides = getattr(
        args, "encoder_conv_strides", "[(1, 1), (2, 2), (1, 1), (2, 2)]",
    )
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 256)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 1024)
    args.encoder_layers = getattr(args, "encoder_layers", 12)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 4)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", True)
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.encoder_transformer_context = getattr(args, "encoder_transformer_context", None)
    args.attention_dropout = getattr(args, "attention_dropout", 0.2)
    args.activation_dropout = getattr(args, "activation_dropout", 0.2)
    args.activation_fn = getattr(args, "activation_fn", "relu")
    args.dropout = getattr(args, "dropout", 0.2)
    args.no_token_positional_embeddings = getattr(
        args, "no_token_positional_embeddings", False
    )
    args.adaptive_input = getattr(args, "adaptive_input", False)
    args.layernorm_embedding = getattr(args, "layernorm_embedding", False)
    args.checkpoint_activations = getattr(args, "checkpoint_activations", False)
    args.offload_activations = getattr(args, "offload_activations", False)
    if args.offload_activations:
        args.checkpoint_activations = True
    args.encoder_layers_to_keep = getattr(args, "encoder_layers_to_keep", None)
    args.encoder_layerdrop = getattr(args, "encoder_layerdrop", 0)
    args.quant_noise_pq = getattr(args, "quant_noise_pq", 0)
    args.quant_noise_pq_block_size = getattr(args, "quant_noise_pq_block_size", 8)
    args.quant_noise_scalar = getattr(args, "quant_noise_scalar", 0)


@register_model_architecture("speech_transformer_encoder_model", "speech_transformer_encoder_model_wsj")
def speech_transformer_encoder_wsj(args):
    base_architecture(args)
