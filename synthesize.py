import re
import numpy as np
import torch
import time


from string import punctuation
from g2p_en import G2p
from text import text_to_sequence
from utils.tools import get_mask_from_lengths, synth_one_sample

def read_lexicon(lex_path):
    lexicon = {}
    with open(lex_path) as f:
        for line in f:
            temp = re.split(r"\s+", line.strip("\n"))
            word = temp[0]
            phones = temp[1:]
            if word.lower() not in lexicon:
                lexicon[word.lower()] = phones
    return lexicon


def get_lexicon_and_g2p(preprocess_config):
    lexicon = read_lexicon(preprocess_config["path"]["lexicon_path"])
    g2p = G2p()
    return lexicon, g2p


def preprocess_english(lexicon, g2p, text, preprocess_config):
    text = text.rstrip(punctuation)

    lang = preprocess_config["preprocessing"]["text"]["language"]
    phones = []
    words = re.split(r"([,;.\-\?\!\s+])", text)
    for w in words:
        if w.lower() in lexicon:
            phones += lexicon[w.lower()]
        elif lang == "t1":
            phones += list(w.lower())
        else:
            phones += list(filter(lambda p: p != " ", g2p(w)))
    phones = "{" + "}{".join(phones) + "}"
    phones = re.sub(r"\{[^\w\s]?\}", "{sp}", phones)
    phones = phones.replace("}{", " ")


    print("Raw Text Sequence: {}".format(text))
    print("Phoneme Sequence: {}".format(phones))

    sequence = np.array(
        text_to_sequence(
            phones, preprocess_config["preprocessing"]["text"]["text_cleaners"]
        )
    )

    return sequence

def synthesize(lexicon, g2p, args, phoneme2mel, hifigan, preprocess_config, verbose=False):
    assert(args.text is not None)

    if verbose:
        start_time = time.time()
    
    phoneme = np.array([preprocess_english(lexicon, g2p, args.text, preprocess_config)])
    phoneme_len = np.array([len(phoneme[0])])

    phoneme = torch.from_numpy(phoneme).long()  
    phoneme_len = torch.from_numpy(phoneme_len) 
    max_phoneme_len = torch.max(phoneme_len).item()
    phoneme_mask = get_mask_from_lengths(phoneme_len, max_phoneme_len)
    x = {"phoneme": phoneme, "phoneme_mask": phoneme_mask}

    if verbose:
        elapsed_time = time.time() - start_time
        print("(Preprocess) time: {:.4f}s".format(elapsed_time))

        start_time = time.time()
    
    with torch.no_grad():
        y = phoneme2mel(x, train=False)
        
    if verbose:
        elapsed_time = time.time() - start_time
        print("(Phoneme2Mel) Synthesizing MEL time: {:.4f}s".format(elapsed_time))
    
    mel_pred = y["mel"]
    mel_pred_len = y["mel_len"]

    return synth_one_sample(mel_pred, mel_pred_len, vocoder=hifigan,
                            preprocess_config=preprocess_config, wav_path=args.wav_path)


def load_jit_modules(args):
    phoneme2mel_ckpt = os.path.join(args.checkpoints, args.phoneme2mel_jit)
    hifigan_ckpt = os.path.join(args.checkpoints, args.hifigan_jit)
    phoneme2mel = torch.jit.load(phoneme2mel_ckpt)
    hifigan = torch.jit.load(hifigan_ckpt)
    return phoneme2mel, hifigan

def load_module(args, pl_module, preprocess_config):
    print("Loading model checkpoint ...", args.checkpoint)
    pl_module = pl_module.load_from_checkpoint(args.checkpoint, preprocess_config=preprocess_config,
                                               lr=args.lr, warmup_epochs=args.warmup_epochs, max_epochs=args.max_epochs,
                                               depth=args.depth, n_blocks=args.n_blocks, block_depth=args.block_depth,
                                               reduction=args.reduction, head=args.head,
                                               embed_dim=args.embed_dim, kernel_size=args.kernel_size,
                                               decoder_kernel_size=args.decoder_kernel_size,
                                               expansion=args.expansion, 
                                               hifigan_checkpoint=args.hifigan_checkpoint,
                                               infer_device=args.infer_device, 
                                               verbose=args.verbose)
    pl_module.eval()

    if args.onnx is not None:
        # random tensor of type int64
        phoneme = torch.randint(low=1, high=10, size=(1,256)).long()
        
        # random tensor of type bool
        #phoneme_mask = torch.randint(low=0, high=2, size=(1,256)).bool()
        #phoneme_mask = torch.ones(1,256)
        #x = {"phoneme": phoneme, "phoneme_mask": phoneme_mask}
        x = {"phoneme": phoneme, }
        print("Converting to ONNX ...", args.onnx)
        #pl_module.to_onnx
        pl_module.eval()
        torch.onnx.export(pl_module, x, args.onnx, export_params=True, 
                          opset_version=10, do_constant_folding=True, 
                          input_names=["phoneme"], output_names=["wav"],
                          dynamic_axes={
                              "phoneme": {0: "batch", 1: "sequence_len"},
                              "wav": {0: "batch", 1: "sequence_len"},
                              #"phoneme": {0: "" 1: "sequence_length"}
                              #"wav": {1: "sequence_length"}, 
                              })
    elif args.jit is not None:
        print("Converting to JIT ...", args.jit)
        #pl_module.to_jit()
        script = pl_module.to_torchscript()
        torch.jit.save(script, args.jit)

    
    phoneme2mel = pl_module.phoneme2mel
    pl_module.hifigan.eval()
    hifigan = pl_module.hifigan
    return phoneme2mel, hifigan