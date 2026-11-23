#!/usr/bin/env python3
"""S3 읽기 중계. 서버 맥의 AWS 키로 오버마인드 버킷을 읽어 사내망의 다른 PC 에 빌려 준다.

이 PC 는 broker 가 context/(고객사맥락)를 막고(403 only raw/, index/) 자기 AWS 키도 없어서 S3 를 못 읽는다.
서버 맥은 키가 있다. 그래서 서버 맥이 S3 요청을 자기 키로 다시 서명해 넘긴다.

  이 PC  boto3(프로필 ovm-mac, endpoint 127.0.0.1:13380)
     -> db_relay.py connect --mac 192.168.0.20:13398 --listen 127.0.0.1:13380   (TLS, 비밀번호)
     -> 서버 맥 db_relay.py serve --listen 0.0.0.0:13398 --target 127.0.0.1:13380
     -> 서버 맥 s3_proxy.py serve (127.0.0.1:13380) -> 자기 키로 서명 -> S3

지키는 것
- 프록시는 루프백에만 연다. 밖에서는 db_relay 의 TLS 와 비밀번호를 거쳐야 닿는다.
- GET, HEAD 만. 버킷은 generativelab-overmind 하나, 키와 목록 prefix 는 context/ raw/ index/ 만.
- 받은 요청의 서명은 버리고 서버 맥의 키로 새로 서명한다. 키는 밖으로 나가지 않는다.

쓰기 (서버 맥)
  python3 s3_proxy.py check --profile genlab            # 이 키로 context/ raw/ index/ 를 읽을 수 있나
  python3 s3_proxy.py install-launchd --profile genlab  # 프록시와 13398 중계를 늘 켜 두기
  python3 s3_proxy.py serve --profile genlab            # 손으로 켜 보기

키는 AWS CLI 의 `aws configure export-credentials`(SSO, 역할 포함)로 받고, CLI 가 없으면
~/.aws/credentials 의 고정 키를 읽는다. 표준 라이브러리만 쓴다. macOS 기본 python3(3.9)에서 돈다.
"""

from __future__ import annotations

import argparse
import configparser
import datetime
import hashlib
import hmac
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BUCKET = "generativelab-overmind"
REGION = "ap-northeast-2"
ALLOWED = ("context/", "raw/", "index/")
DEFAULT_LISTEN = "127.0.0.1:13380"
RELAY_LISTEN = "0.0.0.0:13398"
PASS_HEADERS = (
    "content-type",
    "content-length",
    "etag",
    "last-modified",
    "content-encoding",
    "x-amz-request-id",
    "x-amz-id-2",
    "x-amz-bucket-region",
)
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
CHUNK = 65536


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


# ── 키 ──


class Credentials:
    """프로필의 키. 만료가 있으면 5분 전에 다시 받는다."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._until = 0.0

    def get(self) -> dict:
        with self._lock:
            if self._value is None or time.time() > self._until:
                self._value, self._until = self._load()
            return self._value

    def _load(self) -> tuple[dict, float]:
        env = os.environ
        if not self.profile and env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
            return (
                {
                    "ak": env["AWS_ACCESS_KEY_ID"],
                    "sk": env["AWS_SECRET_ACCESS_KEY"],
                    "token": env.get("AWS_SESSION_TOKEN", ""),
                },
                time.time() + 3600,
            )
        aws = shutil.which("aws") or next(
            (p for p in ("/opt/homebrew/bin/aws", "/usr/local/bin/aws") if os.path.exists(p)), None
        )
        if aws:
            cmd = [aws, "configure", "export-credentials", "--format", "process"]
            if self.profile:
                cmd += ["--profile", self.profile]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout)
                until = time.time() + 3600
                if d.get("Expiration"):
                    exp = datetime.datetime.fromisoformat(d["Expiration"].replace("Z", "+00:00"))
                    until = exp.timestamp() - 300
                return (
                    {"ak": d["AccessKeyId"], "sk": d["SecretAccessKey"], "token": d.get("SessionToken", "")},
                    until,
                )
        path = Path(env.get("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials")).expanduser()
        cp = configparser.ConfigParser()
        cp.read(path)
        section = self.profile or "default"
        if cp.has_section(section) and cp.has_option(section, "aws_access_key_id"):
            s = cp[section]
            return (
                {
                    "ak": s["aws_access_key_id"],
                    "sk": s["aws_secret_access_key"],
                    "token": s.get("aws_session_token", ""),
                },
                time.time() + 3600,
            )
        raise RuntimeError(f"프로필 '{section}' 의 키를 못 찾았다(aws CLI, {path})")


# ── 서명(SigV4) ──


def quote(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


def canonical_query(raw_query: str) -> str:
    pairs = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
    return "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(pairs))


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, query: str, cred: dict, now: datetime.datetime) -> dict:
    """path 는 이미 인코딩된 경로(/bucket/key), query 는 canonical_query 결과. 보낼 헤더를 돌려준다."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": EMPTY_SHA, "x-amz-date": amz_date}
    if cred.get("token"):
        headers["x-amz-security-token"] = cred["token"]
    names = sorted(headers)
    signed = ";".join(names)
    canonical = "\n".join(
        [method, path, query, "".join(f"{n}:{headers[n]}\n" for n in names), signed, EMPTY_SHA]
    )
    scope = f"{day}/{REGION}/s3/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()]
    )
    k = _hmac(("AWS4" + cred["sk"]).encode("utf-8"), day)
    k = _hmac(_hmac(_hmac(k, REGION), "s3"), "aws4_request")
    signature = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={cred['ak']}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    return headers


