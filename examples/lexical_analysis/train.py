#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import ast
import math
import argparse
from functools import partial

import numpy as np
import paddle
from paddle.static import InputSpec
from paddlenlp.data import Pad, Tuple, Stack
from paddlenlp.layers.crf import LinearChainCrfLoss, ViterbiDecoder
from paddlenlp.metrics import ChunkEvaluator
import distutils.util

from data import load_dataset, load_vocab, convert_example
from model import BiGruCrf, get_loss

# yapf: disable
parser = argparse.ArgumentParser(__doc__)
parser.add_argument("--data_dir", type=str, default=None, help="The folder where the dataset is located.")
parser.add_argument("--init_checkpoint", type=str, default=None, help="Path to init model.")
parser.add_argument("--model_save_dir", type=str, default=None, help="The model will be saved in this path.")
parser.add_argument("--epochs", type=int, default=10, help="Corpus iteration num.")
parser.add_argument("--batch_size", type=int, default=300, help="The number of sequences contained in a mini-batch.")
parser.add_argument("--max_seq_len", type=int, default=64, help="Number of words of the longest seqence.")
parser.add_argument("--device", default="gpu", type=str, choices=["cpu", "gpu", "xpu"] ,help="The device to select to train the model, is must be cpu/gpu/xpu.")
parser.add_argument("--base_lr", type=float, default=0.001, help="The basic learning rate that affects the entire network.")
parser.add_argument("--emb_dim", type=int, default=128, help="The dimension in which a word is embedded.")
parser.add_argument("--hidden_size", type=int, default=128, help="The number of hidden nodes in the GRU layer.")
parser.add_argument("--verbose", type=ast.literal_eval, default=128, help="Print reader and training time in details.")
parser.add_argument("--do_eval", type=distutils.util.strtobool, default=True, help="To evaluate the model if True.")
# yapf: enable


def train(args):
    paddle.set_device(args.device)

    # Create dataset.
    train_ds, test_ds = load_dataset(datafiles=(os.path.join(
        args.data_dir, 'train.tsv'), os.path.join(args.data_dir, 'test.tsv')))

    word_vocab = load_vocab(os.path.join(args.data_dir, 'word.dic'))
    label_vocab = load_vocab(os.path.join(args.data_dir, 'tag.dic'))
    token_replace_vocab = load_vocab(os.path.join(args.data_dir, 'q2b.dic'))

    trans_func = partial(
        convert_example,
        max_seq_len=args.max_seq_len,
        word_vocab=word_vocab,
        label_vocab=label_vocab,
        token_replace_vocab=token_replace_vocab)
    train_ds.map(trans_func)
    test_ds.map(trans_func)

    batchify_fn = lambda samples, fn=Tuple(
        Pad(axis=0, pad_val=0, dtype='int64'),  # word_ids
        Stack(dtype='int64'),  # length
        Pad(axis=0, pad_val=0, dtype='int64'),  # label_ids
    ): fn(samples)

    # Create sampler for dataloader
    train_sampler = paddle.io.DistributedBatchSampler(
        dataset=train_ds,
        batch_size=args.batch_size,
        shuffle=True,
        drop_last=True)
    train_loader = paddle.io.DataLoader(
        dataset=train_ds,
        batch_sampler=train_sampler,
        return_list=True,
        collate_fn=batchify_fn)

    test_sampler = paddle.io.BatchSampler(
        dataset=test_ds,
        batch_size=args.batch_size,
        shuffle=False,
        drop_last=False)
    test_loader = paddle.io.DataLoader(
        dataset=test_ds,
        batch_sampler=test_sampler,
        return_list=True,
        collate_fn=batchify_fn)

    # Define the model netword and its loss
    network = BiGruCrf(args.emb_dim, args.hidden_size,
                       len(word_vocab), len(label_vocab))

    inputs = InputSpec(shape=(-1, ), dtype="int64", name='inputs')
    lengths = InputSpec(shape=(-1, ), dtype="int64", name='lengths')
    labels = InputSpec(shape=(-1, ), dtype="int64", name='labels')
    model = paddle.Model(network, inputs=[inputs, lengths, labels])

    # Prepare optimizer, loss and metric evaluator
    optimizer = paddle.optimizer.Adam(
        learning_rate=args.base_lr, parameters=model.parameters())
    chunk_evaluator = ChunkEvaluator(label_list=label_vocab.keys(), suffix=True)
    model.prepare(optimizer, get_loss, chunk_evaluator)
    if args.init_checkpoint:
        model.load(args.init_checkpoint)

    # Start training
    callbacks = paddle.callbacks.ProgBarLogger(
        log_freq=10, verbose=3) if args.verbose else None
    model.fit(train_data=train_loader,
              eval_data=test_loader if args.do_eval else None,
              batch_size=args.batch_size,
              epochs=args.epochs,
              eval_freq=1,
              log_freq=10,
              save_dir=args.model_save_dir,
              save_freq=1,
              shuffle=True,
              callbacks=callbacks)


if __name__ == "__main__":
    args = parser.parse_args()
    train(args)
