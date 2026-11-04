"""Local mock of the Qoder signup surface, used to exercise the automation code
without touching qoder.com or any live anti-bot system.

Serves:
  GET  /users/sign-up                           3-step SPA (form -> password -> otp)
  POST /api/verify                              validates the typed OTP
  POST /api/v1/me/personal-access-tokens        returns a 201 + fake PAT
  GET  /download                                landing page after success
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OTP = "482913"
PAT = "pt-" + "Zx9" * 30  # 92 chars
SUBMITTED = []  # every code the page was asked to verify, in order
PAT_REJECTIONS = 0  # set >0 to answer 401 that many times (session lag race)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sign up</title>
<style>body{font-family:sans-serif;padding:40px}input{margin:6px;padding:6px}
#aliyunCaptcha-captcha-body{width:140px;height:44px;border:1px solid #ccc}</style></head>
<body>
<h2>Create your account</h2>
<div id="stage"></div>
<script>
var STEPS = {
 1: '<input id="basic_firstName" placeholder="First name"><br>'
   +'<input id="basic_lastName" placeholder="Last name"><br>'
   +'<input id="basic_email" placeholder="Email"><br>'
   +'<label><input type="checkbox"> I agree</label><br>'
   +'<button type="button" onclick="go(2)">Continue</button>',
 2: '<input id="basic_password" type="password" placeholder="Password"><br>'
   +'<button type="button" onclick="go(3)">Continue</button>',
 3: '<span style="cursor:pointer" onclick="go(2)">&lsaquo; Go back</span>'
   +'<div id="aliyunCaptcha-captcha-wrapper">'
   +'<div id="aliyunCaptcha-captcha-body"><button type="button">Click to verify</button></div></div>'
   +'<p>Enter the 6-digit code sent to your inbox</p>'
   +'<input class="ant-otp-input" maxlength="1" inputmode="numeric">'
   +'<input class="ant-otp-input" maxlength="1" inputmode="numeric">'
   +'<input class="ant-otp-input" maxlength="1" inputmode="numeric">'
   +'<input class="ant-otp-input" maxlength="1" inputmode="numeric">'
   +'<input class="ant-otp-input" maxlength="1" inputmode="numeric">'
   +'<input class="ant-otp-input" maxlength="1" inputmode="numeric">'
};
var state = {};
function go(n){
  // remember what was typed in the previous step
  state.first = state.first || (document.getElementById('basic_firstName')||{}).value;
  state.last  = state.last  || (document.getElementById('basic_lastName')||{}).value;
  state.email = state.email || (document.getElementById('basic_email')||{}).value;
  state.pw    = (document.getElementById('basic_password')||{}).value || state.pw;
  document.getElementById('stage').innerHTML = STEPS[n];
  if(n===3){
    var all = document.querySelectorAll('.ant-otp-input');
    all.forEach(function(el,i){
      el.addEventListener('input', function(){
        if(el.value && all[i+1]) all[i+1].focus();
        // The live screen posts as soon as the sixth digit lands and has no
        // submit button, so the mock behaves the same way.
        var code = Array.prototype.map.call(all, function(e){return e.value}).join('');
        if(code.length === 6) verify();
      });
    });
    if(all[0]) all[0].focus();
  }
}
function verify(){
  var code = Array.prototype.map.call(
    document.querySelectorAll('.ant-otp-input'), function(e){return e.value}).join('');
  fetch('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:code})})
   .then(function(r){return r.json()})
   .then(function(d){ if(d.ok){ location.href='/download'; }
     else {
       var old = document.getElementById('err');
       if(old) old.remove();
       document.getElementById('stage').insertAdjacentHTML('beforeend',
         '<p id="err">Verification code is invalid or incorrect. Try again. ('+code+')</p>');
     } });
}
document.getElementById('stage').innerHTML = STEPS[1];
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/users/sign-up"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/download"):
            self._send(200, "<html><body><h1>Download Qoder</h1></body></html>",
                       "text/html; charset=utf-8")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        if self.path.startswith("/api/verify"):
            code = payload.get("code")
            SUBMITTED.append(code)
            ok = code == OTP
            self._send(200, json.dumps({"ok": ok}))
        elif self.path.startswith("/api/v1/me/personal-access-tokens"):
            global PAT_REJECTIONS
            print("  [mock] PAT request payload:", payload)
            if PAT_REJECTIONS > 0:
                PAT_REJECTIONS -= 1
                print(f"  [mock] PAT -> 401 (session not verified yet, "
                      f"{PAT_REJECTIONS} rejection(s) left)")
                self._send(401, json.dumps({
                    "errorCode": "Unauthorized",
                    "errorMessage": "User not authenticated",
                }))
                return
            self._send(201, json.dumps({"token": PAT, "name": payload.get("name")}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def start(port=8799):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    s = start()
    print(f"mock qoder on http://127.0.0.1:8799 (OTP={OTP})")
    threading.Event().wait()