# ── 무엇을 넘기나 ──


def judge(method: str, raw_path: str, raw_query: str) -> tuple[str, str] | str:
    """(인코딩된 경로, canonical query) 또는 거절 사유."""
    if method not in ("GET", "HEAD"):
        return "읽기(GET, HEAD)만 넘긴다"
    path = urllib.parse.unquote(raw_path)
    parts = path.lstrip("/").split("/", 1)
    if parts[0] != BUCKET:
        return f"버킷은 {BUCKET} 만"
    key = parts[1] if len(parts) > 1 else ""
    query = dict(urllib.parse.parse_qsl(raw_query, keep_blank_values=True))
    if key:
        if not key.startswith(ALLOWED) or ".." in key.split("/"):
            return f"키는 {', '.join(ALLOWED)} 아래만"
        if set(query) - {"versionId", "partNumber", "response-content-type"}:
            return "객체에는 이 질의를 넘기지 않는다"
    else:
        if query.get("list-type") != "2" and "location" not in query:
            return "버킷에는 목록(list-type=2)과 location 만"
        if "list-type" in query and not query.get("prefix", "").startswith(ALLOWED):
            return f"목록 prefix 는 {', '.join(ALLOWED)} 아래만"
    return "/" + quote(BUCKET + ("/" + key if key else ""), safe="-_.~/"), canonical_query(raw_query)


# ── 서버 ──


def make_handler(cred: Credentials, upstream: str, upstream_tls: bool):
    host = upstream.split("://", 1)[-1]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "s3-proxy"

        def log_message(self, fmt: str, *args) -> None:  # 기본 접근 로그는 끈다
            pass

        def _refuse(self, code: int, why: str) -> None:
            body = (
                '<?xml version="1.0" encoding="UTF-8"?><Error><Code>AccessDenied</Code>'
                f"<Message>s3-proxy: {why}</Message></Error>"
            ).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _forward(self) -> None:
            u = urllib.parse.urlsplit(self.path)
            verdict = judge(self.command, u.path, u.query)
            if isinstance(verdict, str):
                log(f"거절 {self.command} {u.path}?{u.query} — {verdict}")
                self._refuse(403, verdict)
                return
            path, query = verdict
            try:
                headers = sign(self.command, host, path, query, cred.get(), datetime.datetime.now(datetime.timezone.utc))
            except Exception as exc:
                log(f"키 오류: {exc}")
                self._refuse(500, f"서버 맥 키 오류: {exc}")
                return
            if self.headers.get("Range"):
                headers["range"] = self.headers["Range"]
            conn_cls = http.client.HTTPSConnection if upstream_tls else http.client.HTTPConnection
            conn = conn_cls(host, timeout=60)
            try:
                conn.request(self.command, path + ("?" + query if query else ""), headers=headers)
                r = conn.getresponse()
                self.send_response(r.status)
                for name, value in r.getheaders():
                    if name.lower() in PASS_HEADERS:
                        self.send_header(name, value)
                if r.getheader("content-length") is None:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                if self.command != "HEAD":
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                if r.status >= 400:
                    log(f"S3 {r.status} {self.command} {path}?{query}")
            except OSError as exc:
                log(f"S3 에 못 닿음: {exc}")
                self._refuse(502, f"S3 에 못 닿음: {exc}")
            finally:
                conn.close()

        do_GET = _forward
        do_HEAD = _forward

        def _deny(self) -> None:
            # 본문을 읽지 않고 답하므로 연결을 닫는다. 남은 본문이 다음 요청으로 읽히지 않게.
            log(f"거절 {self.command} {self.path} — 읽기만 넘긴다")
            self.close_connection = True
            self._refuse(405, "읽기(GET, HEAD)만 넘긴다")

        do_PUT = do_POST = do_DELETE = do_PATCH = _deny

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    host, port = args.listen.rsplit(":", 1)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"프록시는 루프백에만 연다(밖은 db_relay 가 맡는다): {args.listen}")
    cred = Credentials(args.profile)
    cred.get()  # 켤 때 키부터 확인
    tls = not args.upstream.startswith("http://")
    server = ThreadingHTTPServer((host, int(port)), make_handler(cred, args.upstream, tls))
    log(f"S3 읽기 중계 {args.listen} -> {args.upstream} (프로필 {args.profile or '기본'})")
    server.serve_forever()


