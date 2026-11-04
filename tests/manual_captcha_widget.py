"""Manual check: run the real slider solver against a local mock widget.

Not part of `python -m unittest discover` (the filename does not start with
`test`), because it needs Chromium and takes a few seconds.

The mock deliberately positions the puzzle piece with `transform: translateX()`
instead of `left`. That is the case that broke in production: the refine loop
read `.style.left`, got an empty string, assumed the piece had never moved and
dragged until the mouse hit the screen edge — five failures in a row.

It also mirrors the real DOM shape: the puzzle image is the size of the whole
background with the cut-out drawn at the hole's vertical position, so the
solver's alpha-bbox maths are exercised the same way.

Usage:
    python tests/manual_captcha_widget.py [runs]
"""

import asyncio
import pathlib
import random
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rich.console import Console  # noqa: E402

from src.captcha import solve_slider_local  # noqa: E402

BG_W, BG_H = 480, 240   # natural size of the background
PIECE = 60              # size of the cut-out
DISPLAY_W = 216         # how wide it is rendered, i.e. scale = 0.45

MOCK = """
<style>
  body { margin: 0; background: #eee; }
  #aliyunCaptcha-captcha-wrapper { padding: 30px; width: max-content; }
  #aliyunCaptcha-captcha-body { position: relative; }
  #aliyunCaptcha-img { display: block; width: {{DISPLAY_W}}px; }
  #aliyunCaptcha-puzzle {
    position: absolute; left: 0; top: 0; width: {{DISPLAY_W}}px;
    transform: translateX(0px); pointer-events: none;
  }
  #aliyunCaptcha-sliding-slider {
    margin-top: 10px; width: {{DISPLAY_W}}px; height: 34px; background: #bbb;
    border-radius: 4px; cursor: pointer;
  }
</style>
<div id="aliyunCaptcha-captcha-wrapper">
  <div id="aliyunCaptcha-captcha-body">
    <img id="aliyunCaptcha-img" alt="">
    <img id="aliyunCaptcha-puzzle" alt="">
  </div>
  <div id="aliyunCaptcha-sliding-slider"></div>
</div>
<script>
  const BG_W = {{BG_W}}, BG_H = {{BG_H}}, P = {{PIECE}};
  const scale = {{DISPLAY_W}} / BG_W;
  const GX = {{GX}}, GY = {{GY}};

  // Smooth blobs, not per-pixel noise: a real captcha photo has structure, and
  // flat noise makes normalised cross-correlation ambiguous.
  function texture(ctx, w, h, seed) {
    let s = seed;
    const rnd = () => (s = (s * 9301 + 49297) % 233280) / 233280;
    ctx.fillStyle = '#20303f';
    ctx.fillRect(0, 0, w, h);
    for (let i = 0; i < 140; i++) {
      const x = rnd() * w, y = rnd() * h, r = 10 + rnd() * 70;
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      const c = [Math.floor(rnd() * 255), Math.floor(rnd() * 255), Math.floor(rnd() * 255)];
      g.addColorStop(0, 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',0.9)');
      g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    }
  }

  const bg = document.createElement('canvas');
  bg.width = BG_W; bg.height = BG_H;
  const bx = bg.getContext('2d');
  texture(bx, BG_W, BG_H, {{SEED}});

  // Keep the bright original for the piece, then darken the hole.
  const piece = bx.getImageData(GX, GY, P, P);
  bx.fillStyle = 'rgba(0,0,0,0.45)';
  bx.fillRect(GX, GY, P, P);

  const pz = document.createElement('canvas');
  pz.width = BG_W; pz.height = BG_H;
  const px = pz.getContext('2d');
  px.putImageData(piece, 0, GY);

  px.globalCompositeOperation = 'destination-in';
  px.beginPath();
  px.moveTo(P*0.18, GY); px.lineTo(P*0.82, GY); px.lineTo(P*0.82, GY + P*0.28);
  px.arc(P*0.82, GY + P*0.42, P*0.14, -Math.PI/2, Math.PI/2);
  px.lineTo(P*0.82, GY + P); px.lineTo(P*0.18, GY + P); px.lineTo(P*0.18, GY + P*0.72);
  px.arc(P*0.18, GY + P*0.58, P*0.14, Math.PI/2, -Math.PI/2, true);
  px.closePath(); px.fill();

  const bgImg = document.getElementById('aliyunCaptcha-img');
  const pzImg = document.getElementById('aliyunCaptcha-puzzle');
  const slider = document.getElementById('aliyunCaptcha-sliding-slider');
  bgImg.src = bg.toDataURL();
  pzImg.src = pz.toDataURL();

  // Leftmost opaque column, only for diagnostics.
  const pd = px.getImageData(0, 0, BG_W, BG_H).data;
  let pzMin = BG_W;
  for (let y = 0; y < BG_H; y++) for (let x = 0; x < BG_W; x++) {
    if (pd[(y * BG_W + x) * 4 + 3] > 128 && x < pzMin) pzMin = x;
  }
  // The canvas already carries the hole's own pixels at x=0..P, so aligning the
  // shape with the hole means shifting the element by the hole's x — the alpha
  // inset is internal to the image and must not be subtracted here.
  const target = GX * scale;

  let dragging = false, startX = 0, offset = 0;
  function setOffset(v) {
    offset = Math.max(0, Math.min(v, (BG_W - P) * scale));
    pzImg.style.transform = 'translateX(' + offset + 'px)';
  }

  slider.addEventListener('mousedown', (e) => {
    dragging = true; startX = e.clientX - offset; e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (dragging) setOffset(e.clientX - startX);
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    if (Math.abs(offset - target) <= 4) {
      document.getElementById('aliyunCaptcha-captcha-wrapper').style.display = 'none';
      window.__solvedAt = offset;
    } else {
      window.__misses = (window.__misses || 0) + 1;
      setOffset(0);
    }
  });

  window.__target = target;
  window.__gx = GX;
  window.__pzMin = pzMin;
</script>
"""


def make_page() -> str:
    hole_x = random.randint(PIECE + 20, BG_W - PIECE - 20)
    hole_y = random.randint(10, BG_H - PIECE - 10)
    html = MOCK
    for key, value in {
        "BG_W": BG_W,
        "BG_H": BG_H,
        "PIECE": PIECE,
        "DISPLAY_W": DISPLAY_W,
        "GX": hole_x,
        "GY": hole_y,
        "SEED": random.randint(1, 99999),
    }.items():
        html = html.replace("{{%s}}" % key, str(value))
    return html


async def main(runs: int) -> int:
    console = Console(width=100)
    ok = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for i in range(1, runs + 1):
            page = await browser.new_page(viewport={"width": 900, "height": 700})
            await page.set_content(make_page())
            await page.wait_for_timeout(300)

            target = await page.evaluate("() => window.__target")
            solved = await solve_slider_local(page, max_attempts=3, console=console)
            landed = await page.evaluate("() => window.__solvedAt ?? null")
            misses = await page.evaluate("() => window.__misses ?? 0")

            good = solved and isinstance(landed, (int, float)) and abs(landed - target) <= 4
            ok += good
            print(
                f"  run {i}: solved={solved} target={target:.1f} "
                f"landed={landed} misses={misses} -> {'PASS' if good else 'FAIL'}"
            )
            await page.close()
        await browser.close()

    print(f"\n{ok}/{runs} 通过")
    return 0 if ok == runs else 1


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    raise SystemExit(asyncio.run(main(count)))
