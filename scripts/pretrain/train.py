import argparse
import logging
import math

import datasets
import torch
import torch.distributed as dist
import transformers
from torch.utils.data import DataLoader
from transformers import default_data_collator
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from veturbollm import initialize_veturbollm
from veturbollm.data.datasets import get_train_eval_dataset
from veturbollm.data.sampler import PretrainingRandomSampler, PretrainingSampler, RandomSeedDataset
from veturbollm.global_vars import get_args, get_timers
from veturbollm.models.hf.hf_causal_lm import TurboHFCausalLM
from veturbollm.strategy import prepare_distributed_strategy
from veturbollm.tokenizer import build_tokenizer
from veturbollm.utils import distribution as dist_utils
from veturbollm.utils.log import training_log
from veturbollm.utils.tools import set_seed, print_rank_0
from veturbollm.checkpoint import resume_status, resume_optimizer, save_checkpoint

# The flag below controls whether to allow TF32 on matmul. This flag defaults to False
# in PyTorch 1.12 and later.
torch.backends.cuda.matmul.allow_tf32 = True
# The flag below controls whether to allow TF32 on cuDNN. This flag defaults to True.
torch.backends.cudnn.allow_tf32 = True

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="training a transformers model on a causal language modeling task")
    parser.add_argument(
        "--config_file",
        type=str,
        default=None,
        help="Path to a local config file",
    )
    args = parser.parse_args()
    return args


def main():
    _args = parse_args()

    # Initialize veturbollm
    initialize_veturbollm(_args.config_file)

    # Get global args and timers
    args = get_args()
    timers = get_timers()

    # Resume status from checkpoint
    if args.checkpointing.load:
        resume_status(args)

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )

    if dist_utils.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    set_seed(args.seed)

    tokenizer = build_tokenizer(args)
    train_dataset, eval_dataset = get_train_eval_dataset(tokenizer)

    # DataLoaders creation:
    train_sampler = PretrainingRandomSampler(
        RandomSeedDataset(train_dataset),
        len(train_dataset),
        args.consumed_train_samples,
        args.training.micro_batch_size,
        dist.get_rank(),
        dist.get_world_size(),
        data_sharding=True,
    )
    eval_sampler = PretrainingSampler(
        len(eval_dataset),
        0,
        args.training.micro_batch_size,
        dist.get_rank(),
        dist.get_world_size(),
    )

    train_dataloader = DataLoader(
        train_dataset,
        batch_sampler=train_sampler,
        collate_fn=default_data_collator,
        pin_memory=True,
    )
    eval_dataloader = DataLoader(
        eval_dataset,
        batch_sampler=eval_sampler,
        collate_fn=default_data_collator,
        pin_memory=True,
    )

    # Model
    model = TurboHFCausalLM(tokenizer)

    # Strategy
    model, optimizer, lr_scheduler = prepare_distributed_strategy(model)

    # Resume optimizer and lr_scheduler from checkpoint
    if args.checkpointing.load:
        resume_optimizer(model, optimizer, lr_scheduler, args)

    scaler = torch.cuda.amp.GradScaler(enabled=args.model.enable_native_amp)

    # Afterwards we recalculate our number of training epochs
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.training.gradient_accumulation_steps)
    args.training.train_epochs = math.ceil(args.training.train_iters / num_update_steps_per_epoch)

    # Train!
    print_rank_0("***** Running training *****")
    print_rank_0(f"  Num examples = {len(train_dataset)}")
    print_rank_0(f"  Instantaneous batch size per device = {args.training.micro_batch_size}")
    print_rank_0(
        f"  Total train batch size (w. parallel, distributed & accumulation) = {args.training.global_batch_size}"
    )
    print_rank_0(f"  Gradient Accumulation steps = {args.training.gradient_accumulation_steps}")
    print_rank_0(f"  Total optimization steps = {args.training.train_iters}")

    total_loss_dict = {}

    timers("interval-time", log_level=0).start(barrier=True)
    micro_step = 0
    total_flops = 0
    report_memory_flag = True
    while True:
        for _, batch in enumerate(train_dataloader):
            model.train()
            timers("forward-backward", log_level=1).start()
            outputs = model(batch)
            loss = outputs.loss
            loss_dict = {"lm loss": loss.detach().float()}
            scaler.scale(loss).backward()
            timers("forward-backward").stop()
            micro_step += 1
            total_flops += model.flops_per_batch(batch) / 1e12
            loss_dict["tflops"] = total_flops
            if micro_step % args.training.gradient_accumulation_steps == 0:
                timers("optimizer", log_level=1).start()
                scaler.step(optimizer)
                lr_scheduler.step()
                scaler.update()
                optimizer.zero_grad()
                timers("optimizer").stop()
                args.completed_steps += 1
                args.consumed_train_samples += args.training.global_batch_size

                report_memory_flag = training_log(
                    loss_dict,
                    total_loss_dict,
                    optimizer.param_groups[0]["lr"],
                    args.completed_steps,
                    scaler.get_scale(),
                    report_memory_flag,
                    0,
                )
                total_flops = 0
                if args.checkpointing.save and args.completed_steps % args.checkpointing.save_interval == 0:
                    save_checkpoint(model, optimizer, lr_scheduler, args)
                if args.completed_steps >= args.training.train_iters:
                    break


if __name__ == "__main__":
    main()
