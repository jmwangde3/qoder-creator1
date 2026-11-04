"""Local Aliyun slider solver for the Qoder login challenge.

Adapted from qoder-creator1/src/captcha.py (MIT). This module only detects and
drags the puzzle; it does not call an external CAPTCHA service.
"""

from __future__ import annotations

import base64
import json
import pathlib
import random
import time
from typing import Awaitable, Callable, Optional

from rich.console import Console

from .utils import write_log


CONTAINER_TIMEOUT_MS = 6000
WIDGET_TIMEOUT_MS = 12000
MAX_BARREN_WAITS = 2
SETTLE_TRIES = 5
SETTLE_GAP_MS = 25

# Archive every challenge before dragging. Set to False once target
# identification is trustworthy; until then the images are the only way to
# tell "the drag missed" from "the solver aimed at the wrong x".
SNAP_CHALLENGES = True

READY_JS = (
    "() => { const i=document.querySelector('#aliyunCaptcha-img');"
    " const s=document.querySelector('#aliyunCaptcha-sliding-slider');"
    " return !!(i&&i.complete&&i.naturalWidth>0"
    "&&s&&s.getBoundingClientRect().width>0); }"
)


PUZZLE_X_JS = r"""
() => {
  const puzzle = document.querySelector('#aliyunCaptcha-puzzle');
  if (!puzzle) return null;

  // Read the *target* offset, not the rendered one. getComputedStyle()
  // returns the interpolated transform while the widget's CSS transition is
  // still running, so a read taken mid-drag reports the piece further behind
  // than it will end up; dividing by that ratio makes every correction
  // oversized. The inline style is what the widget set for this pointer
  // position, so it settles immediately.
  const parse = (text) => {
    if (!text) return null;
    let m = /matrix3d\(([^)]+)\)/.exec(text);
    if (m) {
      const v = m[1].split(',').map(Number);
      if (v.length === 16 && Number.isFinite(v[12])) return v[12];
    }
    m = /matrix\(([^)]+)\)/.exec(text);
    if (m) {
      const v = m[1].split(',').map(Number);
      if (v.length === 6 && Number.isFinite(v[4])) return v[4];
    }
    m = /translateX\(\s*(-?[0-9.]+)px/i.exec(text);
    if (m) return parseFloat(m[1]);
    m = /translate3d\(\s*(-?[0-9.]+)px/i.exec(text);
    if (m) return parseFloat(m[1]);
    m = /translate\(\s*(-?[0-9.]+)px/i.exec(text);
    if (m) return parseFloat(m[1]);
    return null;
  };

  const inlineTf = parse(puzzle.style.transform);
  const inlineLeft = parseFloat(puzzle.style.left);
  const hasLeft = Number.isFinite(inlineLeft);

  // A non-zero value on either axis is the real answer. `left` is often the
  // stale 0 the widget parks there while it animates the transform, so the
  // transform wins only when it actually moved.
  if (inlineTf !== null && Math.abs(inlineTf) > 0.01) return inlineTf;
  if (hasLeft && Math.abs(inlineLeft) > 0.01) return inlineLeft;
  if (inlineTf !== null) return inlineTf;
  if (hasLeft) return inlineLeft;

  // Nothing inline yet: fall back to whatever the browser computed.
  return parse(getComputedStyle(puzzle).transform);
}
"""


async def _puzzle_x(page) -> Optional[float]:
    """Current puzzle offset in CSS pixels, or None when it cannot be read."""
    try:
        value = await page.evaluate(PUZZLE_X_JS)
    except Exception:
        return None
    return float(value) if isinstance(value, (int, float)) else None


