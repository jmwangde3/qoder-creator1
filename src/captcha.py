"""
Qoder Creator - Captcha Solver
Local slider solver via image processing + mouse drag.

Di-port dari arbilaksmana/qoder-autoreg (captcha-solver.js).
Algoritma: alpha NCC + Canny edges + Sobel gap detection.
"""
import json
import pathlib
import random
import time
from typing import Optional, Dict, Any

from rich.console import Console

from .utils import write_log

# How long to wait for the widget to render before deciding it is not coming.
CONTAINER_TIMEOUT_MS = 6000
WIDGET_TIMEOUT_MS = 12000

# How many barren waits before giving up. Retrying an identical wait never
# helps, so two is already generous.
MAX_BARREN_WAITS = 2

# The piece animates after each mouse move, so every read has to wait for the
# transform to stop changing before the position is trustworthy.
SETTLE_TRIES = 5
SETTLE_GAP_MS = 25

# The widget is usable only once both images decoded and the slider has width.
READY_JS = (
    "() => { const i=document.querySelector('#aliyunCaptcha-img');"
    " const s=document.querySelector('#aliyunCaptcha-sliding-slider');"
    " return !!(i&&i.complete&&i.naturalWidth>0&&s&&s.getBoundingClientRect().width>0); }"
)

# The captcha is optional: the flow may have advanced past this step.
# Only the OTP inputs count — #basic_password belongs to the step *before*
# the captcha, so testing for it would call a stuck page "advanced".
ADVANCED_JS = "() => !!document.querySelector('input.ant-otp-input')"

# Current offset of the puzzle piece, in CSS pixels.
#
# The previous code read `#aliyunCaptcha-puzzle.style.left` only. When the
# widget positions the piece with a transform instead, parseFloat('') is NaN,
# the code fell back to 0 and every correction step assumed the piece had never
# moved — so it kept dragging right until the mouse hit the screen edge. That is
# why a single bad guess turned into five identical failures.
PUZZLE_X_JS = r"""
() => {
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  if (!p) return null;

  // Read the *target* offset, not the rendered one. getComputedStyle()
  // returns the interpolated transform while the widget's CSS transition is
  // still running, so a mid-drag read reports the piece further behind than
  // it will end up, and dividing by that ratio oversizes every correction.
  // The inline style is what the widget set for this pointer position.
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

  const inlineTf = parse(p.style.transform);
  const inlineLeft = parseFloat(p.style.left);
  const hasLeft = Number.isFinite(inlineLeft);

  // `left` is often the stale 0 the widget parks there while it animates the
  // transform, so the transform wins only when it actually moved. A zero on
  // both is still a valid reading: the piece sits at the start.
  if (inlineTf !== null && Math.abs(inlineTf) > 0.01) return inlineTf;
  if (hasLeft && Math.abs(inlineLeft) > 0.01) return inlineLeft;
  if (inlineTf !== null) return inlineTf;
  if (hasLeft) return inlineLeft;

  return parse(getComputedStyle(p).transform);
}
"""


async def _puzzle_x(page) -> Optional[float]:
    """Puzzle offset in CSS px, or None when it cannot be read."""
    try:
        value = await page.evaluate(PUZZLE_X_JS)
    except Exception:
        return None
    return float(value) if isinstance(value, (int, float)) else None


