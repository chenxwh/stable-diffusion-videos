import os
from subprocess import call
from typing import Optional, List
import shutil
import numpy as np
import torch
from torch import autocast
from diffusers import DDIMScheduler, LMSDiscreteScheduler, PNDMScheduler
from PIL import Image
from cog import BasePredictor, Input, Path

from stable_diffusion_pipeline import StableDiffusionPipeline


MODEL_CACHE = "diffusers-cache"


class Predictor(BasePredictor):
    def setup(self):
        """Load the model into memory to make running multiple predictions efficient"""
        print("Loading pipeline...")

        self.pipeline = StableDiffusionPipeline.from_pretrained(
            "CompVis/stable-diffusion-v1-4",
            # scheduler=lms,
            cache_dir="diffusers-cache",
            local_files_only=True,
            # revision="fp16",
            torch_dtype=torch.float16,
        ).to("cuda")

        default_scheduler = PNDMScheduler(
            beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear"
        )
        ddim_scheduler = DDIMScheduler(
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            clip_sample=False,
            set_alpha_to_one=False,
        )
        klms_scheduler = LMSDiscreteScheduler(
            beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear"
        )
        self.SCHEDULERS = dict(
            default=default_scheduler, ddim=ddim_scheduler, klms=klms_scheduler
        )

    @torch.inference_mode()
    @torch.cuda.amp.autocast()
    def predict(
        self,
        prompts: str = Input(
            description="Input prompts, separate each prompt with '|'.",
            default="a cat | a dog | a horse",
        ),
        seeds: str = Input(
            description="Random seed, separated with '|' to use different seeds for each of the prompt provided above. Leave blank to randomize the seed.",
            default=None,
        ),
        scheduler: str = Input(
            description="Choose the scheduler",
            choices=["default", "ddim", "klms"],
            default="klms",
        ),
        num_inference_steps: int = Input(
            description="Number of denoising steps for each image generated from the prompt",
            ge=1,
            le=500,
            default=50,
        ),
        guidance_scale: float = Input(
            description="Scale for classifier-free guidance", ge=1, le=20, default=7.5
        ),
        num_steps: int = Input(
            description="Steps for generating the interpolation video. Recommended to set to 3 or 5 for testing, then up it to 60-200 for better results.",
            default=50,
        ),
        fps: int = Input(
            description="Frame rate for the video.", default=15, ge=5, le=60
        ),
    ) -> Path:
        """Run a single prediction on the model"""

        prompts = [p.strip() for p in prompts.split("|")]
        if seeds is None:
            print("Setting Random seeds.")
            seeds = [int.from_bytes(os.urandom(2), "big") for s in range(len(prompts))]
        else:
            seeds = [s.strip() for s in seeds.split("|")]
            for s in seeds:
                assert s.isdigit(), "Please provide integer seed."
            seeds = [int(s) for s in seeds]

            if len(seeds) > len(prompts):
                seeds = seeds[: len(prompts)]
            else:
                seeds_not_set = len(prompts) - len(seeds)
                print("Setting Random seeds.")
                seeds = seeds + [
                    int.from_bytes(os.urandom(2), "big") for s in range(seeds_not_set)
                ]

        print("Seeds used for prompts are:")
        for prompt, seed in zip(prompts, seeds):
            print(f"{prompt}: {seed}")

        # use the default settings for the demo
        height = 512
        width = 512
        eta = 0.0
        disable_tqdm = False
        use_lerp_for_text = False

        self.pipeline.set_progress_bar_config(disable=disable_tqdm)
        self.pipeline.scheduler = self.SCHEDULERS[scheduler]

        outdir = "cog_out"
        if os.path.exists(outdir):
            shutil.rmtree(outdir)
        os.makedirs(outdir)

        first_prompt, *prompts = prompts
        embeds_a = self.pipeline.embed_text(first_prompt)

        first_seed, *seeds = seeds
        latents_a = torch.randn(
            (1, self.pipeline.unet.in_channels, height // 8, width // 8),
            device=self.pipeline.device,
            generator=torch.Generator(device=self.pipeline.device).manual_seed(
                first_seed
            ),
        )

        frame_index = 0
        for prompt, seed in zip(prompts, seeds):
            # Text
            embeds_b = self.pipeline.embed_text(prompt)

            # Latent Noise
            latents_b = torch.randn(
                (1, self.pipeline.unet.in_channels, height // 8, width // 8),
                device=self.pipeline.device,
                generator=torch.Generator(device=self.pipeline.device).manual_seed(
                    seed
                ),
            )

            for i, t in enumerate(np.linspace(0, 1, num_steps)):
                do_print_progress = (i == 0) or ((frame_index + 1) % 20 == 0)
                if do_print_progress:
                    print(f"COUNT: {frame_index+1}/{len(seeds)*num_steps}")

                if use_lerp_for_text:
                    embeds = torch.lerp(embeds_a, embeds_b, float(t))
                else:
                    embeds = slerp(float(t), embeds_a, embeds_b)
                latents = slerp(float(t), latents_a, latents_b)

                with torch.autocast("cuda"):
                    im = self.pipeline(
                        latents=latents,
                        text_embeddings=embeds,
                        height=height,
                        width=width,
                        guidance_scale=guidance_scale,
                        eta=eta,
                        num_inference_steps=num_inference_steps,
                    )["sample"][0]

                im.save(os.path.join(outdir, "frame%06d.jpg" % frame_index))
                frame_index += 1

            embeds_a = embeds_b
            latents_a = latents_b

        image_path = os.path.join(outdir, "frame%06d.jpg")
        video_path = f"/tmp/out.mp4"

        cmdd = (
            "ffmpeg -y -r "
            + str(fps)
            + " -i "
            + image_path
            + " -vcodec libx264 -crf 25  -pix_fmt yuv420p "
            + video_path
        )
        run_cmd(cmdd)

        return Path(video_path)


def run_cmd(command):
    try:
        call(command, shell=True)
    except KeyboardInterrupt:
        print("Process interrupted")
        sys.exit(1)


def slerp(t, v0, v1, DOT_THRESHOLD=0.9995):
    """helper function to spherically interpolate two arrays v1 v2"""

    if not isinstance(v0, np.ndarray):
        inputs_are_torch = True
        input_device = v0.device
        v0 = v0.cpu().numpy()
        v1 = v1.cpu().numpy()

    dot = np.sum(v0 * v1 / (np.linalg.norm(v0) * np.linalg.norm(v1)))
    if np.abs(dot) > DOT_THRESHOLD:
        v2 = (1 - t) * v0 + t * v1
    else:
        theta_0 = np.arccos(dot)
        sin_theta_0 = np.sin(theta_0)
        theta_t = theta_0 * t
        sin_theta_t = np.sin(theta_t)
        s0 = np.sin(theta_0 - theta_t) / sin_theta_0
        s1 = sin_theta_t / sin_theta_0
        v2 = s0 * v0 + s1 * v1

    if inputs_are_torch:
        v2 = torch.from_numpy(v2).to(input_device)

    return v2
