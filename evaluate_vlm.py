# Implemented based on https://github.com/huggingface/transfer-learning-conv-ai

import logging
import random
from argparse import ArgumentParser
from itertools import chain
from pprint import pformat
import warnings
import os

import torch
import torch.nn.functional as F
from pytorch_transformers import GPT2LMHeadModel, GPT2Tokenizer, VLM
from train import SPECIAL_TOKENS, build_input_from_segments_dialqa, build_input_from_segments_nlg, build_input_from_segments_mtsm, add_special_tokens_, MAXLEN_MAP
from utils import get_dataset_personalities, download_pretrained_model, get_dataset
import numpy as np
from util.eval_metrics import moses_multi_bleu, rouge #, ent_score
import json
from copy import deepcopy

BOTTLEBECK_MAP = {"mt":300, "summarization":100, "dialogue":100, "qa": 300, "nlg": 10}
COMMAND2ID = {"mt":0, "summarization":1, "dialogue":2, "qa": 3, "nlg":4}
EOSLIST = {"mt":50269, "summarization":50273, "dialogue":50259, "qa":50264, "nlg":50277}
ID2COMMAND = ["mt", "summarization", "dialogue", "qa", "nlg"]
#COMMAND2ID = {"translate":0, "summarize":1, "chat":2, "qa": 3, "nlg":4}
ATTR_TO_SPECIAL_TOKEN = {'pad_token': '<pad>', 'additional_special_tokens': ("<bos_dial>", "<eos_dial>", "<speaker1>", "<speaker2>", "<persona>",
                  "<bos_qa>", "<eos_qa>", "<question>", "<answer>", "<document>", 
                  "<bos_mt>", "<eos_mt>", "<source_mt>", "<target_mt>",
                  "<bos_sm>", "<eos_sm>", "<source_sm>", "<target_sm>",
                  "<bos_nlg>", "<eos_nlg>", "<name>", "<eatType>", "<familyFriendly>", "<priceRange>", "<food>", "<near>", "<area>", "<customerRating>", "<response>",
                  "<pad>",)}
def top_filtering(logits, top_k=0, top_p=0.0, threshold=-float('Inf'), filter_value=-float('Inf')):
    """ Filter a distribution of logits using top-k, top-p (nucleus) and/or threshold filtering
        Args:
            logits: logits distribution shape (vocabulary size)
            top_k: <=0: no filtering, >0: keep only top k tokens with highest probability.
            top_p: <=0.0: no filtering, >0.0: keep only a subset S of candidates, where S is the smallest subset
                whose total probability mass is greater than or equal to the threshold top_p.
                In practice, we select the highest probability tokens whose cumulative probability mass exceeds
                the threshold top_p.
            threshold: a minimal threshold to keep logits
    """
    assert logits.dim() == 1  # Only work for batch size 1 for now - could update but it would obfuscate a bit the code
    top_k = min(top_k, logits.size(-1))
    if top_k > 0:
        # Remove all tokens with a probability less than the last token in the top-k tokens
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        # Compute cumulative probabilities of sorted tokens
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        cumulative_probabilities = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probabilities > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0

        # Back to unsorted indices and set them to -infinity
        indices_to_remove = sorted_indices[sorted_indices_to_remove]
        logits[indices_to_remove] = filter_value

    indices_to_remove = logits < threshold
    logits[indices_to_remove] = filter_value

    return logits


def sample_sequence(tokenizer, model, args, personality=None, history=None, source=None, target=None, current_output=None, task_id = -1):
    special_tokens_ids = tokenizer.convert_tokens_to_ids(SPECIAL_TOKENS[args.task])
    if current_output is None:
        current_output = []

    for i in range(args.max_length):
        if (args.task=="dialogue" or args.task=="qa" or args.task=="smd"):
            instance, _ = build_input_from_segments_dialqa(personality, history, current_output, tokenizer, with_eos=False, task=args.task)
        elif args.task == "nlg":
            instance, _ = build_input_from_segments_nlg(source, current_output, tokenizer, with_eos=False, task=args.task)
        elif (args.task == "mt" or args.task=="summarization"):
            instance, _  = build_input_from_segments_mtsm(source, current_output, tokenizer, with_eos=False, task=args.task)
        input_ids = torch.tensor(instance["input_ids"], device=args.device).unsqueeze(0)
        token_type_ids = torch.tensor(instance["token_type_ids"], device=args.device).unsqueeze(0)
        #print(input_ids)
        #print(token_type_ids)
        logits = model(input_ids, token_type_ids=token_type_ids, task_id = task_id)
        
        #logits = model(input_ids, token_type_ids=token_type_ids)
        if isinstance(logits, tuple):  # for gpt2 and maybe others
            logits = logits[0]
        #mask out some special tokens
        # mask = torch.zeros(50287).bool()
        # mask[50257:EOSLIST[args.task]] = True
        # mask[EOSLIST[args.task]+1:] = True
        # mask.unsqueeze(0).unsqueeze(0)
        # mask.to(args.device)
        
        #logits.masked_fill_(mask, -1e18)
        logits[:,:,50257:EOSLIST[args.task]] = -1e18
        logits[:,:,EOSLIST[args.task]+1:] = -1e18

        if args.self_copy:
            probs = top_filtering(logits[0, -1, :], top_k=1, top_p=0) # generator output probs
            args.no_sample = True
        else:
            logits = logits[0, -1, :] / args.temperature
            logits = top_filtering(logits, top_k=args.top_k, top_p=args.top_p)
            probs = F.softmax(logits, dim=-1)

        prev = torch.topk(probs, 1)[1] if args.no_sample else torch.multinomial(probs, 1)
        if i < args.min_length and prev.item() in special_tokens_ids:
            while prev.item() in special_tokens_ids:
                if probs.max().item() == 1:
                    warnings.warn("Warning: model generating special token with probability 1.")
                    break  # avoid infinitely looping over special token
                prev = torch.multinomial(probs, num_samples=1)

        if prev.item() in special_tokens_ids:
            break
        current_output.append(prev.item())

    return current_output

