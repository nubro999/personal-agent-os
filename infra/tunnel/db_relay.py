#!/usr/bin/env python3
"""DB 중계. 서버 맥의 DB 터널을 사내망의 다른 PC 가 비밀번호로 빌려 쓴다.

서버 맥(serve)은 자기 DB 터널(127.0.0.1:13306)을 사내망 포트 하나로 연다. 이 PC(connect)는
127.0.0.1:13306, 13316 에서 받은 연결을 서버 맥으로 넘긴다. 이 PC 의 프로그램은 터널이 열린 것과
똑같이 127.0.0.1 로 DB 에 붙는다.

지키는 것
- 사이 구간은 TLS 로 감싼다. 인증서는 서버 맥이 처음 켤 때 스스로 만들고, 이 PC 는 짝을 지을 때
  본 지문만 믿는다(다른 인증서면 끊는다).
- 연결마다 비밀번호를 먼저 보낸다. 서버 맥은 PBKDF2 해시만 갖고 있다. 틀리면 2초 쉬고 끊고,
  10분 안에 5번 틀린 주소는 15분 막는다.
- --allow 로 받을 주소 대역을 좁힐 수 있다.
- 비밀번호와 키 파일은 ~/.config/db-relay 에 600 으로 둔다.

쓰기
  서버 맥:  python3 db_relay.py set-password      # 비밀번호 정하기(빈 값이면 만들어 한 번 보여 준다)
            python3 db_relay.py serve              # 0.0.0.0:13399 -> 127.0.0.1:13306, 지문을 찍는다
            python3 db_relay.py install-launchd --allow 192.168.0.0/24   # 늘 켜 두기
  이 PC:    python3 db_relay.py pair 192.168.0.20  # 비밀번호 넣고 지문 대조, 저장
            python3 db_relay.py connect            # 127.0.0.1:13306, 13316 을 연다
            python3 db_relay.py check              # 한 번 붙어 보고 끝

표준 라이브러리만 쓴다. macOS 기본 python3(3.9)에서 돈다.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("DB_RELAY_DIR", "~/.config/db-relay")).expanduser()
SERVER_FILE = "server.json"
CLIENT_FILE = "client.json"
CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"

DEFAULT_LISTEN = "0.0.0.0:13399"
DEFAULT_TARGET = "127.0.0.1:13306"
DEFAULT_LOCAL = ["127.0.0.1:13306", "127.0.0.1:13316"]
RELAY_PORT = 13399

MAGIC = b"DBRELAY1 "
MIN_PASSWORD = 12
AUTH_TIMEOUT = 5.0
FAIL_DELAY = 2.0
FAIL_LIMIT = 5
FAIL_WINDOW = 600.0
BAN_SECONDS = 900.0
CHUNK = 65536
CLOSE_GRACE = 30.0
PBKDF2_ROUNDS = 300_000  # macOS 기본 python 은 LibreSSL 이라 scrypt 가 없을 수 있다


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def split_addr(text: str, default_port: int | None = None) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep:
        if default_port is None:
            raise SystemExit(f"주소에 포트가 없다: {text}")
        return text, default_port
    return host, int(port)


def write_private(path: Path, text: str) -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def read_json(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)


def ask_password(from_stdin: bool, allow_generate: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        hint = " (빈 값이면 새로 만든다)" if allow_generate else ""
        password = getpass.getpass(f"비밀번호{hint}: ")
        if password:
            if getpass.getpass("한 번 더: ") != password:
                raise SystemExit("두 번 넣은 비밀번호가 다르다")
    if not password and allow_generate:
        password = secrets.token_urlsafe(18)
        print(f"만든 비밀번호(이 PC 에서 pair 할 때 넣는다. 다시 보여 주지 않는다): {password}")
    if len(password) < MIN_PASSWORD:
        raise SystemExit(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 한다")
    if "\n" in password:
        raise SystemExit("비밀번호에 줄바꿈을 넣을 수 없다")
    return password


def min_tls(context: ssl.SSLContext) -> None:
    try:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1


def fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def tune(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.IncompleteReadError, ssl.SSLError):
        pass
    finally:
        try:
            # 평문 TCP 쪽은 반만 닫아 반대 방향의 남은 응답을 살린다. TLS 쪽은 반만 닫을 수 없다
            if writer.can_write_eof():
                writer.write_eof()
            else:
                writer.close()
        except Exception:
            pass


async def splice(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    """한쪽이 끝나면 반대쪽이 마저 보낼 때까지 조금 기다린 뒤 둘 다 닫는다."""
    tasks = [
        asyncio.ensure_future(pipe(a_reader, b_writer)),
        asyncio.ensure_future(pipe(b_reader, a_writer)),
    ]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    if pending:
        await asyncio.wait(pending, timeout=CLOSE_GRACE)
    for task in tasks:
        task.cancel()
    for writer in (a_writer, b_writer):
        try:
            writer.close()
        except Exception:
            pass


# ── 서버 맥 ──


def cmd_set_password(args: argparse.Namespace) -> None:
    password = ask_password(args.stdin, allow_generate=True)
    salt = secrets.token_bytes(16)
    record = {"salt": salt.hex(), "hash": hash_password(password, salt).hex(), "kdf": "pbkdf2-sha256"}
    write_private(CONFIG_DIR / SERVER_FILE, json.dumps(record))
    print(f"저장했다: {CONFIG_DIR / SERVER_FILE} (해시만 둔다). 돌고 있는 serve 는 다시 켜야 반영된다")


def ensure_cert() -> tuple[Path, Path]:
    cert, key = CONFIG_DIR / CERT_FILE, CONFIG_DIR / KEY_FILE
    if cert.exists() and key.exists():
        return cert, key
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
            "-subj", "/CN=db-relay", "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(key, 0o600)
    return cert, key


class Guard:
    """주소별로 틀린 횟수를 세고, 넘으면 잠시 막는다."""

    def __init__(self) -> None:
        self.fails: dict[str, list[float]] = {}
        self.banned: dict[str, float] = {}

    def blocked(self, ip: str) -> bool:
        until = self.banned.get(ip)
        if until and until > time.monotonic():
            return True
        self.banned.pop(ip, None)
        return False

    def failed(self, ip: str) -> None:
        now = time.monotonic()
        recent = [t for t in self.fails.get(ip, []) if now - t < FAIL_WINDOW] + [now]
        self.fails[ip] = recent
        if len(recent) >= FAIL_LIMIT:
            self.banned[ip] = now + BAN_SECONDS
            self.fails.pop(ip, None)
            log(f"{ip}: {FAIL_LIMIT}번 틀려 {int(BAN_SECONDS // 60)}분 막는다")

    def passed(self, ip: str) -> None:
        self.fails.pop(ip, None)


async def serve(args: argparse.Namespace) -> None:
    record = read_json(SERVER_FILE)
    if not record or record.get("kdf") != "pbkdf2-sha256":
        raise SystemExit("비밀번호가 없다. 먼저: python3 db_relay.py set-password")
    salt, expected = bytes.fromhex(record["salt"]), bytes.fromhex(record["hash"])
    allow = [ipaddress.ip_network(a, strict=False) for a in args.allow]
    target_host, target_port = split_addr(args.target)
    listen_host, listen_port = split_addr(args.listen)
    cert, key = ensure_cert()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    min_tls(context)
    context.load_cert_chain(str(cert), str(key))
    guard = Guard()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = peer[0]
        if guard.blocked(ip) or (allow and not any(ipaddress.ip_address(ip) in n for n in allow)):
            writer.close()
            return
        try:
            line = await asyncio.wait_for(reader.readline(), AUTH_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError, ValueError):
            writer.close()
            return
        if not line.startswith(MAGIC):
            # 비밀번호를 보내지 않은 연결(짝 지을 때 지문만 보는 것 포함)은 세지 않고 끊는다
            writer.close()
            return
        given = line[len(MAGIC) :].rstrip(b"\n")
        ok = bool(given) and hmac.compare_digest(
            hash_password(given.decode("utf-8", "replace"), salt), expected
        )
        if not ok:
            guard.failed(ip)
            log(f"{ip}: 비밀번호 틀림")
            await asyncio.sleep(FAIL_DELAY)
            try:
                writer.write(b"NO\n")
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return
        guard.passed(ip)
        try:
            t_reader, t_writer = await asyncio.open_connection(target_host, target_port)
        except OSError as exc:
            log(f"{ip}: 터널({args.target})에 못 붙음 {exc}")
            writer.write(b"DOWN\n")
            writer.close()
            return
        tune(writer)
        tune(t_writer)
        writer.write(b"OK\n")
        await writer.drain()
        await splice(reader, writer, t_reader, t_writer)

    server = await asyncio.start_server(handle, listen_host, listen_port, ssl=context)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    log(f"중계 시작 {args.listen} -> {args.target}")
    log(f"인증서 지문 {fingerprint(der)}")
    if allow:
        log(f"받는 대역 {', '.join(args.allow)}")
    async with server:
        await server.serve_forever()


# ── 이 PC ──


def client_context() -> ssl.SSLContext:
    # 지문으로 직접 맞춘다. 자체 서명이라 CA 검증은 끈다.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    min_tls(context)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def open_relay(
    mac: str, pin: str, password: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    """(reader, writer, 상태). 상태가 OK 가 아니면 writer 는 닫혀 있다."""
    host, port = split_addr(mac, RELAY_PORT)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=client_context()), AUTH_TIMEOUT
    )
    der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    if pin and fingerprint(der) != pin:
        writer.close()
        return reader, writer, f"지문이 다르다: {fingerprint(der)}"
    writer.write(MAGIC + password.encode() + b"\n")
    await writer.drain()
    reply = await asyncio.wait_for(reader.readline(), AUTH_TIMEOUT + FAIL_DELAY + 1)
    status = reply.strip().decode() or "끊김"
    if status != "OK":
        writer.close()
    return reader, writer, status


def load_client() -> dict:
    record = read_json(CLIENT_FILE)
    if not record:
        raise SystemExit("짝이 없다. 먼저: python3 db_relay.py pair <서버 맥 주소>")
    return record


async def shut(writer: asyncio.StreamWriter) -> None:
    """루프가 끝나기 전에 닫기를 마친다. 안 그러면 3.9 가 닫힌 루프에 SSL 오류를 찍는다."""
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except Exception:
        pass


async def peek_fingerprint(mac: str) -> str:
    host, port = split_addr(mac, RELAY_PORT)
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=client_context()), AUTH_TIMEOUT
    )
    der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    await shut(writer)
    return fingerprint(der)


async def try_password(mac: str, pin: str, password: str) -> str:
    _, writer, status = await open_relay(mac, pin, password)
    await shut(writer)
    return status


def cmd_pair(args: argparse.Namespace) -> None:
    mac = args.mac if ":" in args.mac else f"{args.mac}:{RELAY_PORT}"
    pin = asyncio.run(peek_fingerprint(mac))
    print(f"서버 맥 인증서 지문\n  {pin}")
    if not args.yes:
        answer = input("서버 맥 serve 가 찍은 지문과 같나? [y/N] ").strip().lower()
        if answer != "y":
            raise SystemExit("짝을 짓지 않았다")
    password = ask_password(args.stdin, allow_generate=False)
    status = asyncio.run(try_password(mac, pin, password))
    if status not in ("OK", "DOWN"):
        raise SystemExit(f"붙지 못했다: {status}")
    write_private(
        CONFIG_DIR / CLIENT_FILE, json.dumps({"mac": mac, "fingerprint": pin, "password": password})
    )
    note = "" if status == "OK" else " (비밀번호는 맞다. 서버 맥의 DB 터널이 지금 꺼져 있다)"
    print(f"저장했다: {CONFIG_DIR / CLIENT_FILE}{note}")


def cmd_check(args: argparse.Namespace) -> None:
    record = load_client()

    status = asyncio.run(try_password(record["mac"], record["fingerprint"], record["password"]))
    if status == "OK":
        print(f"OK {record['mac']}")
        return
    raise SystemExit(f"실패 {record['mac']}: {status}")


async def connect(args: argparse.Namespace) -> None:
    record = load_client()
    mac, pin, password = record["mac"], record["fingerprint"], record["password"]

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            m_reader, m_writer, status = await open_relay(mac, pin, password)
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
            log(f"서버 맥({mac})에 못 붙음: {type(exc).__name__} {exc}")
            writer.close()
            return
        if status != "OK":
            log(f"서버 맥이 거절: {status}")
            writer.close()
            return
        tune(writer)
        tune(m_writer)
        await splice(reader, writer, m_reader, m_writer)

    servers = []
    for local in args.listen or DEFAULT_LOCAL:
        host, port = split_addr(local)
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit(f"이 PC 쪽은 루프백에만 연다: {local}")
        servers.append(await asyncio.start_server(handle, host, port))
        log(f"{local} -> {mac}")
    await asyncio.gather(*(s.serve_forever() for s in servers))


LAUNCHD_LABEL = "local.db-relay"


def cmd_install_launchd(args: argparse.Namespace) -> None:
    """서버 맥: 로그인하면 serve 가 저절로 뜨고, 죽으면 다시 뜨게 한다."""
    import plistlib

    if sys.platform != "darwin":
        raise SystemExit("macOS 에서만 쓴다")
    if not read_json(SERVER_FILE):
        raise SystemExit("비밀번호가 없다. 먼저: python3 db_relay.py set-password")
    ensure_cert()
    script = str(Path(__file__).resolve())
    command = [sys.executable, script, "serve", "--listen", args.listen, "--target", args.target]
    for allow in args.allow:
        command += ["--allow", allow]
    log_file = str(Path("~/Library/Logs/db-relay.log").expanduser())
    plist = Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open("wb") as f:
        plistlib.dump(
            {
                "Label": LAUNCHD_LABEL,
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 10,
                "StandardOutPath": log_file,
                "StandardErrorPath": log_file,
            },
            f,
        )
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(plist)], check=True)
    print(f"켰다: {plist}\n로그: {log_file}  (지문은 로그 첫머리에 있다)")
    print(f"끄기: launchctl unload -w {plist}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("set-password", help="서버 맥: 비밀번호 정하기")
    p.add_argument("--stdin", action="store_true", help="비밀번호를 표준 입력 한 줄로 받는다")

    p = sub.add_parser("serve", help="서버 맥: 중계 켜기")
    p.add_argument("--listen", default=DEFAULT_LISTEN)
    p.add_argument("--target", default=DEFAULT_TARGET, help="서버 맥의 DB 터널")
    p.add_argument("--allow", action="append", default=[], help="받을 주소 대역(예: 192.168.0.0/24). 여러 번")

    p = sub.add_parser("install-launchd", help="서버 맥: serve 를 늘 켜 두기")
    p.add_argument("--listen", default=DEFAULT_LISTEN)
    p.add_argument("--target", default=DEFAULT_TARGET)
    p.add_argument("--allow", action="append", default=[])

    p = sub.add_parser("pair", help="이 PC: 서버 맥과 짝 짓기")
    p.add_argument("mac", help="서버 맥 주소(포트를 빼면 13399)")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--yes", action="store_true", help="지문 확인을 묻지 않는다(시험용)")

    sub.add_parser("check", help="이 PC: 한 번 붙어 보기")

    p = sub.add_parser("connect", help="이 PC: 로컬 포트 열기")
    p.add_argument("--listen", action="append", help="기본 127.0.0.1:13306, 127.0.0.1:13316")

    args = parser.parse_args()
    try:
        if args.cmd == "set-password":
            cmd_set_password(args)
        elif args.cmd == "serve":
            asyncio.run(serve(args))
        elif args.cmd == "install-launchd":
            cmd_install_launchd(args)
        elif args.cmd == "pair":
            cmd_pair(args)
        elif args.cmd == "check":
            cmd_check(args)
        elif args.cmd == "connect":
            asyncio.run(connect(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
