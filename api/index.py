"""Vercel Serverless Function 入口點。

處理 TikTok OAuth 授權回呼、Token 交換與照片輪播貼文發布測試。
"""

import json
from http.server import BaseHTTPRequestHandler
import urllib.parse
import urllib.request
import urllib.error

CLIENT_KEY = "sbaw5hpgnruu6rk1zy"
CLIENT_SECRET = "0N2ycDTtUESn3Q6eXbptBr0Y7K1LJG6W"
REDIRECT_URI = "https://autotok.carry-digital-nomad.in.net/api/auth/callback"
BASE_DOMAIN = "https://autotok.carry-digital-nomad.in.net"

SLIDES = [
    f"{BASE_DOMAIN}/slides/slide_01.jpg",
    f"{BASE_DOMAIN}/slides/slide_02.jpg",
    f"{BASE_DOMAIN}/slides/slide_03.jpg",
    f"{BASE_DOMAIN}/slides/slide_04.jpg",
    f"{BASE_DOMAIN}/slides/slide_05.jpg",
    f"{BASE_DOMAIN}/slides/slide_06.jpg",
]


class handler(BaseHTTPRequestHandler):
    """處理 TikTok OAuth 與發布測試請求。"""

    def _send_html(self, status_code: int, html_content: str) -> None:
        self.send_response(status_code)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content.encode("utf-8"))

    def _send_json(self, status_code: int, data: dict) -> None:
        self.send_response(status_code)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query_params = urllib.parse.parse_qs(parsed_url.query)
        
        # 考慮 Vercel rewrite 傳入的 __path
        custom_path = query_params.get("__path", [""])[0]
        req_path = f"{path}/{custom_path}"

        # 1. 登入轉址
        if "auth/login" in req_path:
            auth_url = (
                "https://www.tiktok.com/v2/auth/authorize/?"
                + urllib.parse.urlencode({
                    "client_key": CLIENT_KEY,
                    "scope": "user.info.basic,video.publish,video.upload",
                    "response_type": "code",
                    "redirect_uri": REDIRECT_URI,
                    "state": "autotok_auth_state",
                })
            )
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
            return

        # 2. OAuth 回呼處理
        if "auth/callback" in req_path:
            code = query_params.get("code", [None])[0]
            error = query_params.get("error", [None])[0]
            error_desc = query_params.get("error_description", [""])[0]

            if error:
                html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>授權失敗 - AutoTok</title>