def run():
    parser = ArgumentParser()
    parser.add_argument("--dataset_path", type=str, default="", help="Path or url of the dataset. If empty download from S3.")
    parser.add_argument("--dataset_cache", type=str, default='./dataset_cache', help="Path or url of the dataset cache")
    parser.add_argument("--model", type=str, default="gpt2", help="Model type (gpt or gpt2)")
    parser.add_argument("--model_checkpoint", type=str, default="gpt2", help="Path, url or short name of the model")
    parser.add_argument("--max_history", type=int, default=2, help="Number of previous utterances to keep in history")
    parser.add_argument("--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu", help="Device (cuda or cpu)")
    parser.add_argument("--no_sample", action='store_true', help="Set to use greedy decoding instead of sampling")
    parser.add_argument("--max_length", type=int, default=150, help="Maximum length of the output utterances")
    parser.add_argument("--min_length", type=int, default=1, help="Minimum length of the output utterances")
    parser.add_argument("--seed", type=int, default=42, help="Seed")
    parser.add_argument("--temperature", type=int, default=0.7, help="Sampling softmax temperature")
    parser.add_argument("--top_k", type=int, default=0, help="Filter top-k tokens before sampling (<=0: no filtering)")
    parser.add_argument("--top_p", type=float, default=0.9, help="Nucleus filtering (top-p) before sampling (<=0.0: no filtering)")
    parser.add_argument("--task", type=str, default="dialogue", help="one of task from [dialogue, qa, mt, nlg, summarization]")
    parser.add_argument("--self_copy", action='store_true', help="add self copy")
    parser.add_argument("--perturbation_layers", type=int, default=0, help="number of perturbation layers")
    parser.add_argument("--adapter_bottleneck", type=int, default=0, help="adapter layer bottleneck")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__file__)
    logger.info(pformat(args))
    

    
    random.seed(args.seed)
    torch.random.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    logger.info("Get pretrained model and tokenizer")
    tokenizer_class = GPT2Tokenizer
    tokenizer = tokenizer_class.from_pretrained(args.model_checkpoint)
    #model_class = GPT2LMHeadModel
    #model = model_class.from_pretrained(args.model_checkpoint, perturbation_layers=0, self_copy=False, adapter_bottleneck=BOTTLEBECK_MAP[args.task])
    
    model_class = VLM
    model = model_class.from_pretrained(args.model_checkpoint, bottleneck_map=BOTTLEBECK_MAP)
    model.to(args.device)
    add_special_tokens_(model, tokenizer)

    if not os.path.exists("results/VLM_result/"):
        os.makedirs("results/VLM_result/")

    if (args.task=="mt" or args.task=="summarization"):
        output_text = []
        ref_text = []
        loaded_dataset = get_dataset(tokenizer, args.dataset_path, args.dataset_cache, args.task)
        for pair in loaded_dataset["test"]:
            source = pair["src"][:MAXLEN_MAP[args.task]['src']]
            target = pair["tgt"]#[:MAXLEN_MAP[args.task]['tgt']]
            with torch.no_grad():
                out_ids = sample_sequence( tokenizer, model, args, source=source, target=target, task_id=COMMAND2ID[args.task])
            out_text = tokenizer.decode(out_ids, skip_special_tokens=True)

            output_text.append(out_text)
            ref_text.append(tokenizer.decode(pair["tgt"], skip_special_tokens=True))
        
        BLEU = moses_multi_bleu(np.array(output_text),np.array(ref_text))
        r_1, r_2, r_l, r_m = rouge(output_text, ref_text)
        print("BLEU:{}".format(BLEU))
        print("ROUGE_1:{}, ROUGE_2:{}, ROUGE_L:{}, ROUGE_mean:{}".format(r_1, r_2, r_l, r_m))
        with open("results/VLM_result/"+args.task+"_output.txt", 'w', encoding='utf-8') as f:
            for line in output_text:
                f.write(line)
                f.write('\n')
        with open("results/VLM_result/"+args.task+"_ref.txt", 'w', encoding='utf-8') as f:
            for line in ref_text:
                f.write(line)
                f.write('\n')

    # nlg interact
    if args.task=="nlg":
        output_text = []
        ref_text = []
        loaded_dataset = get_dataset(tokenizer, args.dataset_path, args.dataset_cache, args.task)
        for pair in loaded_dataset["test"]:
            source = pair["src"]
            target = pair["tgt"]
            with torch.no_grad():
                out_ids = sample_sequence(tokenizer, model, args, source=source, target=target, task_id=COMMAND2ID[args.task] )
            out_text = tokenizer.decode(out_ids, skip_special_tokens=True)
            # print("input: ")
            # print([{k: tokenizer.decode(v, skip_special_tokens=True)} for k, v in pair["src"].items()])
            # print("model output: ")
            # print(out_text)
            # print("ref: ")
            # print(tokenizer.decode(pair["tgt"], skip_special_tokens=True))
            # print("======================================")
            output_text.append(out_text)
            ref_text.append(tokenizer.decode(pair["tgt"], skip_special_tokens=True))
        with open("results/VLM_result/nlg_output.txt", 'w', encoding='utf-8') as f:
            for line in output_text:
                f.write(line)
                f.write('\n')
        with open("results/VLM_result/nlg_ref.txt", 'w', encoding='utf-8') as f:
            for line in ref_text:
                f.write(line)
                f.write('\n')



    if args.task=="dialogue":
        output_text = []
        ref_text = []
        loaded_dataset = get_dataset(tokenizer, args.dataset_path, args.dataset_cache, args.task)
        persona_text = []
        for pair in loaded_dataset["valid"]:
            persona = pair["personality"].copy()
            for utterance in pair["utterances"]:
                history = utterance["history"][-(2*args.max_history+1):]
                with torch.no_grad():
                    out_ids = sample_sequence( tokenizer, model, args, personality=persona, history=history, task_id=COMMAND2ID[args.task])
                out_text = tokenizer.decode(out_ids, skip_special_tokens=True)
                output_text.append(out_text)
                ref_text.append(tokenizer.decode(utterance["candidates"][-1], skip_special_tokens=True))
                persona_text.append([tokenizer.decode(p, skip_special_tokens=True)for p in persona])

        with open("results/VLM_result/"+args.task+"_output.txt", 'w', encoding='utf-8') as f:
            for line in output_text:
                f.write(line)
                f.write('\n')
        with open("results/VLM_result/"+args.task+"_ref.txt", 'w', encoding='utf-8') as f:
            for line in ref_text:
                f.write(line)
                f.write('\n')
        with open("results/VLM_result/"+args.task+"_persona.txt", 'w', encoding='utf-8') as f:
            for line in persona_text:
                f.write("\t".join(line))
                f.write('\n')

        # print("Evaluate ENT score")
        # ent_ref_arr = []
        # ent_pred_arr = []
        # for ref, pred, per in zip(ref_text,output_text,persona_text):
        #     ent_ref_arr.append(ent_score(ref,per))
        #     ent_pred_arr.append(ent_score(pred,per))
        print("Evaluate BLEU")

        BLEU = moses_multi_bleu(np.array(output_text),np.array(ref_text))
        print("BLEU:{}".format(BLEU))
        # print("ENT REF:{}".format(np.mean(ent_ref_arr)))
        # print("ENT PRED:{}".format(np.mean(ent_pred_arr)))

        with open("results/VLM_result/"+args.task+"_output.txt", 'w', encoding='utf-8') as f:
            for line in output_text:
                f.write(line)
                f.write('\n')
        with open("results/VLM_result/"+args.task+"_ref.txt", 'w', encoding='utf-8') as f:
            for line in ref_text:
                f.write(line)
                f.write('\n')
        with open("results/VLM_result/"+args.task+"_persona.txt", 'w', encoding='utf-8') as f:
            for line in persona_text:
                f.write("\t".join(line))
                f.write('\n')
    # qa interact
    if args.task=="qa":
        output_text = []
        ref_text = []
        loaded_dataset = get_dataset(tokenizer, args.dataset_path, args.dataset_cache, args.task)
        ## load dev_set_with_ids here
        for pair in loaded_dataset["valid"]:
            evidence = pair["document"].copy()
            evidence = [evidence[0][:MAXLEN_MAP[args.task]['document']]]
            for utterance in pair["utterances"]:
                history = utterance["history"][-(2*args.max_history+1):]
                with torch.no_grad():
                    out_ids = sample_sequence( tokenizer, model, args, personality=evidence, history=history, task_id=COMMAND2ID[args.task])
                out_text = tokenizer.decode(out_ids, skip_special_tokens=True)
                output_text.append({"id": pair['id'],"turn_id": utterance['turn_id'],"answer": out_text})
                 
        data = json.dumps(output_text)
        with open("results/VLM_result/"+args.task+"_output.txt", 'w', encoding='utf-8') as f:
            f.write(data)

    
if __name__ == "__main__":
    run()
