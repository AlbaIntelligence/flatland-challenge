import numpy as np
import torch
import torch.nn as nn


class MaskedMSELoss(nn.Module):
    '''
    MSE loss with masked inputs/targets
    '''

    def __init__(self):
        super(MaskedMSELoss, self).__init__()

    def forward(self, input, target, mask):
        diff = ((
            torch.flatten(input) - torch.flatten(target)
        ) ** 2.0) * torch.flatten(mask)
        return torch.sum(diff) / torch.sum(mask)


class Sequential(nn.Sequential):
    '''
    Extension of the PyTorch Sequential module, 
    to handle a variable number of arguments 
    '''

    def forward(self, input, *args, **kwargs):
        for module in self:
            input = module(input, *args, **kwargs)
        return input


def masked_softmax(vec, mask, dim=1, temperature=1):
    '''
    Softmax only on valid outputs
    '''
    assert vec.shape == mask.shape
    assert np.all(mask.astype(bool).any(axis=dim)), mask

    exps = vec.copy()
    exps = np.exp(vec / temperature)
    exps[~mask.astype(bool)] = 0
    return exps / exps.sum(axis=dim, keepdims=True)


def masked_max(vec, mask, dim=1):
    '''
    Max only on valid outputs
    '''
    assert vec.shape == mask.shape
    assert np.all(mask.astype(bool).any(axis=dim)), mask

    res = vec.copy()
    res[~mask.astype(bool)] = np.nan
    return np.nanmax(res, axis=dim, keepdims=True)


def masked_argmax(vec, mask, dim=1):
    '''
    Argmax only on valid outputs
    '''
    assert vec.shape == mask.shape
    assert np.all(mask.astype(bool).any(axis=dim)), mask

    res = vec.copy()
    res[~mask.astype(bool)] = np.nan
    argmax_arr = np.nanargmax(res, axis=dim)

    # Argmax has no keepdims argument
    if dim > 0:
        new_shape = list(res.shape)
        new_shape[dim] = 1
        argmax_arr = argmax_arr.reshape(tuple(new_shape))

    return argmax_arr
