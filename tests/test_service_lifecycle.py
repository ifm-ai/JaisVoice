import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import fcntl
from scripts import run_services as services


def test_lock_waits_for_previous_shutdown(tmp_path):
    path = tmp_path / 'lock'
    owner = subprocess.Popen([sys.executable, '-c', 'import fcntl,sys,time; f=open(sys.argv[1],"w"); fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); time.sleep(.4)', str(path)], stdout=subprocess.PIPE, text=True)
    assert owner.stdout.readline().strip() == 'ready'
    with path.open('a+') as lock:
        assert services.acquire_lock(lock, timeout=2)
    owner.wait()


def test_lock_does_not_steal_live_owner(tmp_path):
    with (tmp_path/'lock').open('a+') as first, (tmp_path/'lock').open('a+') as second:
        fcntl.flock(first, fcntl.LOCK_EX)
        assert not services.acquire_lock(second, timeout=.15)


def test_owner_pipe_eof_stops_supervisor():
    child = subprocess.Popen([sys.executable, '-c', 'from scripts.run_services import watch_owner,STOP; watch_owner(); assert STOP.is_set()'], stdin=subprocess.PIPE)
    assert child.poll() is None
    child.stdin.close()
    assert child.wait(timeout=3) == 0


def test_cleanup_kills_descendant_after_leader_exits(tmp_path):
    pidfile = tmp_path/'pid'
    code = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(\\\"ready\\\",flush=True); time.sleep(60)"],stdout=subprocess.PIPE,text=True); p.stdout.readline(); open(sys.argv[1],"w").write(str(p.pid)); time.sleep(60)'
    leader = subprocess.Popen([sys.executable, '-c', code, str(pidfile)], start_new_session=True)
    try:
        deadline=time.monotonic()+3
        while not pidfile.exists() and time.monotonic()<deadline: time.sleep(.02)
        descendant=int(pidfile.read_text())
        services.stop_processes([('fixture',leader)], grace=.3)
        assert leader.poll() is not None
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            result=subprocess.run(['ps','-o','stat=','-p',str(descendant)],capture_output=True,text=True)
            if not result.stdout.strip() or result.stdout.strip().startswith('Z'): break
            time.sleep(.05)
        else: raise AssertionError('descendant survived cleanup')
    finally:
        try: os.killpg(leader.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        leader.wait()
