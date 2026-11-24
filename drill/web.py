"""Flask drill page for hot rebinding of receive workers.

The page lists the container's usable CPUs, what each sample is currently
bound to (read back from ``/proc`` on every request), accepts exactly one
group of target cores at a time, and displays the kernel readback after
every executed step as well as the rollback trail on failure.
"""

from __future__ import annotations

import json
import os
import secrets

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from . import kernel
from .cpulist import format_cpu_list
from .session import (
    MAX_SAMPLES,
    MIN_SAMPLES,
    BusyError,
    RequestError,
    Session,
    describe_exception,
)

DEFAULT_SAMPLES = int(os.environ.get("DRILL_SAMPLES", "3"))
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(session: Session | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["DRILL_CSRF"] = secrets.token_urlsafe(16)
    if session is None:
        pool = kernel.cpu_pool()
        session = Session(sample_count=DEFAULT_SAMPLES, pool=pool)
    app.extensions["drill"] = session

    def styles() -> str:
        with open(os.path.join(_STATIC_DIR, "drill.css"), "r", encoding="utf-8") as handle:
            return handle.read()

    @app.get("/")
    def index():
        drill: Session = app.extensions["drill"]
        flash = request.args.get("flash", "")
        jobs = [job.as_dict() for job in drill.jobs[-6:]]
        last = jobs[-1] if jobs else None
        return render_template(
            "index.html",
            session=drill,
            samples=drill.samples_state(),
            cores=drill.cores(),
            pool=format_cpu_list(drill.pool),
            jobs=jobs,
            last_job=last,
            flash=flash,
            csrf=app.config["DRILL_CSRF"],
            min_samples=MIN_SAMPLES,
            max_samples=MAX_SAMPLES,
            styles=styles(),
        )

    @app.post("/rebind")
    def rebind():
        drill: Session = app.extensions["drill"]
        form = request.form
        if form.get("csrf") != app.config["DRILL_CSRF"]:
            return redirect(url_for("index", flash="表单令牌失效，请刷新页面后重试"))

        names = sorted(drill.set.samples)
        raw_target: dict[str, str] = {}
        for name in names:
            value = form.get(f"target_{name}", "")
            if str(value).strip():
                raw_target[name] = value

        try:
            fail_at = drill.parse_fail_at(form.get("fail_at"))
            job = drill.submit(raw_target, fail_at=fail_at)
        except (BusyError, RequestError) as exc:
            return redirect(url_for("index", flash=str(exc)))
        except Exception as exc:  # noqa: BLE001 - page must not 500 on operator error
            return redirect(url_for("index", flash=f"执行异常：{describe_exception(exc)}"))

        if job.result == "applied":
            flash = f"第 {job.job_id} 组换绑完成，内核回读一致。"
        elif job.result == "noop":
            flash = f"第 {job.job_id} 组目标与当前一致，无需换绑。"
        elif job.result == "rolled_back":
            flash = f"第 {job.job_id} 组换绑中途失败，已恢复原绑定。原因：{job.error}"
        elif job.result == "rollback_failed":
            flash = f"第 {job.job_id} 组换绑失败且回退未完成，需要人工处理：{job.phase_detail}"
        else:
            flash = f"第 {job.job_id} 组请求未受理：{job.error}"
        return redirect(url_for("index", flash=flash))

    @app.get("/api/state")
    def api_state():
        drill: Session = app.extensions["drill"]
        return jsonify(
            {
                "pool": format_cpu_list(drill.pool),
                "samples": drill.samples_state(),
                "cores": drill.cores(),
                "busy": drill.busy,
                "jobs": [job.as_dict() for job in drill.jobs[-6:]],
            }
        )

    @app.get("/api/samples/<name>")
    def api_sample(name: str):
        """Second read path: ask the sample for its own /proc/self view."""

        drill: Session = app.extensions["drill"]
        sample = drill.set.samples.get(name)
        if sample is None:
            return jsonify({"ok": False, "error": "unknown sample"}), 404
        if drill.executing:
            return jsonify({"ok": False, "error": "换绑执行中，暂不查询"}), 409
        try:
            answer = sample.ask("ping")
        except Exception as exc:  # noqa: BLE001 - report, do not 500
            answer = {"ok": False, "error": describe_exception(exc)}
        try:
            answer["orchestrator_view"] = {
                "cpus": kernel.read_affinity_text(sample.pid, sample.pid),
                "threads": kernel.thread_count(sample.pid),
                "state": kernel.process_state(sample.pid),
            }
        except kernel.ProcessGoneError:
            answer["orchestrator_view"] = {"cpus": "", "threads": 0, "state": "X"}
            answer.setdefault("ok", False)
            answer.setdefault("error", f"{name} 已退出，/proc 中已没有该进程")
        return jsonify(answer), (200 if answer.get("ok") else 503)

    @app.get("/healthz")
    def healthz():
        drill: Session = app.extensions["drill"]
        alive = all(sample.process.poll() is None for sample in drill.set.samples.values() if sample.process)
        return Response(
            json.dumps({"ok": alive, "samples": len(drill.set.samples)}),
            mimetype="application/json",
            status=200 if alive else 503,
        )

    @app.get("/samples/<name>")
    def sample_detail(name: str):
        """Compatibility alias for the JSON sample endpoint."""

        return api_sample(name)

    return app


def _main() -> None:
    app = create_app()
    with app.app_context():
        app.extensions["drill"].start()
    host = os.environ.get("DRILL_HOST", "127.0.0.1")
    port = int(os.environ.get("DRILL_PORT", "8008"))
    debug = os.environ.get("DRILL_DEBUG", "") not in {"", "0", "false"}
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)


if __name__ == "__main__":
    _main()
