"""ყალბი "rs.ge" პორტალი ტესტებისთვის (localhost). იმეორებს ნაკადს და არა რეალურ DOM-ს:
login -> (SMS კოდი) -> დეკლარაციების სია -> ახალი/დრაფტი -> ფორმა -> შენახვა -> გაგზავნა.

სცენარები: otp, captcha, prefilled (rs.ge-მ ფორმა ანგარიშ-ფაქტურებიდან შეავსო), already_submitted.
"""

from __future__ import annotations

import html
import itertools
import threading
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TYPE_LABELS = {
    "vat": "დღგ",
    "income_tax_withholding": "საშემოსავლო (წყაროსთან)",
    "profit_tax": "მოგების გადასახადი",
    "small_business": "მცირე ბიზნესი",
}


@dataclass
class Declaration:
    id: int
    type: str
    period: str
    status: str = "დრაფტი"
    values: dict[str, str] = field(default_factory=dict)
    receipt: str | None = None


@dataclass
class FakePortalState:
    username: str = "405123456"
    password: str = "correct-horse"
    otp_code: str | None = "482913"
    captcha: bool = False
    prefill: dict[str, str] = field(default_factory=dict)
    declarations: dict[int, Declaration] = field(default_factory=dict)
    login_attempts: int = 0
    otp_attempts: int = 0
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def add(self, type_: str, period: str, status: str = "დრაფტი") -> Declaration:
        d = Declaration(next(self._ids), type_, period, status)
        d.values = {f"line_{i}": self.prefill.get(f"line_{i}", "") for i in range(1, 6)}
        if status == "წარდგენილი":
            d.receipt = f"DKL-{d.id:06d}"
        self.declarations[d.id] = d
        return d


def _page(body: str) -> bytes:
    return f"""<!doctype html><html lang="ka"><head><meta charset="utf-8"><title>fake rs.ge</title></head>
<body>{body}</body></html>""".encode()


