from contextlib import nullcontext

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.testing import assert_close

import colossalai
from colossalai.lazy import LazyInitContext
from colossalai.shardformer.layer import Linear1D_Col, Linear1D_Row
from colossalai.tensor.d_tensor import is_distributed_tensor
from colossalai.testing import parameterize, rerun_if_address_is_in_use, spawn


@parameterize('lazy_init', [False, True])
def check_linear_1d_col(lazy_init: bool):
    ctx = LazyInitContext() if lazy_init else nullcontext()
    linear = nn.Linear(32, 128).cuda()
    with ctx:
        linear_copy = nn.Linear(32, 128).cuda()
    linear_col = Linear1D_Col.from_native_module(linear_copy, process_group=None, gather_output=True)

    # ensure that the parameters are distributed
    assert is_distributed_tensor(linear_col.weight)
    assert is_distributed_tensor(linear_col.bias)
    assert linear_copy.weight is linear_col.weight
    assert linear_copy.bias is linear_col.bias

    # ensure the shape is correct
    assert linear_col.weight.shape == torch.Size([64, 32])
    assert linear_col.bias.shape == torch.Size([64])

    # ensure state dict is reversibly loadable
    linear.load_state_dict(linear_col.state_dict())
    linear_col.load_state_dict(linear.state_dict())

    # check computation correctness
    x = torch.rand(4, 32).cuda()
    x_for_unshard = x.expand_as(x.clone())
    x_for_unshard.requires_grad_(True)
    x_for_shard = x.expand_as(x.clone())
    x_for_shard.requires_grad_(True)

    out = linear(x_for_unshard)
    gather_out = linear_col(x_for_shard)
    assert_close(out, gather_out)

    # check backward correctness
    out.sum().backward()
    gather_out.sum().backward()

    rank = dist.get_rank()
    target_grad = torch.chunk(linear.weight.grad, 2, dim=0)[rank]
    assert_close(target_grad, linear_col.weight.grad)

    # check the input gradients
    assert x_for_shard.grad is not None
    assert x_for_unshard.grad is not None
    assert_close(x_for_unshard.grad, x_for_shard.grad)


@parameterize('lazy_init', [False, True])
def check_linear_1d_row(lazy_init: bool):
    ctx = LazyInitContext() if lazy_init else nullcontext()

    linear = nn.Linear(32, 128).cuda()
    with ctx:
        linear_copy = nn.Linear(32, 128).cuda()
    linear_row = Linear1D_Row.from_native_module(linear_copy, process_group=None, parallel_input=False)

    assert linear_row.weight.shape == torch.Size([128, 16])
    assert linear_row.bias.shape == torch.Size([128])
    assert linear_copy.weight is linear_row.weight
    assert linear_copy.bias is linear_row.bias

    linear.load_state_dict(linear_row.state_dict())
    linear_row.load_state_dict(linear.state_dict())

    # check computation correctness
    x = torch.rand(4, 32).cuda()
    x_for_unshard = x.expand_as(x.clone())
    x_for_unshard.requires_grad_(True)
    x_for_shard = x.expand_as(x.clone())
    x_for_shard.requires_grad_(True)

    # run forward
    out = linear(x_for_unshard)
    gather_out = linear_row(x_for_shard)
    assert_close(out, gather_out)

    # check backward correctness
    out.sum().backward()
    gather_out.sum().backward()

    rank = dist.get_rank()
    target_grad = torch.chunk(linear.weight.grad, 2, dim=1)[rank]
    assert_close(target_grad, linear_row.weight.grad)

    # check the input gradients
    assert x_for_shard.grad is not None
    assert x_for_unshard.grad is not None
    assert_close(x_for_unshard.grad, x_for_shard.grad)


@parameterize('lazy_init', [False, True])
def check_linear_col_plus_row(lazy_init: bool):
    ctx = LazyInitContext() if lazy_init else nullcontext()

    linear_1 = nn.Linear(32, 128).cuda()
    linear_2 = nn.Linear(128, 32).cuda()

    with ctx:
        linear_1_copy = nn.Linear(32, 128).cuda()
        linear_2_copy = nn.Linear(128, 32).cuda()
    linear_col = Linear1D_Col.from_native_module(linear_1_copy, process_group=None, gather_output=False)
    linear_row = Linear1D_Row.from_native_module(linear_2_copy, process_group=None, parallel_input=True)

    linear_1.load_state_dict(linear_col.state_dict())
    linear_col.load_state_dict(linear_1.state_dict())
    linear_2.load_state_dict(linear_row.state_dict())
    linear_row.load_state_dict(linear_2.state_dict())

    # check computation correctness
    x = torch.rand(4, 32).cuda()
    x_for_unshard = x.expand_as(x.clone())
    x_for_unshard.requires_grad_(True)
    x_for_shard = x.expand_as(x.clone())
    x_for_shard.requires_grad_(True)

    # run forward
    unshard_out = linear_2(linear_1(x_for_unshard))
    shard_out = linear_row(linear_col(x_for_shard))
    assert_close(unshard_out, shard_out)

    # check backward correctness
    unshard_out.sum().backward()
    shard_out.sum().backward()

    rank = dist.get_rank()
    target_1_grad = torch.chunk(linear_1.weight.grad, 2, dim=0)[rank]
    assert_close(target_1_grad, linear_col.weight.grad)

    # check the input gradients
    assert x_for_shard.grad is not None
    assert x_for_unshard.grad is not None
    assert_close(x_for_unshard.grad, x_for_shard.grad)


def run_dist(rank, world_size, port):
    colossalai.launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    check_linear_1d_col()
    check_linear_1d_row()
    check_linear_col_plus_row()


@rerun_if_address_is_in_use()
def test_linear():
    spawn(run_dist, nprocs=2)


if __name__ == '__main__':
    test_linear()
