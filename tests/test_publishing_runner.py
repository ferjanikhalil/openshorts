"""The publisher entrypoint: does it assemble, and does it stay slim?

Two things are worth a test here, and neither is business logic.

First, the refusals. A publisher that boots with publishing disabled reports
itself up while every loop sleeps, and the symptom — posts that never go out —
is indistinguishable from the symptom the whole split deployment exists to fix.

Second, the import isolation. The entrypoint exists so an always-on host can run
the schedule clock without torch, mediapipe, ultralytics or faster-whisper. That
is an import-graph property, and import graphs regress silently: one convenient
``from app import ...`` would make the slim image unbuildable, and nothing else
in the suite would notice.
"""
import importlib
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Valid base64 for 32 bytes — validate_required decodes it.
_MASTER_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_MIN_ENV = {
    "PUBLISHING_ENABLED": "1",
    "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:5432/none",
    "PUBLISHING_MASTER_KEY": _MASTER_KEY,
}


def _served_paths(app) -> set:
    """Fully-prefixed paths the app actually serves.

    Read from the OpenAPI schema rather than walked off ``app.routes``, because
    FastAPI 0.136 defers inclusion behind an ``_IncludedRouter`` wrapper whose
    ``original_router`` holds the sub-router *unprefixed*. Walking it yields
    ``/webhook/{provider_name}`` — a path nothing responds to — which is exactly
    the kind of half-truth a wiring test must not assert on. Building the schema
    resolves every prefix the way a request would.
    """
    return set(app.openapi().get("paths", {}))


@pytest.fixture
def load_runner(monkeypatch):
    """Import publishing.runner under a controlled env, then unload it.

    Unloading matters: the module builds its app at import time and defaults
    PUBLISHING_ROLE, so a cached copy would leak that into later tests.
    """
    def _load(**over):
        # Cleared first so an explicit override survives; the runner is supposed
        # to default this, and the test for that default has to start from unset.
        monkeypatch.delenv("PUBLISHING_ROLE", raising=False)
        env = dict(_MIN_ENV)
        env.update(over)
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        sys.modules.pop("publishing.runner", None)
        return importlib.import_module("publishing.runner")

    yield _load
    sys.modules.pop("publishing.runner", None)


class TestRefusesUselessBoots:
    def test_no_publishing_flag_is_a_hard_error(self, load_runner):
        with pytest.raises(RuntimeError, match="PUBLISHING_ENABLED"):
            load_runner(PUBLISHING_ENABLED=None)

    def test_a_missing_master_key_is_a_hard_error(self, load_runner):
        # Deferred to config.validate_required via setup_sync. Booting without it
        # would defer the failure to the first submit, where an unreadable
        # credential looks exactly like a revoked provider key.
        with pytest.raises(RuntimeError, match="PUBLISHING_MASTER_KEY"):
            load_runner(PUBLISHING_MASTER_KEY=None)

    def test_a_missing_database_url_is_a_hard_error(self, load_runner):
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            load_runner(DATABASE_URL=None)


class TestAssembly:
    def test_it_serves_the_routes_a_publisher_needs(self, load_runner):
        runner = load_runner()
        paths = _served_paths(runner.app)
        # Health, so the container healthcheck and the operator have an answer.
        assert "/api/publishing/health" in paths
        # The webhook, which is the second reason to run this on a public host: a
        # callback that lands is the difference between a confirmed post and one
        # that ages into `unknown`, and `unknown` is never auto-retried.
        assert "/api/publishing/webhook/{provider_name}" in paths

    def test_it_declares_itself_a_publisher(self, load_runner):
        runner = load_runner()
        assert os.environ["PUBLISHING_ROLE"] == "publisher"
        assert runner.publishing.settings.role == "publisher"

    def test_an_explicit_role_is_left_alone(self, load_runner):
        # setdefault, not assignment: this same image is a valid second replica of
        # a host that DOES hold clips, and relabelling it would hide a genuinely
        # missing resolver behind the publisher exemption in api.health.
        runner = load_runner(PUBLISHING_ROLE="full")
        assert runner.publishing.settings.role == "full"

    def test_the_health_route_reports_ok_without_a_clip_resolver(self, load_runner):
        # The whole point of the role flag. Without it this host reports unhealthy
        # for its entire life and sends the operator hunting a phantom.
        import asyncio

        from publishing import api, clips
        load_runner()
        assert not clips.has_resolver(), "no clips live on a publisher"
        payload = asyncio.run(api.health())
        assert payload["role"] == "publisher"
        assert payload["clip_resolver_registered"] is False
        # `ok` still tracks real misconfiguration; only the resolver is excused.
        assert payload["ok"] == (not payload["warnings"])


class TestImportIsolation:
    """A subprocess, because in-process checks cannot prove absence.

    Another test in this session may already have imported app or torch, so
    ``"torch" in sys.modules`` would be answering the wrong question. A fresh
    interpreter answers the real one: what does importing the entrypoint pull in?
    """

    def test_the_entrypoint_pulls_in_no_video_pipeline(self):
        heavy = ("app", "main", "torch", "ultralytics", "mediapipe",
                 "faster_whisper", "cv2", "yt_dlp")
        # A sentinel-delimited line, because the child also prints publishing's
        # boot warnings to stdout and a bare strip() would read those as leaks.
        code = (
            "import sys; import publishing.runner; "
            f"print('<<<' + ','.join(m for m in {heavy!r} if m in sys.modules)"
            " + '>>>')"
        )
        env = dict(os.environ)
        env.update(_MIN_ENV)
        env["PUBLISHING_ROLE"] = "publisher"
        # Encoding is explicit because publishing's boot lines carry emoji: the
        # child writes them as UTF-8 (runner._make_stdio_utf8_safe), and a parent
        # decoding with the Windows default would choke on its own fix.
        proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                              env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
        assert proc.returncode == 0, proc.stderr[-2000:]
        marked = [line for line in proc.stdout.splitlines()
                  if line.startswith("<<<") and line.endswith(">>>")]
        assert len(marked) == 1, f"no sentinel in output: {proc.stdout[-500:]}"
        leaked = marked[0][3:-3]
        assert not leaked, (
            f"publishing.runner imported {leaked}. The publisher image installs "
            "none of that, so this import would crash the always-on host at boot "
            "— see deploy/publisher/requirements.txt.")
