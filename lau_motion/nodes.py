"""LAu Motion Transfer nodes.

Small helper nodes that drive the LAu Motion Transfer workflow for MiniMax H3:
mode selection, H3 frame grid, optional LoRAs, conditional saving, an optional
sparse-attention patch, a lazy wrapper for the T8 face stitch and an exact
audio/video trim.
"""
from __future__ import annotations

import json
import math
import os

import torch
import torch.nn.functional as F

import folder_paths
import nodes
from comfy_api.latest import io, ui, Types

CATEGORY = "LAu Motion Transfer"
H3_FPS = 24
H3_FRAME_STEP = 17      # H3 accepts 17n + 5 frames
H3_FRAME_BASE = 5


# --------------------------------------------------------------------------- modes
class LAuMotionModes(io.ComfyNode):
    """Turns the three workflow choices into the booleans the graph switches on."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuMotionModes",
            display_name="LAu Motion Transfer · Modes & Outputs",
            category=CATEGORY,
            description="Central control of the workflow. The AusBoss switch cards write these values; "
                        "the boolean outputs drive the lazy switches of each branch.",
            inputs=[
                io.Combo.Input("generation_mode", options=["Role Swap", "Fashion Video"]),
                io.Combo.Input("fashion_task", options=["Image to Video", "Reference to Video"]),
                io.Combo.Input("face_refine", options=["No Face Refine", "T8 Face Refine", "Adaptive Face Refine"]),
                io.Boolean.Input("dlss_enabled", default=False),
                io.Boolean.Input("save_comparison", default=False),
                io.Boolean.Input("save_debug", default=False),
                io.Boolean.Input("face_mask_sam3", default=True,
                                 tooltip="Adaptive refine: re-render and paste only the face found by SAM3 "
                                         "(off = the elliptical area of the H3-FaceRefine pack)."),
            ],
            outputs=[io.Boolean.Output(name) for name in (
                "fashion", "reference_task", "t8", "adaptive", "refine", "dlss",
                "comparison", "debug_role", "debug_t8", "debug_adaptive", "face_mask")],
        )

    @classmethod
    def execute(cls, generation_mode, fashion_task, face_refine, dlss_enabled, save_comparison, save_debug,
                face_mask_sam3=True):
        is_fashion = generation_mode == "Fashion Video"
        use_t8 = face_refine == "T8 Face Refine"
        use_adaptive = face_refine == "Adaptive Face Refine"
        any_refine = use_t8 or use_adaptive
        flags = {
            "fashion": is_fashion,
            "reference_task": fashion_task == "Reference to Video",
            "t8": use_t8,
            "adaptive": use_adaptive,
            "refine": any_refine,
            "dlss": bool(dlss_enabled),
            "comparison": bool(save_comparison) and any_refine,
            "debug_role": bool(save_debug) and not is_fashion,
            "debug_t8": bool(save_debug) and use_t8,
            "debug_adaptive": bool(save_debug) and use_adaptive,
            "face_mask": bool(face_mask_sam3) and use_adaptive,
        }
        return io.NodeOutput(*flags.values())


# --------------------------------------------------------------------------- timing
def h3_frames(seconds: float) -> int:
    """Smallest H3-valid frame count (17n + 5) that covers the requested time at 24 fps."""
    wanted = max(H3_FRAME_BASE, round(seconds * H3_FPS))
    steps = math.ceil((wanted - H3_FRAME_BASE) / H3_FRAME_STEP)
    return H3_FRAME_BASE + H3_FRAME_STEP * steps


class LAuDuration(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuDuration",
            display_name="LAu Motion Transfer · Duration (H3 frame grid)",
            category=CATEGORY,
            description="Picks the part of the source video to copy: it starts at start_seconds and lasts duration_seconds. "
                        "The length is rounded up to the next frame count H3 accepts (17n + 5) at its native 24 fps. "
                        "Example: a 15 s clip, last 5 s -> start 10, duration 5; from 6 s to 12 s -> start 6, duration 6.",
            inputs=[
                io.Float.Input("duration_seconds", default=5.0, min=0.01, max=150.0, step=0.01,
                               tooltip="How many seconds to copy."),
                io.Combo.Input("fps", options=["24 (H3 native)"]),
                io.Float.Input("start_seconds", default=0.0, min=0.0, max=3600.0, step=0.01,
                               tooltip="Where the copied part starts in the source video (0 = beginning)."),
            ],
            outputs=[io.Int.Output("frame_count"), io.Float.Output("fps"), io.Float.Output("actual_duration"),
                     io.Int.Output("skip_frames")],
        )

    @classmethod
    def execute(cls, duration_seconds, fps, start_seconds=0.0):
        count = h3_frames(duration_seconds)
        skip = int(round(start_seconds * H3_FPS))
        return io.NodeOutput(count, float(H3_FPS), count / H3_FPS, skip)


# --------------------------------------------------------------------------- LoRA
def resolve_lora(name: str) -> str:
    """Accepts an exact entry of the loras list or just its file name (any subfolder)."""
    available = folder_paths.get_filename_list("loras")
    if name in available:
        return name
    wanted = os.path.basename(name.replace("\\", "/")).lower()
    for entry in available:
        if os.path.basename(entry.replace("\\", "/")).lower() == wanted:
            return entry
    raise FileNotFoundError(f"LoRA '{name}' was not found in models/loras (searched subfolders too).")


class LAuOptionalLoRA(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuOptionalLoRA",
            display_name="LAu Motion Transfer · Optional LoRA",
            category=CATEGORY,
            description="Applies a model-only LoRA when enabled. When disabled the file does not need to exist, "
                        "so the workflow opens and runs without optional downloads.",
            inputs=[
                io.Model.Input("model"),
                io.Boolean.Input("enabled", default=False),
                io.String.Input("lora_name", default="", tooltip="File name in models/loras; subfolders are found automatically."),
                io.Float.Input("strength", default=0.75, min=-4.0, max=4.0, step=0.05),
            ],
            outputs=[io.Model.Output()],
        )

    @classmethod
    def execute(cls, model, enabled, lora_name, strength):
        if not enabled or strength == 0 or not lora_name.strip():
            return io.NodeOutput(model)
        patched, = nodes.LoraLoaderModelOnly().load_lora_model_only(model, resolve_lora(lora_name.strip()), strength)
        return io.NodeOutput(patched)


# --------------------------------------------------------------------------- save
class LAuSaveVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuSaveVideo",
            display_name="LAu Motion Transfer · Save Video (if enabled)",
            category=CATEGORY,
            description="Saves an H.264 MP4 with the workflow embedded. When disabled the upstream branch is not computed.",
            inputs=[
                io.Boolean.Input("enabled", default=True),
                io.Video.Input("video", lazy=True),
                io.String.Input("filename_prefix", default="LAu_Motion_Transfer/Final"),
                io.Float.Input("crf", default=18, min=0, max=51, step=1, tooltip="Lower = better quality, bigger file."),
            ],
            outputs=[io.String.Output("saved_path")],
            is_output_node=True,
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
        )

    @classmethod
    def check_lazy_status(cls, enabled, video=None, **kwargs):
        return ["video"] if enabled and video is None else []

    @classmethod
    def execute(cls, enabled, filename_prefix, crf, video=None):
        if not enabled or video is None:
            return io.NodeOutput("")
        width, height = video.get_dimensions()
        out_dir = folder_paths.get_output_directory()
        folder, base, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, out_dir, width, height)
        name = f"{base}_{counter:05}_.mp4"
        path = os.path.join(folder, name)
        meta = {}
        if cls.hidden.extra_pnginfo:
            meta.update(cls.hidden.extra_pnginfo)
        if cls.hidden.prompt is not None:
            meta["prompt"] = cls.hidden.prompt
        video.save_to(path, format=Types.VideoContainer("mp4"), codec=Types.VideoCodec("h264"), metadata=meta, crf=crf)
        return io.NodeOutput(path, ui=ui.PreviewVideo([ui.SavedResult(name, subfolder, io.FolderType.output)]))


# --------------------------------------------------------------------------- helpers to call other packs
def _run_node(node_id: str, **kwargs):
    """Runs another installed node by its registered id (V1 or V3) with the inputs it declares."""
    node_cls = nodes.NODE_CLASS_MAPPINGS.get(node_id)
    if node_cls is None:
        raise RuntimeError(f"'{node_id}' is not installed. See the LAu Motion Transfer README for the required packs.")
    if hasattr(node_cls, "GET_SCHEMA"):
        schema = node_cls.GET_SCHEMA()
        accepted = {i.id for i in schema.inputs}
        result = node_cls.execute(**{k: v for k, v in kwargs.items() if k in accepted})
        return tuple(result.result) if hasattr(result, "result") else tuple(result)
    spec = node_cls.INPUT_TYPES()
    accepted = set(spec.get("required", {})) | set(spec.get("optional", {}))
    return tuple(getattr(node_cls(), node_cls.FUNCTION)(**{k: v for k, v in kwargs.items() if k in accepted}))


class LAuSparseAttention(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuSparseAttention",
            display_name="LAu Motion Transfer · Optional Sparse Attention",
            category=CATEGORY,
            description="Optional Sol-Attn speed patch (needs a pack that registers SolAttnMiniMax). "
                        "Disabled = dense attention, nothing else is required.",
            inputs=[
                io.Model.Input("model"),
                io.Boolean.Input("enabled", default=False),
                io.Float.Input("tau", default=1.3, min=0.0, max=4.0, step=0.05, tooltip="Higher = sparser and faster."),
            ],
            outputs=[io.Model.Output()],
        )

    @classmethod
    def execute(cls, model, enabled, tau):
        if not enabled:
            return io.NodeOutput(model)
        out = _run_node("SolAttnMiniMax", model=model, tau=tau,
                        selection={"selection": "adaptive tau", "tau": tau},
                        start_percent=0.2, end_percent=0.9, min_tokens=12288,
                        sink_conditioning="exact_kv_and_rows", verbose=False)
        return io.NodeOutput(out[0])


class LAuT8FaceStitch(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuT8FaceStitch",
            display_name="LAu Motion Transfer · T8 Face Stitch (only when selected)",
            category=CATEGORY,
            description="Runs T8's MiniMaxH3FaceRefineStitchAuditT8Advanced. Because this wrapper is not an output node, "
                        "the T8 branch is skipped completely when another face refine is selected.",
            inputs=[
                io.Image.Input("base_frames"),
                io.Image.Input("refined_crops"),
                io.Custom("H3_T8_FACE_REFINE_PLAN").Input("face_plan"),
                io.Combo.Input("paste_region", options=["ellipse", "rectangle", "full_crop_exp"]),
                io.Float.Input("feather_source_px", default=12.0, min=0.5, max=256.0, step=0.5),
                io.Float.Input("blend_strength", default=1.0, min=0.0, max=1.0, step=0.01),
                io.Float.Input("color_match_strength", default=0.65, min=0.0, max=1.0, step=0.01),
                io.Float.Input("max_face_mean_abs_delta", default=0.4, min=0.01, max=1.0, step=0.01),
                io.Int.Input("fallback_neighbor_frames", default=1, min=0, max=12),
                io.Combo.Input("processing_device", options=["cpu_memory_safe", "cuda_if_available"]),
            ],
            outputs=[io.Image.Output("candidate_frames"), io.Mask.Output("changed_mask"), io.Mask.Output("fallback_mask"),
                     io.Int.Output("fallback_count"), io.String.Output("report_json")],
        )

    @classmethod
    def execute(cls, **kwargs):
        return io.NodeOutput(*_run_node("MiniMaxH3FaceRefineStitchAuditT8Advanced", **kwargs))


# --------------------------------------------------------------------------- trim
class LAuTrimAV(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuTrimAV",
            display_name="LAu Motion Transfer · Trim Video + Audio",
            category=CATEGORY,
            description="Cuts the decoded frames to an exact frame count (e.g. the frames really loaded from the source) "
                        "and the audio to the requested seconds, padding the audio with silence if it is short.",
            inputs=[
                io.Image.Input("frames"),
                io.Float.Input("start_seconds", default=0.0, min=0.0, max=900.0, step=0.001),
                io.Float.Input("duration_seconds", default=5.0, min=0.04, max=None, step=0.001),
                io.Float.Input("fps", default=24.0, min=1.0, max=240.0, step=0.001, advanced=True),
                io.Audio.Input("audio", optional=True),
                io.Int.Input("frame_count", default=0, min=0, max=None, optional=True, advanced=True,
                             tooltip="Exact number of video frames to keep; 0 = duration_seconds x fps."),
            ],
            outputs=[io.Image.Output("frames"), io.Audio.Output("audio"), io.String.Output("report_json")],
        )

    @classmethod
    def execute(cls, frames, start_seconds, duration_seconds, fps, audio=None, frame_count=0):
        if frames.ndim != 4:
            raise ValueError(f"frames must be an IMAGE batch [N,H,W,C], got {tuple(frames.shape)}")
        first = int(round(start_seconds * fps))
        keep = int(frame_count) if frame_count else max(1, int(round(duration_seconds * fps)))
        if first + keep > frames.shape[0]:
            raise ValueError(f"Asked for frames {first}..{first + keep} but only {frames.shape[0]} were decoded.")
        video = frames[first:first + keep]

        cut_audio = None
        if audio is not None:
            wave, rate = audio.get("waveform"), audio.get("sample_rate")
            if not isinstance(wave, torch.Tensor) or rate is None or wave.ndim != 3 or wave.shape[-1] == 0:
                raise ValueError("audio must be an AUDIO value with a [batch, channels, samples] waveform")
            rate = int(rate)
            a0 = int(round(start_seconds * rate))
            length = int(round(duration_seconds * rate))
            piece = wave[..., a0:a0 + length]
            if piece.shape[-1] < length:
                piece = F.pad(piece, (0, length - piece.shape[-1]))
            cut_audio = {"waveform": piece, "sample_rate": rate}

        report = json.dumps({"start_frame": first, "frame_count": keep, "fps": fps,
                             "video_seconds": keep / fps, "audio_seconds": duration_seconds}, indent=2)
        return io.NodeOutput(video, cut_audio, report)


# --------------------------------------------------------------------------- labeled comparison
def _label_strip(text: str, width: int, height: int) -> torch.Tensor:
    """White text centred on a black strip, returned as [height, width, 3] in 0..1."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    size = max(10, int(height * 0.55))
    font = ImageFont.load_default(size=size)
    while size > 10 and draw.textlength(text, font=font) > width * 0.94:
        size -= 2
        font = ImageFont.load_default(size=size)
    box = draw.textbbox((0, 0), text, font=font)
    x = (width - (box[2] - box[0])) / 2 - box[0]
    y = (height - (box[3] - box[1])) / 2 - box[1]
    draw.text((x, y), text, font=font, fill=(255, 255, 255))
    return torch.from_numpy(np.asarray(img).astype("float32") / 255.0)