async def _settled_x(
    page,
    tries: int = SETTLE_TRIES,
    gap_ms: int = SETTLE_GAP_MS,
    stable: int = 3,
    min_ms: int = 250,
) -> Optional[float]:
    """Puzzle offset once the widget really stops moving it.

    A read taken immediately after a mouse move samples a mid-flight value.
    Worse, the widget's animation has plateaus: two consecutive samples can
    agree while another 30-60px of travel is still queued, and a "two equal
    reads" rule happily returns that plateau. Demanding three equal reads and
    a minimum elapsed time is what stopped trims from overshooting by exactly
    the distance the plateau was still hiding.
    """
    last = None
    same = 0
    elapsed = 0
    for _ in range(tries):
        value = await _puzzle_x(page)
        if value is not None:
            if last is not None and abs(value - last) <= 0.3:
                same += 1
                if same >= stable - 1 and elapsed >= min_ms:
                    return value
            else:
                same = 0
            last = value
        await page.wait_for_timeout(gap_ms)
        elapsed += gap_ms
    return last


DUMP_JS = r"""
() => {
  const pick = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      id: el.id || null,
      tag: el.tagName,
      rect: {x:r.x, y:r.y, w:r.width, h:r.height},
      inlineLeft: el.style.left || null,
      inlineTransform: el.style.transform || null,
      computedTransform: cs.transform,
      position: cs.position,
      width: cs.width,
      height: cs.height,
      transformOrigin: cs.transformOrigin,
      parent: el.parentElement
        ? (el.parentElement.id || el.parentElement.tagName) : null
    };
  };
  const nodes = {};
  document.querySelectorAll('[id^="aliyunCaptcha"]').forEach((el) => {
    nodes[el.id] = pick(el);
  });
  const img = document.querySelector('#aliyunCaptcha-img');
  const view = {w: window.innerWidth, h: window.innerHeight};
  return {
    nodes,
    view,
    imgNatural: img ? {w: img.naturalWidth, h: img.naturalHeight} : null,
    bodyText: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 240)
  };
}
"""


SNAP_JS = r"""
async () => {
  const bg = document.querySelector('#aliyunCaptcha-img');
  const pz = document.querySelector('#aliyunCaptcha-puzzle');
  if (!bg || !pz) return null;
  async function grab(img) {
    const res = await fetch(img.src, {signal: AbortSignal.timeout(8000)});
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const tmp = new Image();
    await new Promise((ok, bad) => { tmp.onload = ok; tmp.onerror = bad; tmp.src = url; });
    const c = document.createElement('canvas');
    c.width = tmp.naturalWidth; c.height = tmp.naturalHeight;
    c.getContext('2d').drawImage(tmp, 0, 0);
    URL.revokeObjectURL(url);
    return {data: c.toDataURL('image/png'), w: c.width, h: c.height};
  }
  const bgRect = bg.getBoundingClientRect();
  return {
    bg: await grab(bg),
    puzzle: await grab(pz),
    bgRect: {w: bgRect.width, h: bgRect.height},
    natural: {w: bg.naturalWidth, h: bg.naturalHeight}
  };
}
"""


