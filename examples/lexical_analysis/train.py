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
import time
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
from model import BiGruCrf

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


def evaluate(model, metric, data_loader):
    model.eval()
    metric.reset()
    avg_loss, precision, recall, f1_score = 0, 0, 0, 0
    for batch in data_loader:
        token_ids, length, labels = batch
        preds = model(token_ids, length)
        num_infer_chunks, num_label_chunks, num_correct_chunks = metric.compute(
            length, preds, labels)
        metric.update(num_infer_chunks.numpy(),
                      num_label_chunks.numpy(), num_correct_chunks.numpy())
        precision, recall, f1_score = metric.accumulate()
    print("eval loss: %f, precision: %f, recall: %f, f1: %f" %
          (avg_loss, precision, recall, f1_score))
    model.train()


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
    model = BiGruCrf(args.emb_dim, args.hidden_size,
                     len(word_vocab), len(label_vocab))
    # Prepare optimizer, loss and metric evaluator
    optimizer = paddle.optimizer.Adam(
        learning_rate=args.base_lr, parameters=model.parameters())
    chunk_evaluator = ChunkEvaluator(label_list=label_vocab.keys(), suffix=True)

    if args.init_checkpoint:
        model.load(args.init_checkpoint)

    # Start training
    global_step = 0
    last_step = args.epochs * len(train_loader)
    tic_train = time.time()
    for epoch in range(args.epochs):
        for step, batch in enumerate(train_loader):
            global_step += 1
            token_ids, length, label_ids = batch
            loss = model(token_ids, length, label_ids)
            avg_loss = paddle.mean(loss)
            if global_step % args.logging_steps == 0:
                print(
                    "global step %d / %d, epoch: %d / %d, batch: %d, loss: %f, speed: %.2f step/s"
                    % (global_step, last_step, epoch, args.epochs, step,
                       avg_loss,
                       args.logging_steps / (time.time() - tic_train)))
                tic_train = time.time()
            avg_loss.backward()
            optimizer.step()
            optimizer.clear_grad()
            if global_step % args.save_steps == 0 or global_step == last_step:
                if paddle.distributed.get_rank() == 0:
                    evaluate(model, chunk_evaluator, test_loader)
                    paddle.save(model.state_dict(),
                                os.path.join(args.output_dir,
                                             "model_%d.pdparams" % global_step))


if __name__ == "__main__":
    args = parser.parse_args()
    train(args)
