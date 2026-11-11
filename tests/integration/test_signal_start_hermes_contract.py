"""Optional cross-repo acceptance: real Engine HTTP and isolated Telegram HTTP.

Set HERMES_CONTROL_PLANE_ROOT to the matching Hermes branch. No production data,
profile, model or external destination is accessed by this test.
"""

import ast
import importlib.util
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest
import uvicorn
from fastapi import Depends, FastAPI
from sqlalchemy import text

from app.api.deps import get_engine, require_auth
from app.api.signal_start import router
from app.storage.sqlite_store import SQLiteStore
from config import settings

HCP = os.environ.get("HERMES_CONTROL_PLANE_ROOT")
pytestmark = pytest.mark.skipif(not HCP, reason="matching Hermes checkout not configured")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def engine_http(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("isolated Engine server failed")
            time.sleep(0.01)
        assert server.started
        yield address
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def test_real_http_detection_delivery_failure_loss_restart_resolution_and_recurrence(
    tmp_path,
    monkeypatch,
):
    scripts = Path(HCP) / "gateway/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    observer = load("cross_observer", scripts / "signal_start_observer.py")
    sender = load("cross_sender", scripts / "send_telegram.py")
    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "isolated-engine-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "isolated-telegram-token")
    monkeypatch.setenv("CRON_TG_CHAT_ID", "isolated-chat")
    store = SQLiteStore(f"sqlite:///{tmp_path}/source.db")
    a, b, c = [
        store.save_signal(original_product_id=None, title=f"Manual {i}", raw_content="Body")
        for i in range(3)
    ]
    failed = store.create_run("example", b)
    with store._engine.begin() as conn:
        conn.execute(text("UPDATE signals SET status='new', ingested_at='2020-01-01'"))
        conn.execute(
            text("UPDATE signals SET ingested_at=datetime('now') WHERE signal_id=:id"), {"id": c}
        )
        conn.execute(
            text(
                "UPDATE workflow_runs SET lifecycle='done', outcome='failed', "
                "created_at='2020-01-01' WHERE run_id=:id"
            ),
            {"id": failed},
        )
    app = FastAPI()
    app.include_router(router, dependencies=[Depends(require_auth)])
    engine = SimpleNamespace(store=store)
    app.dependency_overrides[get_engine] = lambda: engine
    mode, attempts, accepted = ["reject"], [], []

    class Telegram(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            attempts.append(message)
            if mode[0] == "reject":
                self.send_response(429)
                self.end_headers()
                self.wfile.write(b'{"ok":false}')
                return
            accepted.append(len(accepted) + 1)
            if mode[0] == "lost":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps({"ok": True, "result": {"message_id": accepted[-1]}}).encode()
            )

    tg = ThreadingHTTPServer(("127.0.0.1", 0), Telegram)
    tg_thread = threading.Thread(target=tg.serve_forever)
    tg_thread.start()

    def local_wire(req, **kw):
        assert req.full_url == "https://api.telegram.org/botisolated-telegram-token/sendMessage"
        return urlopen(
            Request(
                f"http://127.0.0.1:{tg.server_port}/send",
                data=req.data,
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            **kw,
        )

    monkeypatch.setattr(sender, "urlopen", local_wire)
    state = tmp_path / "observer-state"
    try:
        with engine_http(app) as base:

            def fetch():
                return observer.fetch_status(base, "isolated-engine-token", 1, 6)

            def sweep():
                return observer.observe(state, fetch, sender.send_recovery_once)

            first = fetch()
            assert first["counts"] == {"stranded": 2, "abandoned": 1, "eligible": 2}
            # Execute the unchanged legacy SQL against this same nonempty source.
            # Grace is verified separately; these are raw class-membership queries.
            legacy = (scripts / "run-signal-start-reconciler.sh").read_text()
            inline = legacy.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
            queries = [
                node.args[0].value
                for node in ast.walk(ast.parse(inline))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ]
            assert len(queries) == 2
            with store._engine.connect() as connection:
                old = [{row[0] for row in connection.exec_driver_sql(query)} for query in queries]
            for kind, expected in zip(("stranded", "abandoned"), old, strict=True):
                assert {
                    item["signal_id"] for item in first["items"] if item["classification"] == kind
                } == expected
            assert sweep()["outstanding"] == 1 and len(attempts) == 1 and not accepted
            pending = json.loads((state / "signal-start-delivery.json").read_text())["events"][0]
            assert pending["state"] == "pending"
            assert not (state / "signal-start-reconciler.json").exists()
            mode[0] = "accept"
            assert sweep()["outstanding"] == 0 and len(attempts) == 2
            assert sweep()["outstanding"] == 0 and len(attempts) == 2
            ledger = json.loads((state / "signal-start-reconciler.json").read_text())
            assert set(ledger["alerted"]) == {a, b}
            confirmed = json.loads((state / "signal-start-delivery.json").read_text())["events"][0]
            assert confirmed["id"] == pending["id"] and confirmed["attempts"] == 2

            # Stranded -> abandoned inside the longer grace must not resolve.
            recent_failed = store.create_run("example", a)
            with store._engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE workflow_runs SET lifecycle='done', outcome='failed' "
                        "WHERE run_id=:id"
                    ),
                    {"id": recent_failed},
                )
                conn.execute(text("UPDATE signals SET status='new' WHERE signal_id=:id"), {"id": a})
            assert sweep()["outstanding"] == 0 and len(attempts) == 2
            assert a in json.loads((state / "signal-start-reconciler.json").read_text())["alerted"]

            # Real auth failure cannot clear the compatibility receipt.
            assert (
                observer.observe(
                    state,
                    lambda: observer.fetch_status(base, "wrong", 1, 6),
                    sender.send_recovery_once,
                )["outstanding"]
                == 1
            )
            assert len(attempts) == 2
            active_a = store.create_run("example", a)
            store.create_run("example", b)
            mode[0] = "lost"
            assert sweep()["outstanding"] == 1 and len(attempts) == 3 and len(accepted) == 2
            restarted = load("restarted_cross_observer", scripts / "signal_start_observer.py")
            assert restarted.observe(state, fetch, sender.send_recovery_once)["outstanding"] == 1
            assert len(attempts) == 3
            event = json.loads((state / "signal-start-delivery.json").read_text())["events"][-1]
            assert event["state"] == "unknown"
            restarted.resolve(
                state,
                event["id"],
                "confirmed",
                message_id=str(accepted[-1]),
                note="Matched isolated destination receipt",
            )
            assert sweep()["outstanding"] == 0 and len(attempts) == 3
            assert json.loads((state / "signal-start-reconciler.json").read_text())["alerted"] == {}

            # Once all attempts fail again and the grace passes, a new episode alerts.
            with store._engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE workflow_runs SET lifecycle='done', outcome='failed', "
                        "created_at='2020-01-01' WHERE signal_id=:sid"
                    ),
                    {"sid": a},
                )
                conn.execute(
                    text("UPDATE signals SET status='new' WHERE signal_id=:sid"), {"sid": a}
                )
            assert active_a
            mode[0] = "accept"
            assert sweep()["outstanding"] == 0 and len(attempts) == 4
            assert sweep()["outstanding"] == 0 and len(attempts) == 4
            assert set(
                json.loads((state / "signal-start-reconciler.json").read_text())["alerted"]
            ) == {a}
    finally:
        tg.shutdown()
        tg_thread.join(10)
        tg.server_close()