async def _dump_images(page, tag: str) -> Optional[str]:
    """Save the challenge images themselves when a solve is rejected.

    Target identification is the remaining failure mode, and it cannot be
    diagnosed from geometry: the drag can land within 0.1px of a wrong
    target. The images are the only way to check which x the solver should
    have chosen.
    """
    try:
        payload = await page.evaluate(SNAP_JS)
    except Exception:
        return None
    if not payload:
        return None
    try:
        out = pathlib.Path("data")
        out.mkdir(parents=True, exist_ok=True)
        stem = out / f"captcha_img_{int(time.time())}_{tag}"
        saved = []
        for key in ("bg", "puzzle"):
            item = payload.get(key) or {}
            data = item.get("data") or ""
            if "," not in data:
                continue
            blob = base64.b64decode(data.split(",", 1)[1])
            path = pathlib.Path(f"{stem}_{key}.png")
            path.write_bytes(blob)
            saved.append(str(path))
        if not saved:
            return None
        meta = pathlib.Path(f"{stem}.json")
        meta.write_text(
            json.dumps(
                {
                    "bgRect": payload.get("bgRect"),
                    "natural": payload.get("natural"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return ", ".join(saved)
    except Exception:
        return None


async def _dump_dom(page, tag: str) -> Optional[str]:
    """Persist the widget's real geometry so a mismatch can be inspected later."""
    try:
        payload = await page.evaluate(DUMP_JS)
    except Exception:
        return None
    try:
        out = pathlib.Path("data")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"captcha_dom_{int(time.time())}_{tag}.json"
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return str(path)
    except Exception:
        return None


async def _smooth_move(
    page, from_x: float, to_x: float, y: float, segments: int = 12, gap_ms: int = 18
) -> None:
    """Drag along one continuous, eased arc instead of move-read-move-read.

    The widget grades the trajectory, not only the endpoint: evenly spaced
    moves separated by fixed pauses read as a robot, which is why a drag that
    landed within 1px was still rejected. Ease-out plus jitter looks like a
    hand, and it keeps the sample rate high enough that the endpoint is exact.
    """
    total = to_x - from_x
    # The last leg has to be an actual jump. The widget ignores pointer deltas
    # below a few pixels, and an eased tail ends in 1-2px samples that are all
    # swallowed: a 232px drag came up 8-9px short every time, which is exactly
    # the sum of the discarded tail. Ease to 92%, then step the rest.
    final_leg = min(abs(total), max(12.0, abs(total) * 0.08))
    eased_to = to_x - (final_leg if total >= 0 else -final_leg)

    for index in range(1, segments + 1):
        fraction = index / segments
        eased = 1.0 - (1.0 - fraction) ** 2
        x = from_x + (eased_to - from_x) * eased
        # steps=1: Playwright's own interpolation fires extra pointermove
        # events, and the live widget loses a slice of every one of them. A
        # 200px drag came up 43px short -- roughly 4px per interpolation
        # point -- so the fewer events, the closer the endpoint lands.
        await page.mouse.move(x, y + (random.random() - 0.5) * 0.6, steps=1)
        await page.wait_for_timeout(gap_ms)

    if final_leg > 0.5:
        await page.mouse.move(to_x, y, steps=1)


SOLVER_JS = r"""
async (mode) => {
  const bgImg = document.querySelector('#aliyunCaptcha-img');
  const puzzleImg = document.querySelector('#aliyunCaptcha-puzzle');
  const slider = document.querySelector('#aliyunCaptcha-sliding-slider');
  if (!bgImg || !puzzleImg || !slider) {
    return {targetX:-1, error:'missing-elements'};
  }
  async function getPixels(img) {
    const res = await fetch(img.src, {signal: AbortSignal.timeout(10000)});
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const tmp = new Image();
    await new Promise((ok,bad) => {
      tmp.onload=ok; tmp.onerror=bad; tmp.src=url;
    });
    const canvas = document.createElement('canvas');
    canvas.width=tmp.naturalWidth; canvas.height=tmp.naturalHeight;
    const context=canvas.getContext('2d');
    context.drawImage(tmp,0,0);
    const data=context.getImageData(0,0,canvas.width,canvas.height);
    URL.revokeObjectURL(url);
    return {w:canvas.width,h:canvas.height,d:data.data};
  }
  const bg=await getPixels(bgImg);
  const pz=await getPixels(puzzleImg);
  let minX=pz.w,maxX=0,minY=pz.h,maxY=0;
  for(let y=0;y<pz.h;y++) for(let x=0;x<pz.w;x++) {
    if(pz.d[(y*pz.w+x)*4+3]>128) {
      if(x<minX)minX=x; if(x>maxX)maxX=x;
      if(y<minY)minY=y; if(y>maxY)maxY=y;
    }
  }
  const cw=maxX-minX+1, ch=maxY-minY+1;
  if(cw<=0||ch<=0) return {targetX:-1,error:'empty-puzzle'};
  const pzG=new Float64Array(cw*ch), pzM=new Uint8Array(cw*ch);
  let maskN=0;
  for(let cy=0;cy<ch;cy++) for(let cx=0;cx<cw;cx++) {
    const si=((minY+cy)*pz.w+(minX+cx))*4, di=cy*cw+cx;
    const alpha=pz.d[si+3];
    pzM[di]=alpha>128?1:0;
    if(alpha>128)maskN++;
    pzG[di]=pz.d[si]*.299+pz.d[si+1]*.587+pz.d[si+2]*.114;
  }
  const bgG=new Float64Array(bg.w*bg.h);
  for(let i=0;i<bg.w*bg.h;i++) {
    bgG[i]=bg.d[i*4]*.299+bg.d[i*4+1]*.587+bg.d[i*4+2]*.114;
  }
  // Erode the mask by one pixel. A real piece's outermost ring is
  // alpha-blended, and the hole's rim still shows the untouched photo, so
  // those pixels disagree by construction and pull the correlation toward a
  // decoy one piece-width away. Dropping the ring costs ~12% of the samples
  // and removes that failure mode entirely.
  const pzE=new Uint8Array(cw*ch);
  let validN=0;
  for(let y=0;y<ch;y++) for(let x=0;x<cw;x++) {
    const i=y*cw+x;
    if(!pzM[i]||x===0||y===0||x===cw-1||y===ch-1) continue;
    let solid=1;
    for(let dy=-1;dy<=1&&solid;dy++) for(let dx=-1;dx<=1;dx++) {
      if(!pzM[i+dy*cw+dx]){solid=0;break;}
    }
    if(solid){pzE[i]=1;validN++;}
  }
  if(validN<64) return {targetX:-1,error:'degenerate-mask'};
  function blur(data,w,h) {
    // Edge-replicating 3x3 blur. The piece's greedy alpha bbox means the
    // outer ring of `data` IS the outline the mask selects, so letting it
    // decay to zero (a plain 1..n-2 loop) corrupts the strongest evidence
    // the matcher has: the silhouette. Clamping costs nothing and keeps
    // border pixels close to their neighbours instead of near black.
    const out=new Float64Array(w*h);
    for(let y=0;y<h;y++) {
      const ym=y>0?y-1:0, yp=y<h-1?y+1:h-1;
      for(let x=0;x<w;x++) {
        const xm=x>0?x-1:0, xp=x<w-1?x+1:w-1;
        out[y*w+x]=(
          data[ym*w+xm]+2*data[ym*w+x]+data[ym*w+xp]+
          2*data[y*w+xm]+4*data[y*w+x]+2*data[y*w+xp]+
          data[yp*w+xm]+2*data[yp*w+x]+data[yp*w+xp]
        )/16;
      }
    }
    return out;
  }
  const bgB=blur(bgG,bg.w,bg.h),pzB=blur(pzG,cw,ch);
  function ncc(src,sw,sh,tpl,tw,th,mask,maskN,offY) {
    let bx=-1,by=-1,best=-2;
    const y0=Math.max(0,offY-10),y1=Math.min(sh-th,offY+10);
    // maskN counts opaque pixels; the mask spans tw*th slots and the opaque
    // ones are scattered through it, so the scan must cover the whole array.
    // Stopping at maskN silently drops the piece's lower rows and skews both
    // means, which makes NCC pick a matching-texture decoy elsewhere.
    const span=mask.length;
    for(let ox=1;ox<=sw-tw-1;ox++) for(let oy=y0;oy<=y1;oy++) {
      let tm=0,sm=0;
      for(let i=0;i<span;i++) {
        if(!mask[i])continue;
        const tx=i%tw,ty=(i/tw)|0;
        tm+=tpl[i]; sm+=src[(oy+ty)*sw+ox+tx];
      }
      tm/=maskN; sm/=maskN;
      let tv=0,sv=0,cv=0;
      for(let i=0;i<span;i++) {
        if(!mask[i])continue;
        const tx=i%tw,ty=(i/tw)|0;
        const td=tpl[i]-tm,sd=src[(oy+ty)*sw+ox+tx]-sm;
        tv+=td*td;sv+=sd*sd;cv+=sd*td;
      }
      tv/=maskN; sv/=maskN;
      const ts=Math.sqrt(tv),ss=Math.sqrt(sv);
      if(ts<.001||ss<.001)continue;
      const score=cv/(maskN*ss*ts);
      if(score>best){best=score;bx=ox;by=oy;}
    }
    return {bx,by,best};
  }
  const found=ncc(bgB,bg.w,bg.h,pzB,cw,ch,pzE,validN,minY);

  // Second opinion. The live challenge whitens the gap: its interior is nearly
  // flat white, so the NCC denominator collapses there and the correlation
  // peaks anywhere with texture instead. Scoring "bright, featureless, with a
  // strong outline" targets what the gap physically is rather than what the
  // piece looks like. Reported only -- the drag still follows NCC until this
  // has been measured against real outcomes.
  function altScore(src,sw,sh,tw,th,mask,maskE,offY) {
    let bx=-1,by=-1,best=-1e9;
    const y0=Math.max(1,offY-12), y1=Math.min(sh-th-1,offY+12);
    const area=tw*th;
    for(let ox=1; ox<=sw-tw-1; ox++) {
      for(let oy=y0; oy<=y1; oy++) {
        let n=0,sum=0,sum2=0;
        for(let i=0;i<area;i++) {
          if(!mask[i]) continue;
          const v=src[(oy+((i/tw)|0))*sw+ox+(i%tw)];
          sum+=v; sum2+=v*v; n++;
        }
        if(n<64) continue;
        const mean=sum/n;
        const sd=Math.sqrt(Math.max(0,sum2/n-mean*mean));
        let gs=0,gn=0;
        for(let i=0;i<area;i++) {
          if(!mask[i]||maskE[i]) continue;
          const px=i%tw, py=(i/tw)|0;
          const c=(oy+py)*sw+ox+px;
          gs+=Math.abs(src[c+1]-src[c-1])+Math.abs(src[c+sw]-src[c-sw]);
          gn++;
        }
        const edge=gn>0?gs/gn:0;
        const s=(mean-2.5*sd)/255+edge/40;
        if(s>best){best=s;bx=ox;by=oy;}
      }
    }
    return {bx,by,best};
  }
  const alt=altScore(bgB,bg.w,bg.h,cw,ch,pzM,pzE,minY);

  // mode picks the channel: 'ncc' for the dimmed gaps the fixtures use,
  // 'white' to force the whitening channel, anything else to decide from the
  // image itself.
  // The whitening score separates cleanly by gap type: painted-white gaps
  // score 1.9-2.3, dimmed or untouched ones 0.6-1.2. The NCC score cannot be
  // used for this -- white_c scored 0.9976 while sitting 55px off.
  let targetX, method;
  if(mode==='ncc') { targetX=found.bx; method='gray-ncc'; }
  else if(mode==='white') { targetX=alt.bx; method='white-silhouette'; }
  else if(alt.bx>15&&alt.best>=1.3) { targetX=alt.bx; method='white-silhouette'; }
  else { targetX=found.bx; method='gray-ncc'; }
  if(targetX<=15) {
    const sx=new Float64Array(bg.w*bg.h);
    for(let y=1;y<bg.h-1;y++) for(let x=1;x<bg.w-1;x++) {
      sx[y*bg.w+x]=
        -bgB[(y-1)*bg.w+x-1]+bgB[(y-1)*bg.w+x+1]
        -2*bgB[y*bg.w+x-1]+2*bgB[y*bg.w+x+1]
        -bgB[(y+1)*bg.w+x-1]+bgB[(y+1)*bg.w+x+1];
    }
    const ys=Math.floor(bg.h*.1),ye=Math.floor(bg.h*.9);
    const columns=new Float64Array(bg.w);
    for(let x=1;x<bg.w-1;x++) {
      let sum=0;
      for(let y=ys;y<ye;y++) sum+=Math.abs(sx[y*bg.w+x]);
      columns[x]=sum;
    }
    let bx=30,best=0;
    for(let x=30;x<bg.w-30;x++) {
      if(columns[x]>best){best=columns[x];bx=x;}
    }
    targetX=bx;
    method='gap-fallback';
  }
  const bgRect=bgImg.getBoundingClientRect();
  const sliderRect=slider.getBoundingClientRect();
  const scale=bgRect.width/bg.w;
  const offset=minX||0;
  const displayLeft=(targetX-offset)*scale;
  return {
    targetX,score:alt.bx>15?alt.best:found.best,
    altX:alt.bx,altScore:alt.best,nccX:found.bx,nccScore:found.best,
    method,
    displayLeft:displayLeft<10?10:displayLeft,
    sliderX:sliderRect.x,sliderY:sliderRect.y,
    sliderW:sliderRect.width,sliderH:sliderRect.height,
    bgDisplayW:bgRect.width,bgNaturalW:bg.w,viewW:window.innerWidth
  };
}
"""


async def widget_state(page) -> dict:
    return await page.evaluate(
        """() => ({
            img: !!document.querySelector('#aliyunCaptcha-img'),
            puzzle: !!document.querySelector('#aliyunCaptcha-puzzle'),
            slider: !!document.querySelector('#aliyunCaptcha-sliding-slider'),
            body: !!document.querySelector('#aliyunCaptcha-captcha-body'),
            wrapper: !!document.querySelector('#aliyunCaptcha-captcha-wrapper')
        })"""
    )


async def is_solved(page) -> bool:
    try:
        wrapper_visible = await page.evaluate(
            """() => {
                const node=document.querySelector('#aliyunCaptcha-captcha-wrapper');
                return node ? node.offsetParent !== null : false;
            }"""
        )
        slider_visible = await page.evaluate(
            """() => {
                const node=document.querySelector('#aliyunCaptcha-sliding-slider');
                return node ? node.offsetParent !== null : false;
            }"""
        )
        if not wrapper_visible or not slider_visible:
            return True
        return bool(
            await page.evaluate(
                """() => {
                    const slider=document.querySelector('#aliyunCaptcha-sliding-slider');
                    const body=document.querySelector('#aliyunCaptcha-captcha-body');
                    const icon=document.querySelector(
                        '#aliyunCaptcha-check-icon,[class*=success],[class*=pass]'
                    );
                    if(icon&&icon.offsetParent!==null)return true;
                    if(slider&&(slider.className.includes('success')||slider.className.includes('pass')))return true;
                    if(body&&(body.className.includes('success')||body.className.includes('pass')))return true;
                    return false;
                }"""
            )
        )
    except Exception:
        return False


async def solve_slider_local(
    page,
    max_attempts: int = 5,
    console: Optional[Console] = None,
    log: Optional[Callable[[str], None]] = None,
    flow_advanced: Optional[Callable[[], Awaitable[bool]]] = None,
) -> bool:
    def emit(message: str) -> None:
        if log:
            log(message)
        if console is not None:
            console.print(" [dim]%s[/]" % message)
        write_log("Captcha: " + message, "INFO")
    barren_waits = 0

    for attempt in range(1, max_attempts + 1):
        emit(f"captcha attempt {attempt}/{max_attempts}")
        try:
            try:
                await page.click("#aliyunCaptcha-captcha-left", timeout=2000)
                await page.wait_for_timeout(1500)
            except Exception:
                pass

            try:
                await page.wait_for_selector(
                    "#aliyunCaptcha-img, #aliyunCaptcha-captcha-wrapper",
                    timeout=CONTAINER_TIMEOUT_MS,
                )
            except Exception:
                if flow_advanced and await flow_advanced():
                    emit("captcha absent; flow advanced")
                    return True
                barren_waits += 1
                emit(f"captcha widget not rendered ({barren_waits}/{MAX_BARREN_WAITS})")
                if barren_waits >= MAX_BARREN_WAITS:
                    return False
                continue

            await page.wait_for_function(READY_JS, timeout=WIDGET_TIMEOUT_MS)
            result = await page.evaluate(SOLVER_JS)
            target_x = result.get("targetX", -1)
            if target_x <= 0:
                emit("captcha target is invalid")
                await page.wait_for_timeout(1200)
                continue

            display_left = result["displayLeft"]
            slider_x = result["sliderX"] + min(
                10.0,
                max(result["sliderW"] / 3, 2.0),
            )
            slider_y = result["sliderY"] + result["sliderH"] / 2
            score = result.get("score", 0.0)
            method = result.get("method", "unknown")
            emit(
                f"captcha target={target_x} score={score:.3f} method={method}"
                f" ncc={result.get('nccX', -1)}({result.get('nccScore', 0):.2f})"
                f" white={result.get('altX', -1)}({result.get('altScore', 0):.2f})"
            )
            # The two channels score on different scales, so a threshold only
            # means something for the channel that produced the target. NCC
            # below 0.6 has been a decoy every time it was checked.
            if method == "gap-fallback":
                emit("captcha used the column-edge fallback; target is unreliable")
            elif method == "gray-ncc" and score < 0.6:
                emit(f"captcha match is weak (score={score:.3f}); likely a decoy")

            # Snapshot the challenge before touching it. A rejected attempt
            # reloads a fresh puzzle within seconds, so archiving on failure
            # would capture the wrong image -- and target identification is
            # the only failure mode left that geometry cannot explain.
            if SNAP_CHALLENGES:
                saved = await _dump_images(
                    page, f"a{attempt}_x{int(target_x)}_s{int(score * 100)}"
                )
                if saved:
                    emit(f"captcha challenge images saved to {saved}")

            await page.mouse.move(slider_x, slider_y)
            await page.wait_for_timeout(80)
            await page.mouse.down()
            await page.wait_for_timeout(60)

            # Probe only to confirm the piece is listening. Do NOT derive a
            # gear ratio from it: the handle has a drag-start deadzone of tens
            # of pixels, so a short probe reads far below 1.0, and dividing by
            # that multiplied the cursor travel until it clamped at the end of
            # the track (target 219 -> landed 260, three times over).
            # The piece follows the cursor 1:1 on this widget; the correction
            # loop below absorbs whatever the deadzone swallowed.
            probe = 24.0
            start_x = await _settled_x(page, tries=8, gap_ms=40)
            await page.mouse.move(slider_x + probe, slider_y, steps=6)
            first = await _settled_x(page, tries=10, gap_ms=40)
            if start_x is None or first is None or first - start_x < 0.5:
                # Short of the deadzone: push further before calling it dead.
                probe = 56.0
                await page.mouse.move(slider_x + probe, slider_y, steps=8)
                first = await _settled_x(page, tries=10, gap_ms=40)

            if start_x is None or first is None or first - start_x < 0.5:
                await page.mouse.up()
                dumped = await _dump_dom(page, "no_follow")
                emit("captcha puzzle did not follow the probe")
                if dumped:
                    emit(f"captcha widget geometry saved to {dumped}")
                await page.wait_for_timeout(1200)
                continue

            view_w = float(result.get("viewW") or 0) or 1600.0
            headroom = view_w - 8.0
            cursor_x = slider_x + probe
            landed = first

            # The solver's offset is already in CSS pixels, so the cursor
            # travels exactly that far. One continuous eased arc, then a few
            # 1:1 corrections.
            target_cursor = min(slider_x + display_left, headroom)
            distance = abs(target_cursor - cursor_x)
            segments = max(4, min(10, int(distance / 28) or 4))
            await _smooth_move(page, cursor_x, target_cursor, slider_y, segments)
            cursor_x = target_cursor
            # The piece keeps moving for a few hundred ms after the cursor
            # stops. Reading too early returned a value ~12px behind, which
            # made the learned offset wrong by exactly that much and sent
            # every trim overshooting to the far side of the gap.
            settled = await _settled_x(page, tries=14, gap_ms=50)
            if settled is not None:
                landed = settled

            # The widget swallows part of a long drag: the same 200px gesture
            # delivered 78-94% of the distance depending on how many pointer
            # samples it saw. Re-issue the shortfall scaled by what it actually
            # delivered, which converges in one extra pass, instead of nudging
            # 7px at a time into a threshold that discards exactly 7px steps.
            if display_left > 40 and landed:
                delivered = landed / display_left
                if 0.5 <= delivered < 0.93:
                    need = (display_left - landed) / delivered
                    extra = min(cursor_x + need, headroom)
                    if extra - cursor_x > 1.0:
                        await _smooth_move(
                            page, cursor_x, extra, slider_y, segments=4
                        )
                        cursor_x = extra
                        settled = await _settled_x(page, tries=14, gap_ms=50)
                        if settled is not None:
                            landed = settled

            stuck = 0
            boost = 1.0
            trims = 0
            trail: list[str] = []
            for _ in range(6):
                error = display_left - landed
                if abs(error) <= 1.0:
                    break
                # The piece trails the cursor by a fixed offset -- the press-in
                # deadzone, spent once when the drag starts. Learn it from
                # where the main drag ended and jump straight to the cursor
                # position that puts the piece on the gap. Walking there in
                # ~7px increments never converged: that is precisely the step
                # size the widget discards, so every trim left the residue at
                # 6-8px and the loop merely burned its six rounds.
                offset = (cursor_x - slider_x) - landed
                if stuck:
                    # The previous step was below the widget's sampling
                    # threshold and got discarded. Repeating the same absolute
                    # target would repeat the same discard, so overshoot by a
                    # growing factor and let the model take over again after
                    # the piece actually moves.
                    next_cursor = min(cursor_x + error * boost, headroom)
                else:
                    next_cursor = min(slider_x + display_left + offset, headroom)
                    if abs(next_cursor - cursor_x) > 3.0 * abs(
                        error
                    ) or abs(next_cursor - cursor_x) < 1.0:
                        # The offset model does not explain this reading; fall
                        # back to stepping by the error itself.
                        next_cursor = min(cursor_x + error, headroom)
                moved_cursor = next_cursor - cursor_x
                if abs(moved_cursor) < 1.0:
                    dumped = await _dump_dom(page, "edge")
                    emit(
                        f"captcha cursor cannot travel further at "
                        f"{landed:.1f}/{display_left:.1f}"
                    )
                    if dumped:
                        emit(f"captcha widget geometry saved to {dumped}")
                    break
                previous = landed
                # One pointermove, not an eased arc. Every event the widget
                # sees adds a small fixed offset on top of the pointer delta,
                # so a 3-event trim of 18px delivered 33px of piece movement
                # (gain 1.83) while an 8-event 238px drag delivered 256px
                # (gain 1.08). Fewer events means the correction lands where
                # it is aimed instead of overshooting to the far side.
                await page.mouse.move(
                    next_cursor, slider_y + (random.random() - 0.5) * 0.6, steps=1
                )
                cursor_x = next_cursor
                trims += 1
                trail.append(f"{error:+.1f}/{landed:.1f}")
                settled = await _settled_x(page, tries=10, gap_ms=50)
                if settled is None:
                    break
                landed = settled
                if abs(landed - previous) < 0.5:
                    stuck += 1
                    if stuck >= 3:
                        dumped = await _dump_dom(page, "stalled")
                        emit(
                            f"captcha piece stopped responding at "
                            f"{landed:.1f}/{display_left:.1f}"
                        )
                        if dumped:
                            emit(f"captcha widget geometry saved to {dumped}")
                        break
                    boost = min(boost * 3.0, 16.0)
                else:
                    stuck = 0
                    boost = 1.0

            await page.mouse.up()
            residual = float("nan") if landed is None else display_left - landed
            if abs(residual) > 2.0 and trail:
                emit("captcha trim trail " + " ".join(trail[-6:]))
            emit(
                f"captcha drag target={display_left:.1f} probe={probe:.0f} "
                f"landed={'-' if landed is None else f'{landed:.1f}'} "
                f"trim={trims} residual={residual:+.1f} "
                f"bgW={result['bgDisplayW']:.0f} handleW={result['sliderW']:.0f}"
            )
            await page.wait_for_timeout(2500)
            if await is_solved(page):
                emit("captcha solved")
                return True
            dumped = await _dump_dom(page, "rejected")
            emit("captcha solution rejected")
            if dumped:
                emit(f"captcha widget geometry saved to {dumped}")
        except Exception as exc:
            emit(f"captcha error {type(exc).__name__}")
        await page.wait_for_timeout(1000)
    return False


# Callers import the geometry dump under this name.
dump_widget_state = widget_state
