"""Vercel Serverless Function 入口點。"""

from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    """處理簡單健康檢查請求。"""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.end_headers()
        response = '{"status": "ok", "app": "AutoTok", "message": "AutoTok Service is running"}'
        self.wfile.write(response.encode("utf-8"))
