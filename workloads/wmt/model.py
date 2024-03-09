import sys
import math
from torch import Tensor
import torch
import torch.nn as nn
from torch.nn import Transformer
import evaluate
from runtime.parameter_groups import GroupedModel, ParameterGroup, merge_parameter_splits
from runtime.specs import RuntimeSpecs
from submissions import Submission
from workloads import WorkloadModel
from workloads.wmt.data \
    import WMTDataModule, PAD_IDX, BOS_IDX, EOS_IDX, MAX_TOKENS_PER_SENTENCE, sequential_transforms, tensor_transform


# code inspired by: https://pytorch.org/tutorials/beginner/translation_transformer.html


def create_mask(src, tgt):
    src_seq_len = src.shape[0]
    tgt_seq_len = tgt.shape[0]

    tgt_mask = Transformer.generate_square_subsequent_mask(tgt_seq_len)
    src_mask = torch.zeros((src_seq_len, src_seq_len)).type(torch.bool)

    src_padding_mask = (src == PAD_IDX).transpose(0, 1)
    tgt_padding_mask = (tgt == PAD_IDX).transpose(0, 1)
    return src_mask, tgt_mask, src_padding_mask, tgt_padding_mask


class PositionalEncoding(nn.Module):
    def __init__(self,
                 emb_size: int,
                 dropout: float,
                 maxlen: int = MAX_TOKENS_PER_SENTENCE):
        super(PositionalEncoding, self).__init__()
        den = torch.exp(- torch.arange(0, emb_size, 2)* math.log(10000) / emb_size)
        pos = torch.arange(0, maxlen).reshape(maxlen, 1)
        pos_embedding = torch.zeros((maxlen, emb_size))
        pos_embedding[:, 0::2] = torch.sin(pos * den)
        pos_embedding[:, 1::2] = torch.cos(pos * den)
        pos_embedding = pos_embedding.unsqueeze(-2)

        self.dropout = nn.Dropout(dropout)
        self.register_buffer('pos_embedding', pos_embedding)

    def forward(self, token_embedding: Tensor):
        return self.dropout(token_embedding + self.pos_embedding[:token_embedding.size(0), :])


class TokenEmbedding(nn.Module):
    def __init__(self, vocab_size: int, emb_size):
        super(TokenEmbedding, self).__init__()
        self.embedding = nn.Embedding(vocab_size, emb_size, padding_idx=PAD_IDX)
        self.emb_size = emb_size

    def forward(self, tokens: Tensor):
        return self.embedding(tokens.long()) * math.sqrt(self.emb_size)


class Seq2SeqTransformer(nn.Module):
    def __init__(self,
                 num_encoder_layers: int,
                 num_decoder_layers: int,
                 emb_size: int,
                 nhead: int,
                 src_vocab_size: int,
                 tgt_vocab_size: int,
                 dim_feedforward: int = 512,
                 dropout: float = 0.1,
                 norm_first: bool = False):
        super().__init__()
        self.transformer = Transformer(d_model=emb_size,
                                       nhead=nhead,
                                       num_encoder_layers=num_encoder_layers,
                                       num_decoder_layers=num_decoder_layers,
                                       dim_feedforward=dim_feedforward,
                                       dropout=dropout,
                                       norm_first=norm_first)
        self.generator = nn.Linear(emb_size, tgt_vocab_size)
        self.src_tok_emb = TokenEmbedding(src_vocab_size, emb_size)
        self.tgt_tok_emb = TokenEmbedding(tgt_vocab_size, emb_size)
        self.positional_encoding = PositionalEncoding(
            emb_size, dropout=dropout, maxlen=MAX_TOKENS_PER_SENTENCE)

    def forward(self,
                src: Tensor,
                trg: Tensor,
                src_mask: Tensor,
                tgt_mask: Tensor,
                src_padding_mask: Tensor,
                tgt_padding_mask: Tensor,
                memory_key_padding_mask: Tensor):
        src_emb = self.positional_encoding(self.src_tok_emb(src))
        tgt_emb = self.positional_encoding(self.tgt_tok_emb(trg))
        outs = self.transformer(src_emb, tgt_emb, src_mask, tgt_mask, None,
                                src_padding_mask, tgt_padding_mask, memory_key_padding_mask)
        return self.generator(outs)

    def encode(self, src: Tensor, src_mask: Tensor):
        return self.transformer.encoder(self.positional_encoding(
                            self.src_tok_emb(src)), src_mask)

    def decode(self, tgt: Tensor, memory: Tensor, tgt_mask: Tensor):
        return self.transformer.decoder(self.positional_encoding(
                          self.tgt_tok_emb(tgt)), memory,
                          tgt_mask)


