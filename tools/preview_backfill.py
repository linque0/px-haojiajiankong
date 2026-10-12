"""只提供扩展弹窗的离线 UI 预览；不启用扩展权限、不连接真实网关或商品网站。"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension/pxb7-extension"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/popup.html"):
            text = (EXT / "popup.html").read_text(encoding="utf-8")
            text = text.replace("</head>", '<script src="preview.js"></script></head>')
            text = text.replace("<body>", '<body><p style="color:#fbbf24">离线界面验收：模拟数据，不访问商品或写入数据库</p>')
            content, mime = text.encode("utf-8"), "text/html"
        elif path in ("/popup.js", "/popup.css", "/preview.js"):
            file = ROOT / "tests/backfill_popup_preview.js" if path == "/preview.js" else EXT / path[1:]
            content = file.read_bytes()
            mime = "text/css" if path.endswith(".css") else "text/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
