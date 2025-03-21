import argparse
import builtins
import json
import multiprocessing as mp
import os, sys
import random
import socket
import traceback

import fairscale.nn.model_parallel.initialize as fs_init
import gradio as gr
import numpy as np
import torch
import torch.distributed as dist
from torchvision.transforms.functional import to_pil_image

import models
from PIL import Image
from transport import create_transport, Sampler


class ModelFailure:
    pass


# Adapted from pipelines.StableDiffusionXLPipeline.encode_prompt
def encode_prompt(
    prompt_batch, text_encoder, tokenizer, proportion_empty_prompts, is_train=True
):

    captions = []
    for caption in prompt_batch:
        if random.random() < proportion_empty_prompts:
            captions.append("")
        elif isinstance(caption, str):
            captions.append(caption)
        elif isinstance(caption, (list, np.ndarray)):
            # take a random caption if there are multiple
            captions.append(random.choice(caption) if is_train else caption[0])

    with torch.no_grad():
        text_inputs = tokenizer(
            captions,
            padding=True,
            pad_to_multiple_of=8,
            max_length=256,
            truncation=True,
            return_tensors="pt",
        )

        text_input_ids = text_inputs.input_ids
        prompt_masks = text_inputs.attention_mask

        prompt_embeds = text_encoder(
            input_ids=text_input_ids.cuda(),
            attention_mask=prompt_masks.cuda(),
            output_hidden_states=True,
        ).hidden_states[-2]

    return prompt_embeds, prompt_masks