def cmd_check(args: argparse.Namespace) -> None:
    """서버 맥에서 이 키로 context/ raw/ index/ 목록을 한 번씩 읽어 본다."""
    cred = Credentials(args.profile)
    c = cred.get()
    host = args.upstream.split("://", 1)[-1]
    bad = 0
    for prefix in ALLOWED:
        verdict = judge("GET", f"/{BUCKET}", f"list-type=2&max-keys=1&delimiter=/&prefix={quote(prefix)}")
        path, query = verdict
        headers = sign("GET", host, path, query, c, datetime.datetime.now(datetime.timezone.utc))
        conn = http.client.HTTPSConnection(host, timeout=30)
        conn.request("GET", f"{path}?{query}", headers=headers)
        r = conn.getresponse()
        body = r.read().decode("utf-8", "replace")
        ok = r.status == 200
        bad += not ok
        print(f"{'OK ' if ok else 'NO '} {prefix:9} {r.status}" + ("" if ok else f"  {body[:200]}"))
    raise SystemExit(1 if bad else 0)


PROXY_LABEL = "local.s3-proxy"
RELAY_LABEL = "local.db-relay-s3"


def _plist(label: str, command: list[str], log_file: str) -> Path:
    import plistlib

    plist = Path(f"~/Library/LaunchAgents/{label}.plist").expanduser()
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open("wb") as f:
        plistlib.dump(
            {
                "Label": label,
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 10,
                "StandardOutPath": log_file,
                "StandardErrorPath": log_file,
                "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
            },
            f,
        )
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(plist)], check=True)
    return plist


def cmd_install_launchd(args: argparse.Namespace) -> None:
    """서버 맥: 프록시(루프백)와 그 앞의 비밀번호 중계(13398)를 늘 켜 둔다."""
    if sys.platform != "darwin":
        raise SystemExit("macOS 에서만 쓴다")
    here = Path(__file__).resolve().parent
    logs = Path("~/Library/Logs").expanduser()
    proxy = [sys.executable, str(here / "s3_proxy.py"), "serve", "--listen", args.listen]
    if args.profile:
        proxy += ["--profile", args.profile]
    relay = [
        sys.executable, str(here / "db_relay.py"), "serve",
        "--listen", args.relay_listen, "--target", args.listen,
    ]
    for allow in args.allow:
        relay += ["--allow", allow]
    p1 = _plist(PROXY_LABEL, proxy, str(logs / "s3-proxy.log"))
    p2 = _plist(RELAY_LABEL, relay, str(logs / "db-relay-s3.log"))
    print(f"켰다: {p1}\n      {p2}\n로그: ~/Library/Logs/s3-proxy.log, db-relay-s3.log")
    print(f"끄기: launchctl unload -w {p1} {p2}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    default_profile = os.environ.get("OVERMIND_AWS_PROFILE", "")
    upstream = f"https://s3.{REGION}.amazonaws.com"

    p = sub.add_parser("serve", help="서버 맥: 프록시 켜기")
    p.add_argument("--listen", default=DEFAULT_LISTEN)
    p.add_argument("--profile", default=default_profile, help="AWS 프로필(비우면 환경변수, default)")
    p.add_argument("--upstream", default=upstream, help="시험용. 기본은 실제 S3")

    p = sub.add_parser("check", help="서버 맥: 이 키로 읽히나 보기")
    p.add_argument("--profile", default=default_profile)
    p.add_argument("--upstream", default=upstream)

    p = sub.add_parser("install-launchd", help="서버 맥: 프록시와 13398 중계를 늘 켜 두기")
    p.add_argument("--profile", default=default_profile)
    p.add_argument("--listen", default=DEFAULT_LISTEN)
    p.add_argument("--relay-listen", default=RELAY_LISTEN)
    p.add_argument("--allow", action="append", default=["192.168.0.0/24"])

    args = parser.parse_args()
    try:
        {"serve": cmd_serve, "check": cmd_check, "install-launchd": cmd_install_launchd}[args.cmd](args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
