"""
Qoder Creator - Captcha Solver
Local slider solver via image processing + mouse drag.

Di-port dari arbilaksmana/qoder-autoreg (captcha-solver.js).
Algoritma: alpha NCC + Canny edges + Sobel gap detection.
"""
import json
import random
from typing import Optional, Dict, Any

from rich.console import Console

from .utils import write_log

# How long to wait for the widget to render before deciding it is not coming.
CONTAINER_TIMEOUT_MS = 6000
WIDGET_TIMEOUT_MS = 12000

# How many barren waits before giving up. Retrying an identical wait never
# helps, so two is already generous.
MAX_BARREN_WAITS = 2

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
  const left = parseFloat(p.style.left);
  if (Number.isFinite(left)) return left;
  const t = getComputedStyle(p).transform;
  const m = /matrix\(([^)]+)\)/.exec(t || '');
  if (m) {
    const v = m[1].split(',').map(Number);
    if (v.length === 6 && Number.isFinite(v[4])) return v[4];
  }
  return null;
}
"""


async def _puzzle_x(page) -> Optional[float]:
    """Puzzle offset in CSS px, or None when it cannot be read."""
    try:
        value = await page.evaluate(PUZZLE_X_JS)
    except Exception:
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _mouse_delta(target: float, observed: float, gain: float) -> float:
    """Mouse movement needed to bring the piece from `observed` to `target`.

    `gain` is measured pixels-of-piece per pixel-of-mouse. It is 1.0 for every
    widget seen so far; the calibration exists so a scaled or transformed
    widget cannot silently break the drag.
    """
    return (target - observed) / max(gain, 0.2)


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

  function blur(data,w,h){const o=new Float64Array(w*h);
    for(let y=1;y<h-1;y++)for(let x=1;x<w-1;x++)o[y*w+x]=(
      data[(y-1)*w+(x-1)]+2*data[(y-1)*w+x]+data[(y-1)*w+(x+1)]+
      2*data[y*w+(x-1)]+4*data[y*w+x]+2*data[y*w+(x+1)]+
      data[(y+1)*w+(x-1)]+2*data[(y+1)*w+x]+data[(y+1)*w+(x+1)])/16;
    return o;}
  const bgB=blur(bgG,bg.w,bg.h), pzB=blur(pzG,cw,ch);

  function ncc(src,sw,sh,tpl,tw,th,mask,maskN,offY){
    let bx=-1,by=-1,bs=-2;
    const y0=Math.max(0,offY-10), y1=Math.min(sh-th,offY+10);
    for(let ox=1;ox<=sw-tw-1;ox++)for(let oy=y0;oy<=y1;oy++){
      let tm=0,sm=0;for(let i=0;i<maskN;i++){if(!mask[i])continue;
        const tx=i%tw,ty=(i/tw)|0;tm+=tpl[i];sm+=src[(oy+ty)*sw+(ox+tx)];}
      tm/=maskN;sm/=maskN;let tv=0,sv=0,cv=0;
      for(let i=0;i<maskN;i++){if(!mask[i])continue;
        const tx=i%tw,ty=(i/tw)|0,td=tpl[i]-tm,sd=src[(oy+ty)*sw+(ox+tx)]-sm;
        tv+=td*td;sv+=sd*sd;cv+=sd*td;}
      tv/=maskN;sv/=maskN;const ts=Math.sqrt(tv),ss=Math.sqrt(sv);
      if(ts<0.001||ss<0.001)continue;const n=cv/(maskN*ss*ts);
      if(n>bs){bs=n;bx=ox;by=oy;}}
    return {bx,by,bs};}

  const nccR=ncc(bgB,bg.w,bg.h,pzB,cw,ch,pzM,maskN,pzMinY);
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
    bgDisplayW: bgRect.width, bgNaturalW: bg.w
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
            # mouse 1:1, so the first move used to be wrong for no reason: it
            # multiplied by (dl / (bgW - 52)) and then by the *handle* width.
            # A 24px probe measures the real gain instead of assuming it.
            start_x = await _puzzle_x(page)
            probe = 24.0
            await page.mouse.move(sx + probe, sy, steps=6)

            gain = 1.0
            observed = await _puzzle_x(page)
            if start_x is not None and observed is not None:
                measured = (observed - start_x) / probe
                if measured <= 0.02:
                    # The handle never took the drag. Release instead of
                    # spending the remaining corrections pushing a dead slider.
                    await page.mouse.up()
                    write_log("Captcha: puzzle did not follow the probe, retrying", "WARNING")
                    await page.wait_for_timeout(1200)
                    continue
                gain = measured

            # Unreadable position: assume the probe moved the piece by its own
            # length, which is what a 1:1 widget does.
            here = observed if observed is not None else probe
            cx = sx + probe + _mouse_delta(dl, here, gain)
            cx = min(cx, sx + res["bgDisplayW"])
            await page.mouse.move(cx, sy, steps=12)
            await page.wait_for_timeout(120)

            # Fine-tune against what the widget reports (tolerance ~1.5px).
            final = None
            for _ in range(8):
                cur = await _puzzle_x(page)
                if cur is None:
                    break
                final = cur
                if abs(cur - dl) <= 1.5:
                    break
                cx += _mouse_delta(dl, cur, gain)
                await page.mouse.move(cx, sy + (random.random() - 0.5) * 1.5, steps=4)
                await page.wait_for_timeout(20)

            await page.mouse.up()
            write_log(
                "Captcha drag: target=%.1f gain=%.2f landed=%s bgW=%.0f handleW=%.0f"
                % (
                    dl,
                    gain,
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