@torch.no_grad()
def model_main(args, master_port, rank, request_queue, response_queue, mp_barrier):
    # import here to avoid huggingface Tokenizer parallelism warnings
    from diffusers.models import AutoencoderKL
    from transformers import AutoModelForCausalLM, AutoTokenizer

    # override the default print function since the delay can be large for child process
    original_print = builtins.print

    # Redefine the print function with flush=True by default
    def print(*args, **kwargs):
        kwargs.setdefault("flush", True)
        original_print(*args, **kwargs)

    # Override the built-in print with the new version
    builtins.print = print

    os.environ["MASTER_PORT"] = str(master_port)
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["RANK"] = str(rank)
    os.environ["WORLD_SIZE"] = str(args.num_gpus)

    dist.init_process_group("nccl")
    # set up fairscale environment because some methods of the Lumina model need it,
    # though for single-GPU inference fairscale actually has no effect
    fs_init.initialize_model_parallel(args.num_gpus)
    torch.cuda.set_device(rank)

    train_args = torch.load(os.path.join(args.ckpt, "model_args.pth"))
    if dist.get_rank() == 0:
        print("Loaded model arguments:", json.dumps(train_args.__dict__, indent=2))

    if dist.get_rank() == 0:
        print(f"Creating lm: LLaMA2-7B")

    dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[
        args.precision
    ]

    text_encoder = (
        AutoModelForCausalLM.from_pretrained(
            "/data/xjl/checkpoints/llama2", torch_dtype=dtype, device_map="cuda"
        )
        .get_decoder()
        .eval()
    )
    cap_feat_dim = text_encoder.config.hidden_size
    if args.num_gpus > 1:
        raise NotImplementedError("Inference with >1 GPUs not yet supported")

    tokenizer = AutoTokenizer.from_pretrained(
        "/data/xjl/checkpoints/llama2", add_bos_token=True, add_eos_token=True
    )
    tokenizer.padding_side = "right"

    if dist.get_rank() == 0:
        print(f"Creating vae: {train_args.vae}")
    vae = AutoencoderKL.from_pretrained(
        "/data/xjl/checkpoints/vae",
        # f"stabilityai/sd-vae-ft-{train_args.vae}"
        # if train_args.vae != "sdxl"
        # else "stabilityai/sdxl-vae",
        torch_dtype=torch.float32,
    ).cuda()

    if dist.get_rank() == 0:
        print(f"Creating DiT: {train_args.model}")
    # latent_size = train_args.image_size // 8
    model = models.__dict__[train_args.model](
        qk_norm=train_args.qk_norm,
        cap_feat_dim=cap_feat_dim,
    )
    model.eval().to("cuda", dtype=dtype)

    assert train_args.model_parallel_size == args.num_gpus
    if args.ema:
        print("Loading ema model.")
    ckpt = torch.load(
        os.path.join(
            args.ckpt,
            f"consolidated{'_ema' if args.ema else ''}.{rank:02d}-of-{args.num_gpus:02d}.pth",
        ),
        map_location="cpu",
    )
    model.load_state_dict(ckpt, strict=True)

    mp_barrier.wait()

    with torch.autocast("cuda", dtype):
        while True:
            (
                cap,
                resolution,
                num_sampling_steps,
                cfg_scale,
                solver,
                t_shift,
                seed,
                ntk_scaling,
                proportional_attn,
            ) = request_queue.get()

            print(
                "> params:",
                cap,
                resolution,
                num_sampling_steps,
                cfg_scale,
                solver,
                t_shift,
                seed,
                ntk_scaling,
                proportional_attn,
            )
            try:
                # begin sampler
                transport = create_transport(
                    args.path_type,
                    args.prediction,
                    args.loss_weight,
                    args.train_eps,
                    args.sample_eps,
                )
                sampler = Sampler(transport)
                if args.sampler_mode == "ODE":
                    if args.likelihood:
                        # assert args.cfg_scale == 1, "Likelihood is incompatible with guidance"  # todo
                        sample_fn = sampler.sample_ode_likelihood(
                            sampling_method=solver,
                            num_steps=num_sampling_steps,
                            atol=args.atol,
                            rtol=args.rtol,
                        )
                    else:
                        print("go as expected")
                        sample_fn = sampler.sample_ode(
                            sampling_method=solver,
                            num_steps=num_sampling_steps,
                            atol=args.atol,
                            rtol=args.rtol,
                            reverse=args.reverse,
                            time_shifting_factor=t_shift,
                        )
                elif args.sampler_mode == "SDE":
                    sample_fn = sampler.sample_sde(
                        sampling_method=solver,
                        diffusion_form=args.diffusion_form,
                        diffusion_norm=args.diffusion_norm,
                        last_step=args.last_step,
                        last_step_size=args.last_step_size,
                        num_steps=num_sampling_steps,
                    )
                # end sampler

                resolution = resolution.split(" ")[-1]
                w, h = resolution.split("x")
                w, h = int(w), int(h)
                latent_w, latent_h = w // 8, h // 8
                if int(seed) != 0:
                    torch.random.manual_seed(int(seed))
                z = torch.randn([1, 4, latent_h, latent_w], device="cuda").to(dtype)
                z = z.repeat(2, 1, 1, 1)

                # cap_tok = tokenizer.encode(cap, truncation=False)
                # null_cap_tok = tokenizer.encode("", truncation=False)
                # tok = torch.zeros([2, max(len(cap_tok), len(null_cap_tok))], dtype=torch.long, device="cuda")
                # tok_mask = torch.zeros_like(tok, dtype=torch.bool)
                # tok[0, :len(cap_tok)] = torch.tensor(cap_tok)
                # tok[1, :len(null_cap_tok)] = torch.tensor(null_cap_tok)
                # tok_mask[0, :len(cap_tok)] = True
                # tok_mask[1, :len(null_cap_tok)] = True
                with torch.no_grad():
                    cap_feats, cap_mask = encode_prompt(
                        [cap] + [""], text_encoder, tokenizer, 0.0
                    )
                cap_mask = cap_mask.to(cap_feats.device)

                # cap_feats = model_lm.get_decoder()(input_ids=tok).last_hidden_state

                model_kwargs = dict(
                    cap_feats=cap_feats,
                    cap_mask=cap_mask,
                    cfg_scale=cfg_scale,
                )
                if proportional_attn:
                    model_kwargs["proportional_attn"] = True
                    model_kwargs["base_seqlen"] = (train_args.image_size // 16) ** 2 + (
                        train_args.image_size // 16
                    ) * 2
                if ntk_scaling:
                    model_kwargs["ntk_factor"] = ((w // 16) * (h // 16)) / (
                        (train_args.image_size // 16) ** 2
                    )

                if dist.get_rank() == 0:
                    print(f"caption: {cap}")
                    print(f"num_sampling_steps: {num_sampling_steps}")
                    print(f"cfg_scale: {cfg_scale}")

                with torch.cuda.amp.autocast(dtype=torch.bfloat16):
                    print("> [debug] start sample")
                    samples = sample_fn(z, model.forward_with_cfg, **model_kwargs)[-1]
                samples = samples[:1]

                factor = 0.18215 if train_args.vae != "sdxl" else 0.13025
                print(f"vae factor: {factor}")
                samples = vae.decode(samples / factor).sample
                samples = (samples + 1.0) / 2.0
                samples.clamp_(0.0, 1.0)
                img = to_pil_image(samples[0].float())

                if response_queue is not None:
                    response_queue.put(img)

            except Exception:
                print(traceback.format_exc())
                response_queue.put(ModelFailure())


def none_or_str(value):
    if value == "None":
        return None
    return value


def parse_transport_args(parser):
    group = parser.add_argument_group("Transport arguments")
    group.add_argument(
        "--path-type",
        type=str,
        default="Linear",
        choices=["Linear", "GVP", "VP"],
        help="the type of path for transport: 'Linear', 'GVP' (Geodesic Vector Pursuit), or 'VP' (Vector Pursuit).",
    )
    group.add_argument(
        "--prediction",
        type=str,
        default="velocity",
        choices=["velocity", "score", "noise"],
        help="the prediction model for the transport dynamics.",
    )
    group.add_argument(
        "--loss-weight",
        type=none_or_str,
        default=None,
        choices=[None, "velocity", "likelihood"],
        help="the weighting of different components in the loss function, can be 'velocity' for dynamic modeling, 'likelihood' for statistical consistency, or None for no weighting.",
    )
    group.add_argument(
        "--sample-eps", type=float, help="sampling in the transport model."
    )
    group.add_argument(
        "--train-eps", type=float, help="training to stabilize the learning process."
    )


def parse_ode_args(parser):
    group = parser.add_argument_group("ODE arguments")
    group.add_argument(
        "--atol",
        type=float,
        default=1e-6,
        help="Absolute tolerance for the ODE solver.",
    )
    group.add_argument(
        "--rtol",
        type=float,
        default=1e-3,
        help="Relative tolerance for the ODE solver.",
    )
    group.add_argument(
        "--reverse", action="store_true", help="run the ODE solver in reverse."
    )
    group.add_argument(
        "--likelihood",
        action="store_true",
        help="Enable calculation of likelihood during the ODE solving process.",
    )


def parse_sde_args(parser):
    group = parser.add_argument_group("SDE arguments")
    group.add_argument(
        "--sampling-method",
        type=str,
        default="Euler",
        choices=["Euler", "Heun"],
        help="the numerical method used for sampling the stochastic differential equation: 'Euler' for simplicity or 'Heun' for improved accuracy.",
    )
    group.add_argument(
        "--diffusion-form",
        type=str,
        default="sigma",
        choices=[
            "constant",
            "SBDM",
            "sigma",
            "linear",
            "decreasing",
            "increasing-decreasing",
        ],
        help="form of diffusion coefficient in the SDE",
    )
    group.add_argument(
        "--diffusion-norm",
        type=float,
        default=1.0,
        help="Normalizes the diffusion coefficient, affecting the scale of the stochastic component.",
    )
    group.add_argument(
        "--last-step",
        type=none_or_str,
        default="Mean",
        choices=[None, "Mean", "Tweedie", "Euler"],
        help="form of last step taken in the SDE",
    )
    group.add_argument(
        "--last-step-size", type=float, default=0.04, help="size of the last step taken"
    )


def find_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main():
    parser = argparse.ArgumentParser()
    if len(sys.argv) < 2:
        print("Usage: program.py <mode> [options]")
        sys.exit(1)
    mode = sys.argv[1]

    assert mode[:2] != "--", "Usage: program.py <mode> [options]"
    assert mode in ["ODE", "SDE"], "Invalid mode. Please choose 'ODE' or 'SDE'"

    parser.add_argument("--num_gpus", type=int, default=1)
    parser.add_argument("--ckpt", type=str, required=True)
    parser.add_argument("--ema", action="store_true")
    parser.add_argument("--precision", default="bf16", choices=["bf16", "fp32"])

    parse_transport_args(parser)
    if mode == "ODE":
        parse_ode_args(parser)
        # Further processing for ODE
    elif mode == "SDE":
        parse_sde_args(parser)
        # Further processing for SDE

    args = parser.parse_known_args()[0]

    if args.num_gpus != 1:
        raise NotImplementedError("Multi-GPU Inference is not yet supported")
    args.sampler_mode = mode

    master_port = find_free_port()

    processes = []
    request_queues = []
    response_queue = mp.Queue()
    mp_barrier = mp.Barrier(args.num_gpus + 1)
    for i in range(args.num_gpus):
        request_queues.append(mp.Queue())
        p = mp.Process(
            target=model_main,
            args=(
                args,
                master_port,
                i,
                request_queues[i],
                response_queue if i == 0 else None,
                mp_barrier,
            ),
        )
        p.start()
        processes.append(p)

    description = """
            # Lumina-T2I Image Generation Demo

            Lumina-T2I is a 5B Flag-DiT model with LLaMA2-7B text encoder.

            Demo current model: `Lumina-T2I 5B`

            ### <span style='color: red;'>Due to the high volume of access, we have temporarily disabled the resolution extrapolation functionality.
            
            ### <span style='color: red;'>We also offer the Lumina-T2I 5B model, which provides different generation effects and styles.

            ### Additionally, we offer two alternative links for Lumina-T2X access. Try to visit other demo sites. 
            
            Lumina-T2I 5B model: [[demo for 5B model](http://106.14.2.150:10022/)] 
            Luimna-Next-T2I 2B model: [[demo for 2B model](http://106.14.2.150:10023/)]
"""
    with gr.Blocks() as demo:
        with gr.Row():
            gr.Markdown(description)
        with gr.Row():
            with gr.Column():
                cap = gr.Textbox(
                    lines=2,
                    label="Caption",
                    interactive=True,
                    value="Miss Mexico portrait of the most beautiful mexican woman, Exquisite detail, 30-megapixel, 4k, 85-mm-lens, sharp-focus, f:8, "
                    "ISO 100, shutter-speed 1:125, diffuse-back-lighting, award-winning photograph, small-catchlight, High-sharpness, facial-symmetry, 8k --q 2 --ar 18:32 --v 5",
                )
                with gr.Row():
                    res_choices = ["1024x1024", "512x2048", "2048x512"] + [
                        "(Extrapolation) 1664x1664",
                        "(Extrapolation) 1024x2048",
                        "(Extrapolation) 2048x1024",
                    ]
                    res_choices = ["1024x1024"]
                    resolution = gr.Dropdown(
                        value=res_choices[0], choices=res_choices, label="Resolution"
                    )
                with gr.Row():
                    num_sampling_steps = gr.Slider(
                        minimum=1,
                        maximum=70,
                        value=30,
                        interactive=True,
                        label="Sampling steps",
                    )
                    seed = gr.Slider(
                        minimum=0,
                        maximum=int(1e5),
                        value=1,
                        step=1,
                        interactive=True,
                        label="Seed (0 for random)",
                    )
                with gr.Accordion(
                    "Advanced Settings for Resolution Extrapolation", open=False
                ):
                    with gr.Row():
                        solver = gr.Dropdown(
                            value="euler",
                            choices=["euler"],
                            # choices=["euler", "dopri5", "dopri8"],
                            label="solver",
                        )
                        t_shift = gr.Slider(
                            minimum=1,
                            maximum=20,
                            value=6,
                            step=1,
                            interactive=True,
                            label="Time shift",
                        )
                        cfg_scale = gr.Slider(
                            minimum=1.0,
                            maximum=20.0,
                            value=4.0,
                            interactive=True,
                            label="CFG scale",
                        )
                    with gr.Row():
                        ntk_scaling = gr.Checkbox(
                            value=True,
                            interactive=True,
                            label="ntk scaling",
                        )
                        proportional_attn = gr.Checkbox(
                            value=True,
                            interactive=True,
                            label="Proportional attention",
                        )
                with gr.Row():
                    submit_btn = gr.Button("Submit", variant="primary")
                    # reset_btn = gr.ClearButton([
                    #     cap, resolution,
                    #     num_sampling_steps, cfg_scale, solver,
                    #     t_shift, seed,
                    #     ntk_scaling, proportional_attn
                    # ])
            with gr.Column():
                default_img = Image.open("./image.png")
                output_img = gr.Image(
                    label="Generated image",
                    interactive=False,
                    format="png",
                    value=default_img,
                )

        with gr.Row():
            gr.Examples(
                [
                    [
                        "A fluffy mouse holding a watermelon, in a magical and colorful setting, illustrated in the style of Hayao Miyazaki anime by Studio Ghibli."
                    ],
                    # noqa
                    ["a man with facial expression of  🤯"],
                    [
                        "Tour de Tokyo, estampes ukiyo-e de la plus haute qualité, période Edo"
                    ],
                    ["東京タワー、最高品質の浮世絵、江戸時代。"],
                    ["A humanoid eagle soldier of the First World War."],  # noqa
                    [
                        "A cute Christmas mockup on an old wooden industrial desk table with Christmas decorations and bokeh lights in the background."
                    ],
                    # noqa
                    ["a man with facial expression of  😊"],
                    [
                        "A scared cute rabbit in Happy Tree Friends style and punk vibe."
                    ],  # noqa
                    [
                        "Tour de Tokyo, estampes ukiyo-e de la plus haute qualité, période Edo"
                    ],
                    ["味噌ラーメン, 最高品質の浮世絵、江戸時代。"],
                    [
                        "A front view of a romantic flower shop in France filled with various blooming flowers including lavenders and roses."
                    ],
                    # noqa
                    [
                        "An old man, portrayed as a retro superhero, stands in the streets of New York City at night"
                    ],
                    # noqa
                    [
                        "Inka warrior with a war make up, medium shot, natural light, Award winning wildlife photography, hyperrealistic, 8k resolution, --ar 9:16"
                    ],
                    [
                        "many trees are surrounded by a lake in autumn colors, in the style of nature-inspired imagery, havencore, brightly colored, dark white and dark orange, bright primary colors, environmental activism, forestpunk --ar 64:51"
                    ],
                    [
                        "Character of lion in style of saiyan, mafia, gangsta, citylights background, Hyper detailed, hyper realistic, unreal engine ue5, cgi 3d, cinematic shot, 8k"
                    ],
                    ["wise owl, paint splash art"],
                    [
                        "Cyberpunk eagle, neon ambiance, abstract black oil, gear mecha, detailed acrylic, grunge, intricate complexity, rendered in unreal engine 5, photorealistic, 8k"
                    ],
                    ["Astronaut on Mars During sunset"],
                    [
                        "summer landscape, vivid colors, a work of art, grotesque, Mysterious"
                    ],
                    [
                        "Dobby the house elf with wide, pleading eyes, holding a sock, signaling his freedom"
                    ],
                    [
                        "close-up photo of a beautiful red rose breaking through a cube made of ice , splintered cracked ice surface, frosted colors, blood dripping from rose, melting ice, Valentine’s Day vibes, cinematic, sharp focus, intricate, cinematic, dramatic light"
                    ],
                    [
                        "Miss Mexico portrait of the most beautiful mexican woman, Exquisite detail, 30-megapixel, 4k, 85-mm-lens, sharp-focus, f:8, ISO 100, shutter-speed 1:125, diffuse-back-lighting, award-winning photograph, small-catchlight, High-sharpness, facial-symmetry, 8k --q 2 --ar 18:32 --v 5"
                    ],
                    [
                        "3D cartoon Fox Head with Human Body, Wearing Iridescent Holographic Liquid Texture & Translucent Material Sun Protective Shirt, Boss Feel, Nike or Addidas Sun Protective Shirt, WitchPunk, Y2K Style, Green and blue, Blue, Metallic Feel, Strong Reflection, plain background, no background, pure single color background, Digital Fashion, Surreal Futurism, Supreme Kong NFT Artwork Style, disney style, headshot photography for portrait studio shoot, fashion editorial aesthetic, high resolution in the style of HAPE PRIME NFT, NFT 3D IP Feel, Bored Ape Yacht Club NFT project Feel, high detail, fine luster, 3D render, oc render, best quality, 8K, bright, front lighting, Face Shot, fine luster, ultra detailed"
                    ],
                ],
                [cap],
                label="Examples",
            )

        def on_submit(*args):
            import time

            now_time = time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(time.time()))
            with open("./user_prompt_5b.txt", "a+") as f:
                print("recording user prompt...")
                f.write(f"{now_time}@{args[0]}\n")

            for q in request_queues:
                q.put(args)
            result = response_queue.get()
            if isinstance(result, ModelFailure):
                raise RuntimeError
            return result

        submit_btn.click(
            on_submit,
            [
                cap,
                resolution,
                num_sampling_steps,
                cfg_scale,
                solver,
                t_shift,
                seed,
                ntk_scaling,
                proportional_attn,
            ],
            [output_img],
        )

    mp_barrier.wait()
    demo.queue().launch(share=True, server_name="0.0.0.0", server_port=7863)


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main()