async def _settled_x(
    page, tries: int = SETTLE_TRIES, gap_ms: int = SETTLE_GAP_MS
) -> Optional[float]:
    """Puzzle offset once the widget stops animating it."""
    last = None
    for _ in range(tries):
        value = await _puzzle_x(page)
        if value is not None:
            if last is not None and abs(value - last) <= 0.5:
                return value
            last = value
        await page.wait_for_timeout(gap_ms)
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
      parent: el.parentElement
        ? (el.parentElement.id || el.parentElement.tagName) : null
    };
  };
  const nodes = {};
  document.querySelectorAll('[id^="aliyunCaptcha"]').forEach((el) => {
    nodes[el.id] = pick(el);
  });
  const img = document.querySelector('#aliyunCaptcha-img');
  return {
    nodes,
    view: {w: window.innerWidth, h: window.innerHeight},
    imgNatural: img ? {w: img.naturalWidth, h: img.naturalHeight} : null
  };
}
"""


async def _dump_dom(page, tag: str) -> Optional[str]:
    """Persist the widget's real geometry so a mismatch can be inspected."""
    try:
        payload = await page.evaluate(DUMP_JS)
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
    landed within 1px was still rejected.
    """
    for index in range(1, segments + 1):
        fraction = index / segments
        eased = 1.0 - (1.0 - fraction) ** 2
        x = from_x + (to_x - from_x) * eased
        await page.mouse.move(x, y + (random.random() - 0.5) * 1.4, steps=2)
        await page.wait_for_timeout(gap_ms)


async def _flow_advanced(page) -> bool:
    """True when the page already sits on a later step than the captcha."""
    try:
        return bool(await page.evaluate(ADVANCED_JS))
    except Exception:
        return False


async def dump_widget_state(page) -> str:
    """Which captcha/step elements exist right now — logged when things go wrong."""
    try:
        state = await page.evaluate(
            """() => {
                const q = s => !!document.querySelector(s);
                return {
                    url: location.href.slice(0, 100),
                    img: q('#aliyunCaptcha-img'),
                    puzzle: q('#aliyunCaptcha-puzzle'),
                    slider: q('#aliyunCaptcha-sliding-slider'),
                    body: q('#aliyunCaptcha-captcha-body'),
                    wrapper: q('#aliyunCaptcha-captcha-wrapper'),
                    otp: q('input.ant-otp-input'),
                    pwd: q('#basic_password')
                };
            }"""
        )
        return json.dumps(state, ensure_ascii=False)
    except Exception as e:
        return f"<state dump failed: {e}>"


# ==================== LOCAL SLIDER SOLVER JS ====================
SOLVER_JS = r"""
async () => {
  const bgImg = document.querySelector('#aliyunCaptcha-img');
  const puzzleImg = document.querySelector('#aliyunCaptcha-puzzle');
  const slider = document.querySelector('#aliyunCaptcha-sliding-slider');
  if (!bgImg || !puzzleImg || !slider) return {targetX:-1, error:'missing-elements'};

  async function getPixels(img) {
    const res = await fetch(img.src, { signal: AbortSignal.timeout(10000) });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const tmp = new Image();
    await new Promise((r,j) => { tmp.onload=r; tmp.onerror=j; tmp.src=url; });
    const c = document.createElement('canvas');
    c.width = tmp.naturalWidth; c.height = tmp.naturalHeight;
    c.getContext('2d').drawImage(tmp,0,0);
    const id = c.getContext('2d').getImageData(0,0,c.width,c.height);
    URL.revokeObjectURL(url);
    return {w:c.width,h:c.height,d:id.data};
  }

  const bg = await getPixels(bgImg);
  const pz = await getPixels(puzzleImg);

  // 1) puzzle alpha bbox
  let pzMinX=pz.w,pzMaxX=0,pzMinY=pz.h,pzMaxY=0;
  for (let y=0;y<pz.h;y++) for (let x=0;x<pz.w;x++) {
    if (pz.d[(y*pz.w+x)*4+3] > 128) {
      if (x<pzMinX)pzMinX=x; if(x>pzMaxX)pzMaxX=x;
      if (y<pzMinY)pzMinY=y; if(y>pzMaxY)pzMaxY=y;
    }
  }
  const cw=pzMaxX-pzMinX+1, ch=pzMaxY-pzMinY+1;
  if (cw<=0||ch<=0) return {targetX:-1, error:'empty-puzzle'};
  const pzG=new Float64Array(cw*ch), pzM=new Uint8Array(cw*ch);
  let maskN=0;
  for (let cy=0;cy<ch;cy++) for (let cx=0;cx<cw;cx++) {
    const si=((pzMinY+cy)*pz.w+(pzMinX+cx))*4, di=cy*cw+cx;
    const a=pz.d[si+3]; pzM[di]=a>128?1:0; if(a>128)maskN++;
    pzG[di]=pz.d[si]*0.299+pz.d[si+1]*0.587+pz.d[si+2]*0.114;
  }
  const bgG=new Float64Array(bg.w*bg.h);
  for (let i=0;i<bg.w*bg.h;i++) bgG[i]=bg.d[i*4]*0.299+bg.d[i*4+1]*0.587+bg.d[i*4+2]*0.114;

  // Erode the mask by one pixel. A real piece's outermost ring is
  // alpha-blended, and the hole's rim still shows the untouched photo, so
  // those pixels disagree by construction and pull the correlation toward a
  // decoy one piece-width away. Dropping the ring costs ~12% of the samples
  // and removes that failure mode entirely.
  const pzE=new Uint8Array(cw*ch);
  let validN=0;
  for (let y=0;y<ch;y++) for (let x=0;x<cw;x++) {
    const i=y*cw+x;
    if(!pzM[i]||x===0||y===0||x===cw-1||y===ch-1) continue;
    let solid=1;
    for(let dy=-1;dy<=1&&solid;dy++) for(let dx=-1;dx<=1;dx++) {
      if(!pzM[i+dy*cw+dx]){solid=0;break;}
    }
    if(solid){pzE[i]=1;validN++;}
  }
  if (validN<64) return {targetX:-1, error:'degenerate-mask'};

  function blur(data,w,h){const o=new Float64Array(w*h);
    // Edge-replicating. The greedy alpha bbox means the outer ring of `data`
    // IS the silhouette the mask selects; letting it decay to zero would
    // replace the strongest evidence with near-black.
    for(let y=0;y<h;y++){const ym=y>0?y-1:0,yp=y<h-1?y+1:h-1;
      for(let x=0;x<w;x++){const xm=x>0?x-1:0,xp=x<w-1?x+1:w-1;
        o[y*w+x]=(
          data[ym*w+xm]+2*data[ym*w+x]+data[ym*w+xp]+
          2*data[y*w+xm]+4*data[y*w+x]+2*data[y*w+xp]+
          data[yp*w+xm]+2*data[yp*w+x]+data[yp*w+xp])/16;}}
    return o;}
  const bgB=blur(bgG,bg.w,bg.h), pzB=blur(pzG,cw,ch);

  function ncc(src,sw,sh,tpl,tw,th,mask,maskN,offY){
    let bx=-1,by=-1,bs=-2;
    const y0=Math.max(0,offY-10), y1=Math.min(sh-th,offY+10);
    // maskN counts opaque pixels; the mask spans tw*th slots and the opaque
    // ones are scattered through it, so the scan must cover the whole array.
    // Stopping at maskN drops the piece's lower rows and skews both means.
    const span=mask.length;
    for(let ox=1;ox<=sw-tw-1;ox++)for(let oy=y0;oy<=y1;oy++){
      let tm=0,sm=0;for(let i=0;i<span;i++){if(!mask[i])continue;
        const tx=i%tw,ty=(i/tw)|0;tm+=tpl[i];sm+=src[(oy+ty)*sw+(ox+tx)];}
      tm/=maskN;sm/=maskN;let tv=0,sv=0,cv=0;
      for(let i=0;i<span;i++){if(!mask[i])continue;
        const tx=i%tw,ty=(i/tw)|0,td=tpl[i]-tm,sd=src[(oy+ty)*sw+(ox+tx)]-sm;
        tv+=td*td;sv+=sd*sd;cv+=sd*td;}
      tv/=maskN;sv/=maskN;const ts=Math.sqrt(tv),ss=Math.sqrt(sv);
      if(ts<0.001||ss<0.001)continue;const n=cv/(maskN*ss*ts);
      if(n>bs){bs=n;bx=ox;by=oy;}}
    return {bx,by,bs};}

  const nccR=ncc(bgB,bg.w,bg.h,pzB,cw,ch,pzE,validN,pzMinY);
  let targetX = nccR.bx;
  // fallback: gap via column edge sum (Sobel X)
  if (targetX<=15) {
    const sx=new Float64Array(bg.w*bg.h);
    for(let y=1;y<bg.h-1;y++)for(let x=1;x<bg.w-1;x++)sx[y*bg.w+x]=
      -bgB[(y-1)*bg.w+(x-1)]+bgB[(y-1)*bg.w+(x+1)]-2*bgB[y*bg.w+(x-1)]+2*bgB[y*bg.w+(x+1)]-bgB[(y+1)*bg.w+(x-1)]+bgB[(y+1)*bg.w+(x+1)];
    const yS=Math.floor(bg.h*0.1),yE=Math.floor(bg.h*0.9),col=new Float64Array(bg.w);
    for(let x=1;x<bg.w-1;x++){let s=0;for(let y=yS;y<yE;y++)s+=Math.abs(sx[y*bg.w+x]);col[x]=s;}
    let bx=30,bsm=0;for(let x=30;x<bg.w-30;x++)if(col[x]>bsm){bsm=col[x];bx=x;}
    targetX=bx;
  }

  const bgRect=bgImg.getBoundingClientRect(), sRect=slider.getBoundingClientRect();
  const scale=bgRect.width/bg.w;
  const pzOff=pzMinX||0;
  const displayLeft=(targetX-pzOff)*scale;
  return {
    targetX, score:nccR.bs, method: targetX<=15?'gap-fallback':'gray-ncc',
    displayLeft: displayLeft<10?10:displayLeft,
    sliderX:sRect.x, sliderY:sRect.y, sliderW:sRect.width, sliderH:sRect.height,
    bgDisplayW: bgRect.width, bgNaturalW: bg.w, viewW: window.innerWidth
  };
}
"""


async def solve_slider_local(page, max_attempts: int = 5, console: Console = None) -> bool:
    """Solve the Aliyun slider captcha locally via image processing + drag.

    Returns True when the captcha is solved *or* was not required (the flow
    already advanced). Returns False only when the widget should be there but
    never became usable.

    Note on the wait: the original code waited 15s per attempt and retried all
    5 times even when the widget had not rendered at all — that burned 75s and
    then reported "captcha failed" for a step that never needed solving.
    """
    c = console or Console()
    barren_waits = 0

    for attempt in range(1, max_attempts + 1):
        c.print(f"    [dim]captcha attempt {attempt}/{max_attempts}[/]", end="")
        write_log(f"Captcha local attempt {attempt}/{max_attempts}", "INFO")
        try:
            # Expand puzzle jika collapsed
            try:
                await page.click("#aliyunCaptcha-captcha-left", timeout=2000)
                await page.wait_for_timeout(2500)
            except Exception:
                pass

            # Tahap 1: apakah widget-nya ada sama sekali?
            try:
                await page.wait_for_selector(
                    "#aliyunCaptcha-img, #aliyunCaptcha-captcha-wrapper",
                    timeout=CONTAINER_TIMEOUT_MS,
                )
            except Exception:
                state = await dump_widget_state(page)

                if await _flow_advanced(page):
                    c.print(" [yellow]not required, flow already advanced[/]")
                    write_log(f"Captcha absent, flow advanced: {state}", "INFO")
                    return True

                barren_waits += 1
                c.print(f" [red]widget never rendered ({barren_waits}/{MAX_BARREN_WAITS})[/]")
                write_log(f"Captcha widget missing: {state}", "WARNING")
                if barren_waits >= MAX_BARREN_WAITS:
                    c.print()
                    write_log("Giving up on captcha after repeated empty waits", "ERROR")
                    return False
                continue

            # Tahap 2: gambar sudah ter-decode dan slider punya lebar?
            await page.wait_for_function(READY_JS, timeout=WIDGET_TIMEOUT_MS)

            # Jalankan solver JS di browser
            res = await page.evaluate(SOLVER_JS)
            if res.get("targetX", -1) <= 0:
                c.print(" [yellow]invalid, retrying...[/]")
                await page.wait_for_timeout(1500)
                continue

            dl = res["displayLeft"]
            sx = res["sliderX"] + min(10.0, max(res["sliderW"] / 3, 2.0))
            sy = res["sliderY"] + res["sliderH"] / 2

            c.print(f" targetX={res['targetX']}, score={res.get('score',0):.3f}" +
              f" [dim]({res.get('method','?')})[/]")

            await page.mouse.move(sx, sy)
            await page.wait_for_timeout(80)
            await page.mouse.down()
            await page.wait_for_timeout(40)

            # `displayLeft` is already in CSS pixels and the piece follows the
            # mouse 1:1, so the cursor travels exactly that far. Earlier builds
            # multiplied by (dl / (bgW - 52)) and then by the *handle* width.
            # Probe only to confirm the piece is listening. Do NOT derive a
            # gear ratio from it: the handle has a drag-start deadzone of tens
            # of pixels, so a short probe reads far below 1.0, and dividing by
            # that multiplied the cursor travel until it clamped at the end of
            # the track (target 219 -> landed 260, three times over).
            # `displayLeft` is already CSS pixels and the piece follows 1:1;
            # the correction loop absorbs whatever the deadzone swallowed.
            probe = 24.0
            start_x = await _settled_x(page, tries=8, gap_ms=40)
            await page.mouse.move(sx + probe, sy, steps=6)
            first = await _settled_x(page, tries=10, gap_ms=40)
            if start_x is None or first is None or first - start_x < 0.5:
                # Short of the deadzone: push further before calling it dead.
                probe = 56.0
                await page.mouse.move(sx + probe, sy, steps=8)
                first = await _settled_x(page, tries=10, gap_ms=40)

            if start_x is None or first is None or first - start_x < 0.5:
                # The handle never took the drag. Release instead of spending
                # the remaining corrections pushing a dead slider.
                await page.mouse.up()
                dumped = await _dump_dom(page, "no_follow")
                write_log("Captcha: puzzle did not follow the probe, retrying", "WARNING")
                if dumped:
                    write_log("Captcha: widget geometry saved to %s" % dumped, "INFO")
                await page.wait_for_timeout(1200)
                continue

            view_w = float(res.get("viewW") or 0) or 1600.0
            headroom = view_w - 8.0
            cx = sx + probe
            final = first

            # One continuous eased arc to the computed offset, then a few 1:1
            # corrections. Reading between every step produced an evenly spaced
            # move-pause pattern that landed within 1px and was still rejected.
            target_cx = min(sx + dl, headroom)
            distance = abs(target_cx - cx)
            segments = max(6, min(18, int(distance / 18) or 6))
            await _smooth_move(page, cx, target_cx, sy, segments)
            cx = target_cx
            settled = await _settled_x(page, tries=8, gap_ms=30)
            if settled is not None:
                final = settled

            stuck = 0
            for _ in range(6):
                error = dl - final
                if abs(error) <= 1.5:
                    break
                next_cx = min(cx + error, headroom)
                moved_cursor = next_cx - cx
                if abs(moved_cursor) < 1.0:
                    dumped = await _dump_dom(page, "edge")
                    write_log(
                        "Captcha: cursor cannot travel further at %.1f/%.1f"
                        % (final, dl),
                        "WARNING",
                    )
                    if dumped:
                        write_log("Captcha: widget geometry saved to %s" % dumped, "INFO")
                    break
                previous = final
                await _smooth_move(page, cx, next_cx, sy, segments=4, gap_ms=14)
                cx = next_cx
                settled = await _settled_x(page, tries=6, gap_ms=30)
                if settled is None:
                    break
                final = settled
                if abs(final - previous) < 0.5:
                    stuck += 1
                    if stuck >= 2:
                        dumped = await _dump_dom(page, "stalled")
                        write_log(
                            "Captcha: piece stopped responding at %.1f/%.1f"
                            % (final, dl),
                            "WARNING",
                        )
                        if dumped:
                            write_log(
                                "Captcha: widget geometry saved to %s" % dumped,
                                "INFO",
                            )
                        break
                else:
                    stuck = 0

            await page.mouse.up()
            write_log(
                "Captcha drag: target=%.1f probe=%.0f landed=%s bgW=%.0f handleW=%.0f"
                % (
                    dl,
                    probe,
                    "-" if final is None else "%.1f" % final,
                    res["bgDisplayW"],
                    res["sliderW"],
                ),
                "INFO",
            )
            await page.wait_for_timeout(2500)

            # Cek apakah solved
            if await _is_solved(page):
                c.print(" [green]solved![/]")
                write_log("Captcha solved locally", "SUCCESS")
                return True

            dumped = await _dump_dom(page, "rejected")
            write_log("Captcha solution rejected", "WARNING")
            if dumped:
                write_log("Captcha: widget geometry saved to %s" % dumped, "INFO")
            c.print(" [red]miss, retrying...[/]", end="")
        except Exception as e:
            c.print(f" [red]err: {str(e)[:60]}[/]")
            write_log(f"Captcha local error: {e}", "WARNING")

        await page.wait_for_timeout(1200)

    return False


async def _is_solved(page) -> bool:
    """Deteksi apakah captcha sudah solved."""
    try:
        # Check body text
        body = await page.evaluate(
            "() => document.body ? document.body.innerText.toLowerCase().substring(0,500) : ''"
        )
        if "verified" in body or "success" in body:
            return True

        # Check wrapper visibility — jika hidden, captcha solved
        wv = await page.evaluate(
            "() => { const w=document.querySelector('#aliyunCaptcha-captcha-wrapper');"
            " return w ? w.offsetParent !== null : false; }"
        )
        if not wv:
            return True

        # Check slider — jika hilang setelah solve, captcha solved
        slider_visible = await page.evaluate(
            "() => { const s=document.querySelector('#aliyunCaptcha-sliding-slider');"
            " return s && s.offsetParent !== null; }"
        )
        if not slider_visible:
            return True

        # Check success class on slider/captcha-body
        cls = await page.evaluate(
            "() => {"
            " const s=document.querySelector('#aliyunCaptcha-sliding-slider');"
            " const c=document.querySelector('#aliyunCaptcha-captcha-body');"
            " const ci=document.querySelector('#aliyunCaptcha-check-icon, [class*=success], [class*=pass]');"
            " if(ci&&ci.offsetParent!==null)return true;"
            " if(s&&(s.className.includes('success')||s.className.includes('pass')))return true;"
            " if(c&&(c.className.includes('success')||c.className.includes('pass')))return true;"
            " return false; }"
        )
        return bool(cls)
    except Exception:
        return False