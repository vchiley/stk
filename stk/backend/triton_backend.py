import triton_kernels as backend
import torch


def _standardize_shape(x, transpose):
    if transpose:
        return torch.Size((x[1], x[0]))
    return x


def _sparse_transpose(x):
    return (torch.Size((x[0][1], x[0][0])), ) + x[1:]


def _transpose_helper(x, transpose):
    if isinstance(x, torch.Tensor):
        return x.t() if transpose else x
    if transpose:
        x = _sparse_transpose(x)
    return x + (transpose,)


def _wrap(x):
    if isinstance(x, torch.Tensor):
        return (x,)
    return x


def _is_transposed(x):
    return (not x.is_contiguous() and
            x.stride()[0] == 1 and
            x.stride()[1] == x.size()[0])


def _call_helper(op, out, a, b, trans_a, trans_b):
    args = (_wrap(_transpose_helper(a, trans_a)) +
            _wrap(_transpose_helper(b, trans_b)))
    if isinstance(out, tuple):
        args = args + out
    return op(*args)


def _preprocess_inputs(lhs, rhs, dy):
    if isinstance(lhs, torch.Tensor) and _is_transposed(lhs):
        lhs = lhs.t()
    if isinstance(rhs, torch.Tensor) and _is_transposed(rhs):
        rhs = rhs.t()
    if (isinstance(dy, torch.Tensor) and
        not dy.is_contiguous() and
        not _is_transposed(dy)):
        dy = dy.contiguous()
    if isinstance(dy, tuple) and not dy[1].is_contiguous():
        dy = (dy[0], dy[1].contiguous()) + dy[2:]
    return lhs, rhs, dy


def _postprocess_outputs(x, transpose, grad):
    if isinstance(x, torch.Tensor) and transpose:
        return grad.t()
    return grad


def _lhs_gradient(op, lhs, rhs, dy, trans_lhs, trans_rhs):
    lhs, rhs, dy = _preprocess_inputs(lhs, rhs, dy)

    a, b = (rhs, dy) if trans_lhs else (dy, rhs)
    trans_a = trans_lhs and trans_rhs
    trans_b = trans_lhs or not trans_rhs
    out = _call_helper(op, lhs, a, b, trans_a, trans_b)
    return _postprocess_outputs(lhs, trans_lhs, out)


def _rhs_gradient(op, lhs, rhs, dy, trans_lhs, trans_rhs):
    lhs, rhs, dy = _preprocess_inputs(lhs, rhs, dy)

    a, b = (dy, lhs) if trans_rhs else (lhs, dy)
    trans_a = not trans_lhs or trans_rhs
    trans_b = trans_lhs and trans_rhs
    out = _call_helper(op, rhs, a, b, trans_a, trans_b)
    return _postprocess_outputs(rhs, trans_rhs, out)


class DSD(torch.autograd.Function):

    @staticmethod
    def forward(ctx,
                shape,
                data,
                offsets,
                row_indices,
                column_indices,
                offsets_t,
                column_indices_t,
                block_offsets_t,
                transpose_a,
                rhs):
        ctx.save_for_backward(data,
                              offsets,
                              row_indices,
                              column_indices,
                              offsets_t,
                              column_indices_t,
                              block_offsets_t,
                              rhs)
        ctx.shape = _standardize_shape(shape, transpose_a)
        ctx.transpose_a = transpose_a

        out = torch.empty(
            (shape[0], rhs.size()[1]),
            dtype=rhs.dtype,
            device=rhs.device)

        backend.dsd(shape,
                    data,
                    offsets,
                    row_indices,
                    column_indices,
                    offsets_t,
                    column_indices_t,
                    block_offsets_t,
                    transpose_a,
                    rhs,
                    out)
        return out

    @staticmethod
    def backward(ctx, dy):
        lhs = (ctx.shape,) + ctx.saved_tensors[:-1]
        rhs = ctx.saved_tensors[-1]
        trans_a = ctx.transpose_a
        trans_b = _is_transposed(rhs)

        ddata = None
        if ctx.needs_input_grad[1]:
            ddata = _lhs_gradient(sdd,
                                  lhs,
                                  rhs,
                                  dy,
                                  trans_a,
                                  trans_b)
        drhs = None
        if ctx.needs_input_grad[-1]:
            op = dds if trans_b else dsd
            drhs = _rhs_gradient(op,
                                 lhs,
                                 rhs,
                                 dy,
                                 trans_a,
                                 trans_b)
        return None, ddata, None, None, None, None, None, None, None, drhs