class LAuLabeledComparison(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuLabeledComparison",
            display_name="LAu Motion Transfer · Labeled Comparison",
            category=CATEGORY,
            description="Puts two clips side by side with a caption above each one. The smaller clip is resized to "
                        "the larger one, so both halves and both captions have the same size.",
            inputs=[
                io.Image.Input("before"),
                io.Image.Input("after"),
                io.String.Input("before_label", default="ORIGINAL VIDEO"),
                io.String.Input("after_label", default="LAu MOTION TRANSFER"),
                io.Int.Input("spacing", default=8, min=0, max=128, tooltip="Black gap between the two clips, in pixels."),
            ],
            outputs=[io.Image.Output("image")],
        )

    @classmethod
    def execute(cls, before, after, before_label, after_label, spacing):
        n = min(before.shape[0], after.shape[0])
        before, after = before[:n], after[:n]
        big = after if after.shape[1] * after.shape[2] > before.shape[1] * before.shape[2] else before
        h, w = big.shape[1], big.shape[2]

        def fit(clip):
            if clip.shape[1:3] == (h, w):
                return clip
            clip = F.interpolate(clip.movedim(-1, 1), size=(h, w), mode="bicubic", align_corners=False)
            return clip.clamp(0, 1).movedim(1, -1)

        before, after = fit(before[..., :3]), fit(after[..., :3])
        strip_h = max(24, int(round(h * 0.045)) // 2 * 2)
        rows = []
        for clip, text in ((before, before_label), (after, after_label)):
            strip = _label_strip(text, w, strip_h).to(clip.device, clip.dtype)
            rows.append(torch.cat([strip.unsqueeze(0).expand(n, -1, -1, -1), clip[..., :3]], dim=1))
        gap = torch.zeros((n, h + strip_h, spacing, 3), device=before.device, dtype=before.dtype)
        return io.NodeOutput(torch.cat([rows[0], gap, rows[1]], dim=2))


# --------------------------------------------------------------------------- face mask on the latent
class LAuFaceMaskLatent(io.ComfyNode):
    """Re-render only the face: limits the H3 refine latent to a SAM3 face mask (or passes everything through)."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="LAuFaceMaskLatent",
            display_name="LAu Motion Transfer · Face Mask on Latent",
            category=CATEGORY,
            description="When enabled: multiplies the video noise mask of a MiniMax H3 joint AV latent by a "
                        "latent-resolution face mask (SAM3 masks reduced with MVEx Mask To Latent Space), keeping a "
                        "per-frame mask already on the latent (H3 Per-Frame Denoise); applies Differential Diffusion "
                        "to the model; and outputs the pixel masks for the face stitch. When disabled the masks are "
                        "not computed at all and model, latent and stitch behave as without this node.",
            inputs=[
                io.Model.Input("model"),
                io.Latent.Input("av_latent"),
                io.Boolean.Input("enabled", default=True),
                io.Mask.Input("latent_mask", optional=True, lazy=True,
                              tooltip="One mask per latent frame, at latent resolution."),
                io.Mask.Input("pixel_masks", optional=True, lazy=True,
                              tooltip="Face masks in crop (canvas) space, passed on to the face stitch."),
                io.Float.Input("outside_strength", default=0.0, min=0.0, max=1.0, step=0.05,
                               tooltip="Denoise kept outside the mask. 0 = only the face is re-rendered."),
            ],
            outputs=[io.Model.Output(display_name="model"), io.Latent.Output(display_name="av_latent"),
                     io.Mask.Output(display_name="stitch_masks"), io.String.Output(display_name="report")],
        )

    @classmethod
    def check_lazy_status(cls, enabled, latent_mask=None, pixel_masks=None, **kwargs):
        if not enabled:
            return []
        return [n for n, v in (("latent_mask", latent_mask), ("pixel_masks", pixel_masks)) if v is None]

    @classmethod
    def execute(cls, model, av_latent, enabled, outside_strength, latent_mask=None, pixel_masks=None):
        if not enabled or latent_mask is None:
            return io.NodeOutput(model, av_latent, None, "face mask off: elliptical face area")
        import comfy.nested_tensor as nt

        def is_nested(x):
            return isinstance(x, nt.NestedTensor) or getattr(x, "is_nested", False)

        samples = av_latent["samples"]
        nested = is_nested(samples)
        members = list(samples.unbind()) if nested else [samples]
        video = members[0]                                    # [B, C, T, H, W]
        t, h, w = video.shape[-3:]

        m = latent_mask.float()
        if m.ndim == 2:
            m = m[None]
        if tuple(m.shape) != (t, h, w):
            m = F.interpolate(m[None, None], size=(t, h, w), mode="nearest")[0, 0]
        m = outside_strength + (1.0 - outside_strength) * m.clamp(0, 1)
        m5 = m.view(1, 1, t, h, w).to(video.device)

        prev = av_latent.get("noise_mask")
        prev_members = (list(prev.unbind()) if is_nested(prev) else [prev]) if prev is not None else [None]
        pv = prev_members[0]
        if pv is None or pv.ndim != 5 or tuple(pv.shape[-3:]) != (t, h, w):
            pv = torch.ones(video.shape[:2] + (t, h, w), device=video.device)
        vmask = (pv.to(video.device, torch.float32) * m5).expand(video.shape[0], video.shape[1], t, h, w)
        vmask = vmask.contiguous().to(video.dtype)

        rest = []
        for i, mem in enumerate(members[1:], start=1):
            old = prev_members[i] if i < len(prev_members) else None
            rest.append(old if old is not None else torch.zeros_like(mem))   # audio: unchanged / held
        out = dict(av_latent)
        out["noise_mask"] = nt.NestedTensor(tuple([vmask] + rest)) if nested else vmask

        soft_model = _run_node("DifferentialDiffusion", model=model, strength=1.0)[0]

        cover = float((m > 0.5).float().mean())
        empty = int(((m.reshape(t, -1) > 0.5).sum(dim=1) == 0).sum())
        report = (f"face mask on latent {t}x{h}x{w}: {cover * 100:.1f}% of the video latent is re-rendered; "
                  f"{empty} latent frame(s) without a face are kept as they were")
        print(f"[LAu Motion Transfer] {report}")
        return io.NodeOutput(soft_model, out, pixel_masks, report)


NODES = [LAuMotionModes, LAuDuration, LAuOptionalLoRA, LAuSaveVideo, LAuSparseAttention, LAuT8FaceStitch, LAuTrimAV,
         LAuLabeledComparison, LAuFaceMaskLatent]