class _Handler(BaseHTTPRequestHandler):
    state: FakePortalState  # დაყენდება კლასის შექმნისას

    def log_message(self, *args):  # ტესტებში ხმაური არ გვინდა
        pass

    # ---------------------------------------------------------- utils
    def _cookies(self) -> dict[str, str]:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return {k: v.value for k, v in c.items()}

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        data = parse_qs(self.rfile.read(length).decode())
        return {k: v[0] for k, v in data.items()}

    def _send(self, body: bytes, status: int = 200, cookies: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in (cookies or {}).items():
            self.send_header("Set-Cookie", f"{k}={v}; Path=/; HttpOnly")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, cookies: dict[str, str] | None = None):
        self.send_response(303)
        for k, v in (cookies or {}).items():
            self.send_header("Set-Cookie", f"{k}={v}; Path=/; HttpOnly")
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authed(self) -> bool:
        return self._cookies().get("session") == "ok"

    # ---------------------------------------------------------- pages
    def _login_page(self, error: str = "") -> bytes:
        captcha = '<div class="g-recaptcha">captcha</div>' if self.state.captcha else ""
        err = f'<div class="alert-danger">{error}</div>' if error else ""
        return _page(f"""<h1>შესვლა</h1>{err}{captcha}
<form method="post" action="/login">
  <input name="username"><input type="password" name="password">
  <button type="submit">შესვლა</button>
</form>""")

    def _otp_page(self, error: str = "") -> bytes:
        err = f'<div class="otp-error">{error}</div>' if error else ""
        return _page(f"""<p>კოდი გაიგზავნა ნომერზე ***-**-12</p>{err}
<form method="post" action="/otp"><input name="otp" autocomplete="one-time-code">
<button type="submit">დადასტურება</button></form>""")

    def _nav(self) -> str:
        return '<nav><a href="/declarations">დეკლარაციები</a> <a href="/logout">გასვლა</a></nav>'

    def _list_page(self) -> bytes:
        rows = "".join(
            f"<tr><td>{TYPE_LABELS[d.type]}</td><td>{d.period}</td><td>{d.status}</td>"
            f'<td><a href="/declarations/{d.id}">გახსნა</a></td></tr>'
            for d in self.state.declarations.values()
        )
        return _page(f"""{self._nav()}<a href="/declarations/new">ახალი დეკლარაცია</a>
<table class="declarations"><thead><tr><th>ტიპი</th><th>პერიოდი</th><th>სტატუსი</th><th></th></tr></thead>
<tbody>{rows}</tbody></table>""")

    def _new_page(self) -> bytes:
        types = "".join(f'<option value="{k}">{v}</option>' for k, v in TYPE_LABELS.items())
        years = "".join(f"<option>{y}</option>" for y in range(2024, 2028))
        months = "".join(f'<option value="{m}">{m:02d}</option>' for m in range(1, 13))
        return _page(f"""{self._nav()}<form method="post" action="/declarations/new">
<select name="declaration_type">{types}</select><select name="year">{years}</select>
<select name="month">{months}</select><button type="submit">გაგრძელება</button></form>""")

    def _form_page(self, d: Declaration, saved: bool) -> bytes:
        inputs = "".join(
            f'<label>სტრ. {i}<input type="number" step="0.01" name="line_{i}" '
            f'value="{html.escape(d.values.get(f"line_{i}", ""))}"></label>'
            for i in range(1, 6)
        )
        note = '<div class="draft-saved">დრაფტი შენახულია</div>' if saved else ""
        return _page(f"""{self._nav()}<h2>{TYPE_LABELS[d.type]} - {d.period}</h2>{note}
<form class="declaration-form" method="post" action="/declarations/{d.id}/save">{inputs}
<button type="submit">შენახვა</button>
<button type="submit" formaction="/declarations/{d.id}/submit"
        onclick="return confirm('ნამდვილად გსურთ დეკლარაციის გაგზავნა?')">გაგზავნა</button>
</form>""")

    # ---------------------------------------------------------- routing
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self._send(self._login_page())
        if not self._authed():
            return self._redirect("/login")
        if path == "/dashboard":
            return self._send(_page(self._nav() + "<h1>მთავარი</h1>"))
        if path == "/declarations":
            return self._send(self._list_page())
        if path == "/declarations/new":
            return self._send(self._new_page())
        if path.startswith("/declarations/"):
            d = self.state.declarations[int(path.split("/")[2])]
            return self._send(self._form_page(d, saved="saved=1" in (urlparse(self.path).query or "")))
        self._send(_page("404"), 404)

    def do_POST(self):
        path = urlparse(self.path).path
        form = self._form()
        if path == "/login":
            self.state.login_attempts += 1
            if self.state.captcha:
                return self._send(self._login_page())
            if form.get("username") != self.state.username or form.get("password") != self.state.password:
                return self._send(self._login_page("არასწორი მომხმარებელი ან პაროლი"))
            if self.state.otp_code:
                return self._send(self._otp_page(), cookies={"pending": "1"})
            return self._redirect("/dashboard", cookies={"session": "ok"})
        if path == "/otp":
            self.state.otp_attempts += 1
            if self._cookies().get("pending") != "1":
                return self._redirect("/login")
            if form.get("otp") != self.state.otp_code:
                return self._send(self._otp_page("კოდი არასწორია"))
            return self._redirect("/dashboard", cookies={"session": "ok", "pending": "0"})
        if not self._authed():
            return self._redirect("/login")
        if path == "/declarations/new":
            period = f"{form['year']}-{int(form['month']):02d}"
            d = self.state.add(form["declaration_type"], period)
            return self._redirect(f"/declarations/{d.id}")
        parts = path.strip("/").split("/")
        d = self.state.declarations[int(parts[1])]
        d.values.update({k: v for k, v in form.items() if k.startswith("line_")})
        if parts[2] == "save":
            return self._redirect(f"/declarations/{d.id}?saved=1")
        if parts[2] == "submit":
            d.status = "წარდგენილი"
            d.receipt = f"DKL-2026-{d.id:06d}"
            return self._send(_page(f"""{self._nav()}<div class="submission-success">დეკლარაცია წარდგენილია.
ნომერი: <span class="receipt-number">{d.receipt}</span></div>"""))


class FakePortal:
    def __init__(self, state: FakePortalState | None = None):
        self.state = state or FakePortalState()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakePortal:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
