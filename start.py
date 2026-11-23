"""
Container entry point (Dockerfile CMD): `python start.py gunicorn ...`.

Starts the web server (the gunicorn command given on the command line) and the jobs runner
(jobs_worker.py), and supervises them:
  - the runner is restarted when it exits, after 2 s, then 4, 8 ... up to 30 s if it keeps
    dying (the delay resets after 5 minutes alive);
  - if gunicorn exits, the runner is stopped and the container exits, so Railway restarts it;
  - SIGTERM / SIGINT (redeploy) is passed on to both, which end their work cleanly.
"""

import logging
import os
import signal
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdf2wordly.start")

RESTART_MIN = 2
RESTART_MAX = 30
HEALTHY_AFTER = 300
STOP_WAIT = 25


def next_delay(delay, alive_seconds):
    """Backoff for the next runner restart."""
    if alive_seconds >= HEALTHY_AFTER:
        return RESTART_MIN
    return min(RESTART_MAX, max(RESTART_MIN, delay * 2))


class Supervisor:
    def __init__(self, web_cmd, runner_cmd, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
        self.web_cmd = web_cmd
        self.runner_cmd = runner_cmd
        self.popen = popen
        self.clock = clock
        self.sleep = sleep
        self.stopping = False
        self.web = None
        self.runner = None
        self.runner_started = 0.0
        self.delay = RESTART_MIN
        self.restart_at = None
        self.restarts = 0

    def start(self):
        self.web = self.popen(self.web_cmd)
        self._start_runner()

    def _start_runner(self):
        self.runner = self.popen(self.runner_cmd)
        self.runner_started = self.clock()
        self.restart_at = None

    def step(self):
        """One check. Returns an exit code when the container must stop, else None."""
        code = self.web.poll()
        if code is not None:
            logger.error("Web server exited (%s); stopping", code)
            self.stop()
            return code or 1
        now = self.clock()
        if self.restart_at is not None:
            if now >= self.restart_at and not self.stopping:
                self.restarts += 1
                self._start_runner()
            return None
        code = self.runner.poll()
        if code is not None and not self.stopping:
            alive = now - self.runner_started
            self.delay = next_delay(self.delay, alive) if self.restarts else RESTART_MIN
            logger.error("Jobs runner exited (code %s); restarting in %d s", code, self.delay)
            self.restart_at = now + self.delay
        return None

    def stop(self):
        self.stopping = True
        for proc in (self.runner, self.web):
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except OSError:
                    pass
        end = self.clock() + STOP_WAIT
        for proc in (self.runner, self.web):
            if proc is None:
                continue
            try:
                proc.wait(timeout=max(0.1, end - self.clock()))
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv):
    web_cmd = argv[1:]
    if not web_cmd:
        sys.exit("usage: python start.py gunicorn ...")
    here = os.path.dirname(os.path.abspath(__file__))
    sup = Supervisor(web_cmd, [sys.executable, os.path.join(here, "jobs_worker.py")])

    def on_term(_sig, _frame):
        sup.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    sup.start()
    while True:
        code = sup.step()
        if code is not None:
            sys.exit(code)
        time.sleep(1)


if __name__ == "__main__":
    main(sys.argv)