dsd = DSD.apply


class DDS(torch.autograd.Function):

    @staticmethod
    def forward(ctx,
                lhs,
                shape,
                data,
                offsets,
                row_indices,
                column_indices,
                offsets_t,
                column_indices_t,
                block_offsets_t,
                transpose_b):
        ctx.save_for_backward(lhs,
                              data,
                              offsets,
                              row_indices,
                              column_indices,
                              offsets_t,
                              column_indices_t,
                              block_offsets_t)
        ctx.shape = _standardize_shape(shape, transpose_b)
        ctx.transpose_b = transpose_b
        out = torch.empty((lhs.size()[0], shape[1]),
                          dtype=lhs.dtype,
                          device=lhs.device)
        backend.dds(lhs,
                    shape,
                    data,
                    offsets,
                    row_indices,
                    column_indices,
                    offsets_t,
                    column_indices_t,
                    block_offsets_t,
                    transpose_b,
                    out)
        return out

    @staticmethod
    def backward(ctx, dy):
        lhs = ctx.saved_tensors[0]
        rhs = (ctx.shape,) + ctx.saved_tensors[1:]
        trans_a = _is_transposed(lhs)
        trans_b = ctx.transpose_b

        dlhs = None
        if ctx.needs_input_grad[0]:
            op = dsd if trans_a else dds
            dlhs = _lhs_gradient(op,
                                 lhs,
                                 rhs,
                                 dy,
                                 trans_a,
                                 trans_b)
        ddata = None
        if ctx.needs_input_grad[2]:
            ddata = _rhs_gradient(sdd,
                                  lhs,
                                  rhs,
                                  dy,
                                  trans_a,
                                  trans_b)
        return dlhs, None, ddata, None, None, None, None, None, None, None


dds = DDS.apply


class SDD(torch.autograd.Function):

    @staticmethod
    def forward(ctx,
                lhs,
                rhs,
                shape,
                data,
                offsets,
                row_indices,
                column_indices,
                offsets_t,
                column_indices_t,
                block_offsets_t):
        ctx.save_for_backward(
            lhs,
            rhs,
            data,
            offsets,
            row_indices,
            column_indices,
            offsets_t,
            column_indices_t,
            block_offsets_t)
        ctx.shape = shape
        out = torch.empty(
            data.shape,
            dtype=data.dtype,
            device=data.device)
        backend.sdd(lhs,
                    rhs,
                    shape,
                    out,
                    offsets,
                    row_indices,
                    column_indices)
        return out

    @staticmethod
    def backward(ctx, dy):
        lhs, rhs = ctx.saved_tensors[:2]
        dy = (ctx.shape, dy) + ctx.saved_tensors[3:]
        trans_a = _is_transposed(lhs)
        trans_b = _is_transposed(rhs)

        dlhs = None
        if ctx.needs_input_grad[0]:
            op = dds if trans_a else dsd
            dlhs = _lhs_gradient(op,
                                 lhs,
                                 rhs,
                                 dy,
                                 trans_a,
                                 trans_b)
        drhs = None
        if ctx.needs_input_grad[1]:
            op = dsd if trans_b else dds
            drhs = _rhs_gradient(op,
                                 lhs,
                                 rhs,
                                 dy,
                                 trans_a,
                                 trans_b)
        return dlhs, drhs, None, None, None, None, None, None, None, None

sdd = SDD.apply