class GroupedTransformer(GroupedModel):
    def __init__(self, model: Seq2SeqTransformer) -> None:
        super().__init__(model)
        self.generator = self.model.generator

    def encode(self, src: Tensor, src_mask: Tensor):
        return self.model.encode(src, src_mask)

    def decode(self, tgt: Tensor, memory: Tensor, tgt_mask: Tensor):
        return self.model.decode(tgt, memory, tgt_mask)

    def parameter_groups(self) -> list[ParameterGroup]:
        split1 = super().parameter_groups()  # default split
        split2 = [ParameterGroup(dict(self.model.named_parameters()), lr_multiplier=0.1)]  # use less learning rate
        return merge_parameter_splits(split1, split2)


class WMTModel(WorkloadModel):
    def __init__(self, submission: Submission, data_module: WMTDataModule):
        self.vocab_size = data_module.vocab_size
        self.batch_size = data_module.batch_size
        self.train_data_len = data_module.train_data_len
        self.tokenizer = data_module.tokenizer
        self.vocab_transform = data_module.vocab_transform
        self.bleu = evaluate.load("bleu", cache_dir=str(data_module.cache_dir))
        self.metric_cache_pred: list[str] = []
        self.metric_cache_trues: list[str] = []
        if "de" not in self.vocab_size:
            raise Exception("prepare dataset before running the model!")
        model = GroupedTransformer(Seq2SeqTransformer(6, 6, 1024, 16, self.vocab_size["de"], self.vocab_size["en"], 1024))

        for p in model.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        super().__init__(model, submission)
        self.loss = nn.functional.cross_entropy

    def forward(self, src: str) -> str:
        return self.translate(src)

    def greedy_decode(self, src: Tensor, src_mask: Tensor, max_len=MAX_TOKENS_PER_SENTENCE, start_symbol=BOS_IDX) -> Tensor:
        self.model: Seq2SeqTransformer
        memory = self.model.encode(src, src_mask)
        ys = torch.ones(1, 1).fill_(start_symbol).type(torch.long).to(self.device)
        for i in range(max_len-1):
            tgt_mask = (Transformer.generate_square_subsequent_mask(ys.size(0))
                        .type(torch.bool)).to(self.device)
            out = self.model.decode(ys, memory, tgt_mask)
            out = out.transpose(0, 1)
            prob = self.model.generator(out[:, -1])
            _, next_word = torch.max(prob, dim=1)
            next_word = next_word.item()

            ys = torch.cat([ys,
                            torch.ones(1, 1).type_as(src.data).fill_(next_word)], dim=0)
            if next_word == EOS_IDX:
                break
        return ys

    def beam_search(self, src: Tensor,
                    src_mask: Tensor,
                    beam_width=4,
                    length_penalty=0.6,
                    max_len=MAX_TOKENS_PER_SENTENCE,
                    start_symbol=BOS_IDX) -> Tensor:
        self.model: Seq2SeqTransformer
        memory = self.model.encode(src, src_mask)
        ys = torch.ones(1, 1).fill_(start_symbol).type(torch.long).to(self.device)
        topk = [(0, ys)]
        for i in range(max_len-1):
            candidates = []
            for score, seq in topk:
                if seq[0][-1] == EOS_IDX:
                    candidates.append((score, seq))
                    continue
                tgt_mask = (Transformer.generate_square_subsequent_mask(seq.size(0))
                            .type(torch.bool)).to(self.device)
                out = self.model.decode(seq, memory, tgt_mask)
                out = out.transpose(0, 1)
                prob = self.model.generator(out[:, -1])
                prob = torch.softmax(prob, dim=1)

                # Get top k candidates
                topk_probs, topk_idx = torch.topk(prob, beam_width)
                for i in range(beam_width):
                    new_seq = torch.cat([seq, topk_idx[0, i].unsqueeze(0).unsqueeze(0)], dim=0)
                    new_score = score + topk_probs[0, i].item()
                    candidates.append((new_score, new_seq))

                _, next_word = torch.max(prob, dim=1)
                next_word = next_word.item()

             # Select top k candidates
            topk = sorted(candidates, key=lambda x: x[0] / (x[1].size(0) ** length_penalty), reverse=True)[:beam_width]
        return topk[0][1]

    def translate(self, src: str) -> str:
        def transform_text(sentence: str) -> Tensor:
            return sequential_transforms(
                    self.tokenizer["de"],
                    self.vocab_transform["de"],
                    tensor_transform)(sentence)  # type: ignore

        src_tensor = transform_text(src).view(-1, 1).to(self.device)
        num_tokens = src_tensor.shape[0]
        src_mask = (torch.zeros(num_tokens, num_tokens)).type(torch.bool).to(self.device)
        tgt_tokens = self.beam_search(src_tensor, src_mask, max_len=num_tokens + 5).flatten()
        return " ".join(self.vocab_transform["en"].lookup_tokens(list(tgt_tokens.cpu().numpy()))).replace("<bos>", "").replace("<eos>", "")

    def training_step(self, batch, batch_idx):
        return self.compute_and_log_loss(batch, "train_loss")

    def compute_bleu(self, en_preds: list[str], en_target: list[str]) -> float:
        assert len(en_preds) == len(en_target)
        try:
            result = self.bleu.compute(predictions=en_preds, references=[[t] for t in en_target])
        except ZeroDivisionError:
            print("Error: Bleu Score computing resulted in a ZeroDivisionError", file=sys.stderr)
            result = {"bleu": 0.0}
        return result["bleu"]  # type: ignore

    def validation_step(self, batch, batch_idx):
        src, tgt, de, en = batch
        self.compute_and_log_loss((src, tgt), "val_loss")
        self.metric_cache_trues += en
        self.metric_cache_pred += [self.translate(s) for s in de]

    def test_step(self, batch, batch_idx):
        src, tgt, de, en = batch
        self.compute_and_log_loss((src, tgt), "test_loss")
        self.metric_cache_trues += en
        self.metric_cache_pred += [self.translate(s) for s in de]

    def on_validation_epoch_end(self):
        bleu = self.compute_bleu(self.metric_cache_pred, self.metric_cache_trues)
        self.log("val_bleu", bleu, batch_size=self.batch_size, sync_dist=True)
        self.metric_cache_trues.clear()
        self.metric_cache_pred.clear()

    def on_test_epoch_end(self) -> None:
        bleu = self.compute_bleu(self.metric_cache_pred, self.metric_cache_trues)
        self.log("test_bleu", bleu, batch_size=self.batch_size, sync_dist=True)
        self.metric_cache_trues.clear()
        self.metric_cache_pred.clear()

    def compute_and_log_loss(self, batch, log_name: str):
        src, tgt = batch
        tgt_input = tgt[:-1, :]
        src_mask, tgt_mask, src_padding_mask, tgt_padding_mask = create_mask(src, tgt_input)
        src_mask = src_mask.to(self.device)
        tgt_mask = tgt_mask.to(self.device)
        logits = self.model(src, tgt_input, src_mask, tgt_mask, src_padding_mask, tgt_padding_mask, src_padding_mask)
        tgt_out = tgt[1:, :]
        loss = self.loss(logits.reshape(-1, logits.shape[-1]), tgt_out.reshape(-1),
                         ignore_index=PAD_IDX, label_smoothing=0.1)
        self.log(log_name, loss, batch_size=self.batch_size, sync_dist=True)
        return loss

    def get_specs(self) -> RuntimeSpecs:
        epochs = 18
        devices = 4
        return RuntimeSpecs(
            max_epochs=epochs,
            max_steps=math.ceil(self.train_data_len / self.batch_size / devices) * epochs,
            devices=devices,
            target_metric="val_loss",
            target_metric_mode="min"
        )