<style>body{{font-family:sans-serif;max-width:600px;margin:50px auto;padding:20px;line-height:1.6;}}</style>
</head><body>
<h2 style="color:#e53e3e;">❌ 授權失敗</h2>
<p>錯誤代碼: {error}</p>
<p>錯誤說明: {error_desc}</p>
<p><a href="/api/auth/login">重新授權</a></p>
</body></html>"""
                self._send_html(400, html)
                return

            if not code:
                self._send_html(400, "<h2>未收到授權碼 (Code)</h2>")
                return

            # 向 TikTok 交換 Token
            token_url = "https://open.tiktokapis.com/v2/oauth/token/"
            token_data = urllib.parse.urlencode({
                "client_key": CLIENT_KEY,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
            }).encode("utf-8")

            req = urllib.request.Request(
                token_url,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp_body = resp.read().decode("utf-8")
                    token_json = json.loads(resp_body)
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="ignore")
                self._send_html(500, f"<h2>Token 交換失敗</h2><pre>{err_text}</pre>")
                return
            except Exception as e:
                self._send_html(500, f"<h2>連線錯誤</h2><p>{str(e)}</p>")
                return

            access_token = token_json.get("access_token", "")
            open_id = token_json.get("open_id", "")
            expires_in = token_json.get("expires_in", 0)

            # 授權成功頁面，附帶發布按鈕
            html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>AutoTok - TikTok 授權成功</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 680px; margin: 40px auto; padding: 24px; background: #0f172a; color: #f8fafc; line-height: 1.6; border-radius: 12px; }}
        h1 {{ color: #22c55e; display: flex; align-items: center; gap: 10px; font-size: 24px; }}
        .card {{ background: #1e293b; border-radius: 8px; padding: 20px; margin-bottom: 24px; border: 1px solid #334155; }}
        .token-box {{ word-break: break-all; background: #090d16; padding: 12px; border-radius: 6px; font-family: monospace; font-size: 13px; color: #38bdf8; }}
        button {{ background: #ef4444; color: white; border: none; padding: 14px 28px; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; transition: background 0.2s; }}
        button:hover {{ background: #dc2626; }}
        .preview {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 15px; }}
        .preview img {{ width: 100%; border-radius: 6px; }}
    </style>
</head>
<body>
    <h1>🎉 TikTok 授權成功！</h1>
    <div class="card">
        <h3>授權資訊</h3>
        <p><strong>Open ID:</strong> {open_id}</p>
        <p><strong>有效期限:</strong> {expires_in} 秒</p>
        <p><strong>Access Token:</strong></p>
        <div class="token-box">{access_token}</div>
    </div>

    <div class="card">
        <h3>即將發布的測試照片輪播貼文 (Photo Carousel)</h3>
        <p>標題：<code>AutoTok 自動化發布測試 🚀 #autotok #vietnam #travel</code></p>
        <p>圖片張數：6 張輪播圖</p>
        <div class="preview">
            <img src="{SLIDES[0]}">
            <img src="{SLIDES[1]}">
            <img src="{SLIDES[2]}">
        </div>
        <br>
        <form action="/api/publish" method="POST">
            <input type="hidden" name="token" value="{access_token}">
            <button type="submit">🚀 立即發布一則照片輪播到 @carry.vietnam.tw</button>
        </form>
    </div>
</body>
</html>"""
            self._send_html(200, html)
            return

        # 3. 預設健康檢查
        self._send_json(200, {
            "status": "ok",
            "app": "AutoTok",
            "received_path": self.path,
            "x_matched_path": self.headers.get("x-matched-path", ""),
            "auth_login_url": f"{BASE_DOMAIN}/api/auth/login",
            "callback_url": REDIRECT_URI,
        })

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query_params = urllib.parse.parse_qs(parsed_url.query)
        custom_path = query_params.get("__path", [""])[0]
        req_path = f"{path}/{custom_path}"

        if "publish" in req_path:
            content_length = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_length).decode("utf-8")
            form_data = urllib.parse.parse_qs(post_body)
            token = form_data.get("token", [None])[0]

            if not token:
                self._send_html(400, "<h2>缺少 Access Token</h2>")
                return

            # 呼叫 TikTok Content Posting API 發布相片輪播貼文
            publish_url = "https://open.tiktokapis.com/v2/post/publish/content/init/"
            payload = {
                "media_type": "PHOTO",
                "post_info": {
                    "title": "AutoTok 自動化發布測試 🚀 #autotok #vietnam #travel",
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "auto_add_music": True,
                },
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "photo_cover_index": 1,
                    "photo_images": SLIDES,
                },
            }

            req = urllib.request.Request(
                publish_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
            )

            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    resp_data = resp.read().decode("utf-8")
                    result_json = json.loads(resp_data)
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="ignore")
                self._send_html(500, f"""<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0f172a;color:#fff;padding:40px;">
                    <h2 style="color:#ef4444;">發布失敗</h2><pre style="background:#1e293b;padding:16px;border-radius:8px;">{err_text}</pre>
                </body></html>""")
                return
            except Exception as e:
                self._send_html(500, f"<h2>發布連線錯誤</h2><p>{str(e)}</p>")
                return

            publish_id = result_json.get("data", {}).get("publish_id", "未知")
            html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>AutoTok - 發布成功</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 680px; margin: 40px auto; padding: 24px; background: #0f172a; color: #f8fafc; line-height: 1.6; border-radius: 12px; }}
        h1 {{ color: #22c55e; }}
        .card {{ background: #1e293b; border-radius: 8px; padding: 20px; border: 1px solid #334155; }}
        pre {{ background: #090d16; padding: 14px; border-radius: 6px; color: #38bdf8; overflow-x: auto; }}
        a {{ color: #38bdf8; text-decoration: none; font-weight: bold; }}
    </style>
</head>
<body>
    <h1>🎉 貼文發布請求成功！</h1>
    <div class="card">
        <h3>TikTok 伺服器已成功接收任務</h3>
        <p><strong>Publish ID:</strong> <code>{publish_id}</code></p>
        <p>狀態回傳：</p>
        <pre>{json.dumps(result_json, ensure_ascii=False, indent=2)}</pre>
        <br>
        <p>請打開您的手機 TikTok App 或至帳號查看最新照片輪播貼文！</p>
    </div>
</body>
</html>"""
            self._send_html(200, html)
            return

        self._send_json(404, {"error": "Not Found"})
