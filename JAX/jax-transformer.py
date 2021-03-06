from jax.experimental import stax, minmax
import dataset_util
import functools
import jax.numpy as np
import numpy as onp
import numpy.random as npr
import re
import os
import sys
import jax
import time



def create_root_context():
    return VariableContext({}, '')

class VariableContext(object):
    def __init__(self, name2val, prefix, allow_new=True):
        self.name2val = name2val
        self.prefix = prefix
        self.allow_new = allow_new
    def scope(self, name):
        return VariableContext(self.name2val, 
            self._join(self.prefix, name), self.allow_new)
    def get_variable(self, name, initializer):
        return self.get_variable_absolute(
            name=self._join(self.prefix, name), 
            initializer=initializer)
    def get_variable_absolute(self, name, initializer):
        if name not in self.name2val:
            assert self.allow_new
            val = initializer()
            assert type(val) == onp.ndarray and val.dtype == onp.float32
            self.name2val[name] = val

        return self.name2val[name]
    def _join(self, *xs):
        return '/'.join(xs)
    def variables_list(self):
        return list(self.name2val.values())
    def replace_with_list(self, newlist):
        assert len(newlist) == len(self.name2val)
        name2val = {k : v for (k, v) in zip(self.name2val.keys(), newlist)}
        return VariableContext(name2val, self.prefix, self.allow_new)

def print_variables(cx):
    for (name, val) in sorted(cx.name2val.items()):
        print(f'{name:20s} {str(val.shape):20s} {str(val.dtype):20s}')

def normax(shape, axis):
    out = npr.randn(*shape).astype(np.float32)
    out /= onp.sqrt(onp.square(out).sum(axis=axis, keepdims=True))
    return out

def normc(*shape):
    return normax(shape, axis=0)

def randn(shape, stddev):
    return npr.randn(*shape).astype(np.float32) * stddev

def gelu(x):
    return 0.5*x*(1+np.tanh(0.79788*(x+0.044715*x**3)))

def _norm(x, *, axis, g=None, b=None, e=1e-5):
    u = np.mean(x, axis=axis, keepdims=True)
    s = np.mean(np.square(x-u), axis=axis, keepdims=True)
    x = (x - u) / np.sqrt(s + e)
    if g is not None and b is not None:
        x = x * g + b
    return x

def norm(cx, x, axis=-1):
    n_state = x.shape[axis]
    g = cx.get_variable("g", initializer=lambda : onp.ones(n_state, 'f'))
    b = cx.get_variable("b", initializer=lambda : onp.zeros(n_state, 'f'))
    return _norm(x, g=g, b=b, axis=axis)

def mask_attn_weights(w):
    n = w.shape[-1]
    b = np.tril(np.ones((n,n)))
    b = np.reshape(b, (1, 1, n, n))
    w = w * b - 1e9 * (1 - b)
    return w

def _attn(Q_bhtr, K_bhrt, V_bhtr):
    R = Q_bhtr.shape[-1]
    W_bhtt = np.matmul(Q_bhtr, K_bhrt) / np.sqrt(R)
    W_bhtt = mask_attn_weights(W_bhtt)
    W_bhtt = stax.softmax(W_bhtt, axis=-1)
    A_bhtr = np.matmul(W_bhtt, V_bhtr)
    return A_bhtr

def dense(cx, X_btk, F):
    B, T, K = X_btk.shape
    X_bt_k = np.reshape(X_btk, (-1, K))
    W_kf = cx.get_variable("w", initializer=lambda : normc(K, F))
    b_f = cx.get_variable("b", initializer=lambda : onp.zeros(F,'f'))
    Y_bt_f = np.matmul(X_bt_k, W_kf) + b_f
    return np.reshape(Y_bt_f, (B, T, F))

