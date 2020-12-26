#!/usr/bin/env python3
# Copyright (c) Yiming Wang
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import logging
import os
import sys

import torch


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
    stream=sys.stdout,
)
logger = logging.getLogger("mobvoihotwords.create_decoding_graph")


def get_parser():
    parser = argparse.ArgumentParser(
        description="Create the decoding graph for decoding"
    )
    # fmt: off
    parser.add_argument("--HCL-inv-path", type=str, help="path to the HCL_inv fst file (torch_saved)", required=True)
    parser.add_argument("--G-path", type=str, help="path to the LM fsa (openfst text format or torch saved)", required=True)
    parser.add_argument("--out-dir", type=str, default="data", help="directory to save the decoding graph")
    # fmt: on

    return parser


def main(args):
    try:
        import k2
    except ImportError:
        raise ImportError("Please install k2 by `pip install k2`")

    HCL_inv = k2.arc_sort(k2.Fsa.from_dict(torch.load(args.HCL_inv_path)))

    if args.lm_fsa_path[-3:] == ".pt":
        G = k2.Fsa.from_dict(torch.load(args.G_path))
    else:
        with open(args.lm_fsa_path, "r", encoding="utf-8") as f:
            G = k2.Fsa.from_openfst(f.read(), acceptor=True)
    assert not hasattr(G, "aux_labels")
    G = k2.arc_sort(G)

    HCLG = k2.invert(k2.connect(k2.intersect(G, HCL_inv)))
    HCLG = k2.determinize(HCLG)
    HCLG = k2.connect(HCLG)
    save_path = os.path.join(args.out_dir, "HCLG.pt")
    torch.save(HCLG.as_dict(), save_path)
    logger.info(f"saved the decoding graph as {save_path}")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    main(args)
