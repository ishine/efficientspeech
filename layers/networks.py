
from einops import rearrange
from torch import nn
from .blocks import EfficientSelfAttention, MixFFN
from .acoustic import LengthRegulator
from text.symbols import symbols
#from utils.tools import get_mask_from_lengths

import torch

class Encoder(nn.Module):
    """ Encoder """

    def __init__(self, depth=2, embed_dim=256, kernel_size=5, expansion=2, reduction=4, head=2):
        super().__init__()

        small_embed_dim = embed_dim // reduction
        dim_ins = [small_embed_dim*(i+1) for i in range(depth-1)]
        dim_ins.insert(0, embed_dim)
        self.dim_outs = [small_embed_dim*2**i for i in range(depth)]
        heads = [head*(i+1) for i in range(depth)]
        kernels = [kernel_size-(2 if i>0 else 0) for i in range(depth)]
        paddings = [k//2 for k in kernels]
        strides = [2 for _ in range(depth-1)]
        strides.insert(0, 1)
        
        self.embed = nn.Embedding(len(symbols) + 1, embed_dim, padding_idx=0)

        self.attn_blocks = nn.ModuleList([])
        for dim_in, dim_out, head, kernel, stride, padding in zip(dim_ins, self.dim_outs, heads, kernels, strides, paddings):
            self.attn_blocks.append(
                    nn.ModuleList([
                        nn.Conv1d(dim_in, dim_in, kernel_size=kernel, stride=stride, padding=padding, bias=False),
                        nn.Conv1d(dim_in, dim_out, kernel_size=1, bias=False), 
                        EfficientSelfAttention(dim_out, head=head), 
                        MixFFN(dim_out, expansion),
                        nn.LayerNorm(dim_out),
                        ]))

    def get_feature_dims(self):
        return self.dim_outs

    def forward(self, phoneme, mask=None):
        features = []
        x = self.embed(phoneme) 
        # merge, attn and mixffn operates on n or seqlen dim
        # b = batch, n = sequence len, c = channel (1st layer is embedding)
        # (b, n, c)
        n = x.shape[-2]
        decoder_mask = None

        for merge3x3, merge1x1, attn, mixffn, norm in self.attn_blocks:
            # after each encoder block, merge features
            x = rearrange(x, 'b n c -> b c n')
            x = merge3x3(x)
            x = merge1x1(x)
            x = rearrange(x, 'b c n -> b n c')
            # self-attention with skip connect
            pool = round(n / x.shape[-2])
            y, attn_mask = attn(x, mask=mask, pool=pool)
            x = norm(y + x)
            if attn_mask is not None:
                x = x.masked_fill(attn_mask, 0)
                if decoder_mask is None:
                    decoder_mask = attn_mask
           
            # Mix-FFN with skip connect
            x = norm(mixffn(x) + x)
            
            if attn_mask is not None:
                x = x.masked_fill(attn_mask, 0)
            # mlp decoder operates on c or channel dim
            features.append(x)

        return features, decoder_mask


class AcousticDecoder(nn.Module):
    """ Pitch, Duration, Energy Decoder """

    def __init__(self, dims, pitch_stats=None, energy_stats=None, n_mel_channels=80, dropout=0.1, duration=False):
        super().__init__()

        assert(len(dims)>0)
        
        self.n_mel_channels = n_mel_channels
        dim = dims[0]

        self.conv1 = nn.Sequential(nn.Conv1d(dim, dim, kernel_size=3, padding=1), nn.ReLU())
        self.norm1 = nn.LayerNorm(dim)
        self.dropout = nn.Dropout(dropout)
        self.conv2 = nn.Sequential(nn.Conv1d(dim, dim, kernel_size=3, padding=1), nn.ReLU())
        self.norm2 = nn.LayerNorm(dim)
        self.linear = nn.Linear(dim, 1)
        self.relu = nn.ReLU()
        self.duration = duration

        if pitch_stats is not None:
            pitch_min, pitch_max = pitch_stats
            self.pitch_bins = nn.Parameter(torch.linspace(pitch_min, pitch_max, dim - 1), requires_grad=False,)
            self.pitch_embedding = nn.Embedding(dim, dim)
        else:
            self.pitch_bins = None
            self.pitch_embedding = None

        if energy_stats is not None:
            energy_min, energy_max = energy_stats
            self.energy_bins = nn.Parameter(torch.linspace(energy_min, energy_max, dim - 1), requires_grad=False,)
            self.energy_embedding = nn.Embedding(dim, dim)
        else:
            self.energy_bins = None
            self.energy_embedding = None


    def get_pitch_embedding(self, pred, target, mask, control=1.):
        if target is not None:
            embedding = self.pitch_embedding(torch.bucketize(target, self.pitch_bins))
        else:
            #pred = pred * control
            embedding = self.pitch_embedding(torch.bucketize(pred, self.pitch_bins))
        return embedding

    def get_energy_embedding(self, pred, target, mask, control=1.):
        if target is not None:
            embedding = self.energy_embedding(torch.bucketize(target, self.energy_bins))
        else:
            #pred = pred * control
            embedding = self.energy_embedding(torch.bucketize(pred, self.energy_bins))
        return embedding

    def get_embedding(self, pred, target, mask, control=1.):
        if self.pitch_embedding is not None:
            return self.get_pitch_embedding(pred, target, mask, control)
        elif self.energy_embedding is not None:
            return self.get_energy_embedding(pred, target, mask, control)
        return None

    def forward(self, fused_features, target=None):
        y = rearrange(fused_features, 'b n c -> b c n')
        y = self.conv1(y)
        y = rearrange(y, 'b c n -> b n c')
        y = self.dropout(self.norm1(y))
        y = rearrange(y, 'b n c -> b c n')
        y = self.conv2(y)
        y = rearrange(y, 'b c n -> b n c')
        y = self.dropout(self.norm2(y))
        features = y
        y = self.linear(y)
        if self.duration:
            y = self.relu(y)
            return y, features
        return y


class Fuse(nn.Module):
    """ Fuse Attn Features"""

    def __init__(self, dims, kernel_size=5):
        super().__init__()

        assert(len(dims)>0)

        dim = dims[0]
        self.mlps = nn.ModuleList([])
        for d in dims:
            upsample = d // dim
            self.mlps.append(
                    nn.ModuleList([
                        nn.Linear(d, dim),
                        #nn.Upsample(scale_factor=upsample) if upsample>1 else nn.Identity()
                        nn.ConvTranspose1d(dim, dim, kernel_size=kernel_size, stride=upsample) if upsample>1 else nn.Identity()
                        ]))

        self.fuse = nn.Linear(dim*len(dims), dim)

    def forward(self, features, mask=None):

        fused_features = []
        
        # each feature from encoder block
        for feature, mlps in zip(features, self.mlps):
            mlp, upsample = mlps
            # linear projection to uniform channel size (eg 256)
            x = mlp(feature)
            # upsample operates on the n or seqlen dim
            x = rearrange(x, 'b n c -> b c n')
            # upsample sequence len downsampled by encoder blocks
            x = upsample(x)
            if mask is not None:
                x = x[:,:,:mask.shape[1]]

            fused_features.append(x)
            #print(x.size())

        # cat on the feature dim
        fused_features = torch.cat(fused_features, dim=-2)
        fused_features = rearrange(fused_features, 'b c n -> b n c')

        fused_features = self.fuse(fused_features)
        if mask is not None:
            fused_features = fused_features.masked_fill(mask, 0)

        return fused_features


class FeatureUpsampler(nn.Module):
    """ Upsample fused features using predicted duration """

    def __init__(self):
        super().__init__()

        self.len_regulator = LengthRegulator()

    def forward(self, fused_features, duration, max_mel_len):

        # expand features to max_mel_len dim
        # len_regulator operates on n or sequence len dim
        features, len_pred = self.len_regulator(fused_features, duration, max_mel_len)

        return features, len_pred



class MelPredictor(nn.Module):
    """ Mel Spectrogram Decoder """

    def __init__(self, dims, n_mel_channels=80, dropout=0.1, depth=3, kernel_size=5):
        super().__init__()

        assert(len(dims)>0)

        # arg parameters
        self.n_mel_channels = n_mel_channels
        dim1 = dims[0]
        dim2 = max(2*dim1, 256)
        dim4 = 4*dim1
        padding = kernel_size // 2

        self.fuse = nn.Sequential(nn.Linear(dim4, dim2),
                                  nn.Tanh(), 
                                  nn.LayerNorm(dim2),)

        self.convs1 = nn.ModuleList([])
        for _ in range(depth):
            self.convs1.append(
                    nn.ModuleList([nn.Conv1d(dim2, dim2, kernel_size=kernel_size, padding=padding),
                                   nn.Tanh(),
                                   nn.LayerNorm(dim2),]))

        self.convs2 = nn.ModuleList([])
        for _ in range(depth):
            self.convs2.append(
                    nn.ModuleList([nn.Conv1d(dim2, dim2, kernel_size=kernel_size, padding=padding),
                                   nn.Tanh(),
                                   nn.LayerNorm(dim2),]))

        self.convs3 = nn.ModuleList([])
        for _ in range(depth):
            self.convs3.append(
                    nn.ModuleList([nn.Conv1d(dim2, dim2, kernel_size=kernel_size, padding=padding),
                                nn.Tanh(),
                                nn.LayerNorm(dim2),]))
    
        self.dim2 = dim2
        self.mel_linear = nn.Linear(dim2, self.n_mel_channels)
        self.norm = nn.LayerNorm(dim2)


    def forward(self, features, mask=None):

        skip = self.fuse(features)

        blocks = [self.convs1, self.convs2, self.convs3]
        for convs in blocks:
            mel = rearrange(skip, 'b n c -> b c n') 
            for conv, act, norm in convs:
                x = act(conv(mel))
                x = norm(rearrange(x, 'b c n -> b n c')) # + rearrange(mel, 'b c n -> b n c'))
                mel = rearrange(x, 'b n c -> b c n')

            skip = self.norm(x + skip)

        # resize channel to mel length (eg 80)
        mel = self.mel_linear(skip)

        return mel


class PhonemeEncoder(nn.Module):
    """ Encodes phonemes to acoustic features """

    def __init__(self,
                 pitch_stats=None, 
                 energy_stats=None, 
                 depth=2, 
                 reduction=4, 
                 head=2, 
                 embed_dim=256, 
                 kernel_size=5, 
                 expansion=2):
        super().__init__()

        self.encoder = Encoder(depth=depth,
                               reduction=reduction, 
                               head=head, 
                               embed_dim=embed_dim, 
                               kernel_size=kernel_size, 
                               expansion=expansion)
        dims = self.encoder.get_feature_dims()
        self.fuse = Fuse(dims, kernel_size=kernel_size)
        self.feature_upsampler = FeatureUpsampler()
        self.pitch_decoder = AcousticDecoder(dims, pitch_stats=pitch_stats)
        self.energy_decoder = AcousticDecoder(dims, energy_stats=energy_stats)
        self.duration_decoder = AcousticDecoder(dims, duration=True)
        self.dims = dims

    def forward(self, x, train=True):
        phoneme = x["phoneme"]
        phoneme_mask = x["phoneme_mask"]

        pitch_target = x["pitch"] if train else None
        energy_target = x["energy"] if train  else None
        duration_target = x["duration"] if train  else None
        mel_len = x["mel_len"] if train  else None
        max_mel_len = torch.max(mel_len).item() if train else None

        features, mask = self.encoder(phoneme, mask=phoneme_mask)
        fused_features = self.fuse(features, mask=mask)
        
        pitch_pred = self.pitch_decoder(fused_features)
        pitch_features = self.pitch_decoder.get_embedding(pitch_pred, pitch_target, mask)

        pitch_features = pitch_features.squeeze()
        if mask is not None:
            pitch_features = pitch_features.masked_fill(mask, 0)

        
        energy_pred = self.energy_decoder(fused_features)
        energy_features = self.energy_decoder.get_embedding(energy_pred, energy_target, mask)
        energy_features = energy_features.squeeze()
        if mask is not None:
            energy_features = energy_features.masked_fill(mask, 0)

        duration_pred, duration_features = self.duration_decoder(fused_features)
        if mask is not None:
            duration_features = duration_features.masked_fill(mask, 0)
       
        fused_features = torch.cat([fused_features, pitch_features, energy_features, duration_features], dim=-1)
        
        if duration_target is None:
            duration_target = torch.round(duration_pred).squeeze()
        duration_target = duration_target.masked_fill(phoneme_mask, 0)

        features, mel_len_pred = self.feature_upsampler(fused_features,
                                                    duration=duration_target,
                                                    max_mel_len=max_mel_len)
        y = {"pitch": pitch_pred,
             "energy": energy_pred,
             "duration": duration_pred,
             "mel_len": mel_len_pred,
             "features": features,
             "mask": mask, }

        return y


        
class MelDecoder(nn.Module):
    """ Mel Generator """

    def __init__(self,
                 dims,
                 kernel_size=5):
        super().__init__()
        self.mel_predictor = MelPredictor(dims,
                                          kernel_size=kernel_size)

    def forward(self,
                features, 
                mask): 
                #mel_len=None,
                #max_mel_len=None):

        return self.mel_predictor(features, mask=mask)
        #mel_mask = get_mask_from_lengths(mel_len, max_mel_len) if mel_len is not None else None
        #y = {"mel": mel_pred}
        #if self.training:
        #    y["mel_mask"] = mel_mask
        
        #return y

        
class Phoneme2Mel(nn.Module):
    """ Mel Former """

    def __init__(self,
                 encoder,
                 decoder):
        super().__init__()

        self.encoder = encoder
        self.decoder = decoder

    def forward(self, x, train=True):

        #mel_len = x["mel_len"] if self.training else None
        #max_mel_len = torch.max(mel_len).item() if mel_len is not None else None
    
        pred = self.encoder(x, train=train)
        mel_pred = self.decoder(pred["features"], pred["mask"]) #, mel_len=mel_len, max_mel_len=max_mel_len)
        pred["mel"] = mel_pred

        #if self.training:
        #    pred["mel_mask"] = mel_pred["mel_mask"]
        
        return pred