def attn(cx, X_btk, n_state, n_head):
    B, T, _K = X_btk.shape
    assert n_state % n_head==0
    QKV_b_t_3s = dense(cx.scope('c_attn'), X_btk, n_state * 3)
    QKV_b_t_3h_r = np.reshape(QKV_b_t_3s, (B, T, 3 * n_head, n_state // n_head))
    Q_bthr, K_bthr, V_bthr = np.split(QKV_b_t_3h_r, 3, axis=2)
    Q_bhtr = np.transpose(Q_bthr, (0, 2, 1, 3))
    V_bhtr = np.transpose(V_bthr, (0, 2, 1, 3))
    K_bhrt = np.transpose(K_bthr, (0, 2, 3, 1))
    A_bhtr = _attn(Q_bhtr, K_bhrt, V_bhtr)
    A_bthr = np.transpose(A_bhtr, (0, 2, 1, 3))
    A_bts = np.reshape(A_bthr, (B, T, n_state))
    P_bts = dense(cx.scope('c_proj'), A_bts, n_state)
    return P_bts

def mlp(cx, X_bts, *, n_hid):
    S = X_bts.shape[-1]
    H_bth = stax.relu(dense(cx.scope('c_fc'), X_bts, n_hid))
    Y_bts = dense(cx.scope('c_proj'), H_bth, S)
    return Y_bts

def block(cx, X_bts, *, n_head):
    _B, _T, S = X_bts.shape
    A_bts = attn(cx.scope('attn'), X_bts, S, n_head)
    N_bts = norm(cx.scope('ln_1'), X_bts + A_bts, axis=-1)
    M_bts = mlp(cx.scope('mlp'), N_bts, n_hid=S * 4)
    Y_bts = norm(cx.scope('ln_2'), N_bts + M_bts, axis=-1)
    return Y_bts

def transformer(cx, tok_bt, *, n_vocab, n_head, n_layer, n_ctx, n_embd):
    B, T = tok_bt.shape
    pos_bt = jax.lax.broadcasted_iota(np.int32, (B, T), 1)
    tokenembs_qe = cx.get_variable('tokenembs', 
        initializer=lambda : normc(n_vocab, n_embd) * 0.1)
    posembs_pe = cx.get_variable('posembs', 
        initializer=lambda : normc(n_ctx, n_embd) * 0.1)
    tokenemb_bte = tokenembs_qe[tok_bt]
    assert isinstance(tok_bt, np.ndarray)
    posemb_bte = posembs_pe[pos_bt]
    last_bts = tokenemb_bte + posemb_bte
    for layer in range(n_layer):
        last_bts = block(cx.scope(f'h{layer:02d}'), last_bts, n_head=n_head)
    logits_btq = np.matmul(last_bts, tokenembs_qe.T)
    logprobs_btq = stax.logsoftmax(logits_btq)    
    return logprobs_btq

def train_test_split(codebook, text, n_ctx):

    flatdata = onp.array([codebook.token2idx(token) for token in text])
    splits = [mo.end() for mo in re.finditer(r'\n\n|\. |; |: ',text)]
    starts = onp.concatenate([[0], splits])
    teststart = starts[int(len(starts) * 0.75)]
    chunksize = n_ctx + 1
    starts_train = starts[starts + chunksize <= teststart]
    starts_test = starts[starts + chunksize <= len(flatdata)]
    return (onp.array([flatdata[s : s+chunksize] for s in starts_train]),
        onp.array([flatdata[s : s+chunksize] for s in starts_test]))

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('text_file')
    parser.add_argument('--load_model')
    args = parser.parse_args()
    text, codebook = dataset_util.process_dataset(args.text_file)
    npr.seed(0)
    n_ctx = 64
    batch_size = 64
    n_head = 4
    n_layer = 4
    n_embd = 128
    model = functools.partial(transformer, n_vocab=codebook.size,
        n_head=n_head, n_layer=n_layer, n_ctx=n_ctx, n_embd=n_embd)

    Xtr_bt, Xte_bt = train_test_split(codebook, text, n_ctx)
    root_cx = create_root_context()

    def loss(cx, XY_bt):
        X_bt = XY_bt[:, :-1]
        B, T = X_bt.shape
        Y_bt = XY_bt[:, 1:]
        logprobs_btq = model(cx, X_bt)
        loglosses_bt = - logprobs_btq.reshape((B*T, -1))[
            np.arange(B*T), Y_bt.reshape((-1,))]
        return loglosses_bt.mean()
    def loss2(params, XY_bt):
        cx = root_cx.replace_with_list(params)
        return loss(cx, XY_bt)

    loss(root_cx, Xtr_bt[:batch_size]) # Just create variables
    root_cx.allow_new = False
    print_variables(root_cx)
    init_params = root_cx.variables_list()

    opt_init, opt_update = minmax.adam(step_size=3e-4)
    opt_state = opt_init(init_params)

    @jax.jit
    def update(i, opt_state, batch):
        XY, = batch
        params = minmax.get_params(opt_state)
        v, g = jax.value_and_grad(loss2)(params, XY)
        return v, opt_update(i, g, opt_state)

    for epoch in range(1000):
        print('Epoch', epoch)
        for XY in dataset_util.iterbatches(Xtr_bt, batch_size=batch_size, 
                include_final_partial_batch=False):
            tstart = time.time()
            lossval, opt_state = update(0, opt_state, XY)
            print(f'loss={lossval:8.3f} dur={time.time()-tstart:8.3f}')

if __name__ == '__main__':
    main()
