"""SigLIP 2 grass detector — zero-shot 'is this real grass?' classification
on webcam frames. The eyes that decide whether to clear the TOUCH GRASS
lockout on the main dashboard.

Two ways to use it:

  CLI smoke test (one shot — score a single frame from cam.py):
    .venv\\Scripts\\python.exe grass_detector.py

  CLI live mode (stream confidence at 5 Hz, never returns):
    .venv\\Scripts\\python.exe grass_detector.py --loop

  As a module (intended use, called from cam.py):
    from grass_detector import Detector
    det = Detector()                    # auto-picks cuda if available
    result = det.score(pil_image)       # -> DetectionResult(confidence, ...)

Model: google/siglip2-base-patch16-256 (Apache 2.0, ~400MB).
First run downloads weights to ~/.cache/huggingface/. Offline thereafter.
"""
from __future__ import annotations

import argparse
import io
import time
from dataclasses import dataclass, field

import requests
import torch
from PIL import Image
from transformers import AutoModel, AutoProcessor

MODEL_ID = "google/siglip2-base-patch16-256"

# Caption banks. Softmax across all captions; "confidence" is the probability
# mass on positives. Add more negatives if a specific cheat (green sticky note,
# fake plant, ...) starts scoring above threshold.
POSITIVE_CAPTIONS = [
    "a close-up photo of real grass blades",
    "fresh green grass outdoors in sunlight",
    "a hand holding a clump of grass",
    "a tuft of green plant leaves",
]
NEGATIVE_CAPTIONS = [
    "a green plastic object",
    "a green piece of paper",
    "a green pen or marker",
    "a green fabric or shirt",
    "a green plush toy",
    "a fake plastic plant",
    "a person sitting indoors at a desk",
    "an empty room",
    "a computer keyboard",
    "a screen displaying text",
]


@dataclass
class DetectionResult:
    confidence: float                                       # 0..1, mass on positives
    latency_ms: float = 0.0
    raw_probs: dict[str, float] = field(default_factory=dict)


class Detector:
    """SigLIP-2 zero-shot grass classifier. Text features are pre-computed
    once at construction so per-frame inference is image-encode + matmul only."""

    def __init__(self, device: str | None = None, dtype: torch.dtype | None = None):
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if dtype is None:
            # FP16 on GPU saves ~half the VRAM; FP32 on CPU because not all CPU
            # ops support FP16 cleanly.
            dtype = torch.float16 if device == "cuda" else torch.float32
        self.device = device
        self.dtype = dtype

        print(f"[grass] loading {MODEL_ID} on {device} ({dtype})...")
        t0 = time.time()
        self.processor = AutoProcessor.from_pretrained(MODEL_ID)
        self.model = (
            AutoModel.from_pretrained(MODEL_ID, dtype=dtype)
            .to(device)
            .eval()
        )
        print(f"[grass] loaded in {time.time() - t0:.1f}s")

        self.captions = POSITIVE_CAPTIONS + NEGATIVE_CAPTIONS
        self.n_pos = len(POSITIVE_CAPTIONS)

        # Pre-tokenize captions once. Text gets re-encoded per forward pass —
        # cheap for 14 short captions, and lets us use the canonical
        # model(**all_inputs) API which is version-stable.
        self.text_inputs = self.processor(
            text=self.captions, return_tensors="pt", padding="max_length"
        ).to(device)

    @torch.no_grad()
    def score(self, pil_image: Image.Image) -> DetectionResult:
        t0 = time.time()
        img_inputs = self.processor(images=pil_image, return_tensors="pt").to(self.device)
        img_inputs["pixel_values"] = img_inputs["pixel_values"].to(self.dtype)
        outputs = self.model(**img_inputs, **self.text_inputs)
        logits = outputs.logits_per_image.squeeze(0).float()    # (n_captions,)
        # Softmax across captions: forces a choice between positives and
        # negatives, gives a clean 0-1 confidence as "mass on positives".
        # SigLIP's logits already include the learned scale, so no extra
        # temperature factor is needed.
        probs = torch.softmax(logits, dim=-1).cpu().numpy()
        confidence = float(probs[: self.n_pos].sum())
        raw = {c: float(p) for c, p in zip(self.captions, probs)}
        return DetectionResult(
            confidence=confidence,
            latency_ms=(time.time() - t0) * 1000.0,
            raw_probs=raw,
        )


def fetch_snapshot(
    url: str = "http://localhost:8767/snapshot.jpg", timeout: float = 3.0
) -> Image.Image:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _print_breakdown(result: DetectionResult) -> None:
    print(f"\nconfidence: {result.confidence:.3f}   latency: {result.latency_ms:.1f}ms\n")
    print("breakdown (highest first):")
    for caption, p in sorted(result.raw_probs.items(), key=lambda kv: -kv[1]):
        sign = "+" if caption in POSITIVE_CAPTIONS else "-"
        bar = "#" * int(p * 40)
        print(f"  {sign}  {p:.3f}  |{bar:<40}|  {caption}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="stream scores at --hz forever")
    ap.add_argument("--hz", type=float, default=5.0, help="scoring rate when looping")
    ap.add_argument("--device", default=None, help="cuda | cpu (default: auto)")
    ap.add_argument("--cam", default="http://localhost:8767/snapshot.jpg",
                    help="cam.py snapshot URL")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="confidence cutoff for the 'GRASS' verdict")
    args = ap.parse_args()

    det = Detector(device=args.device)

    if not args.loop:
        img = fetch_snapshot(args.cam)
        r = det.score(img)
        _print_breakdown(r)
        verdict = "GRASS" if r.confidence >= args.threshold else "not grass"
        print(f"\nverdict: {verdict} (threshold {args.threshold})")
        return

    period = 1.0 / args.hz
    print(f"streaming at {args.hz} Hz, threshold {args.threshold}, ctrl-C to stop\n")
    try:
        while True:
            t_start = time.time()
            try:
                img = fetch_snapshot(args.cam)
                r = det.score(img)
                bar = "#" * int(r.confidence * 40)
                tag = " GRASS" if r.confidence >= args.threshold else "      "
                print(f"  conf {r.confidence:.3f} |{bar:<40}| {r.latency_ms:5.1f}ms {tag}")
            except Exception as e:
                print(f"  err: {e}")
            time.sleep(max(0.0, period - (time.time() - t_start)))
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
