#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check a debug build's default desktop across cold startup and sleep/wake.

Requires adb and host Pillow. Stops only Plebdroid, preserves application data,
and leaves the device awake. Store the output outside the source checkout.
"""
import argparse
import io
import json
from pathlib import Path
import subprocess
import time

from PIL import Image

PACKAGE = 'io.github.itsmygithubacct.plebdroid'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case', choices=['awake-start', 'asleep-start', 'interrupt-start'],
                        default='asleep-start')
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    adb = [args.adb, '-s', args.serial]
    report = {'serial': args.serial, 'case': args.case, 'started': time.time(), 'events': []}

    def run(*command, check=True):
        return subprocess.run(adb + list(command), capture_output=True, check=check, timeout=20)

    def save():
        (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')

    def event(label, result=None):
        entry = {'label': label, 'at': time.time()}
        if result is not None:
            output = (result.stdout + result.stderr).decode(errors='replace')
            if label.endswith('power'):
                output = '\n'.join(line for line in output.splitlines() if any(key in line for key in (
                    'mWakefulness=', 'mHalInteractiveModeEnabled=', 'mScreenOffTimeoutSetting=',
                    'mStayOn=', 'mIsInteractive:')))
            entry['output'] = output
        report['events'].append(entry)
        save()

    def wake():
        run('shell', 'input', 'keyevent', 'KEYCODE_WAKEUP')
        run('shell', 'wm', 'dismiss-keyguard')

    def launch():
        event('launch', run('shell', 'am', 'start', '-W', '-n', PACKAGE + '/.PlebdroidActivity'))
        pid = run('shell', 'pidof', PACKAGE, check=False).stdout.decode().strip()
        if 'initial_pid' not in report:
            if not pid:
                raise RuntimeError('Application process did not start')
            report['initial_pid'] = pid
        elif pid != report['initial_pid']:
            raise RuntimeError('Waking or refocusing restarted the application process')

    try:
        event('force-stop-owned-app', run('shell', 'am', 'force-stop', PACKAGE))
        if args.case == 'asleep-start':
            run('shell', 'input', 'keyevent', 'KEYCODE_SLEEP')
            time.sleep(2)
            event('sleep-power', run('shell', 'dumpsys', 'power'))
            launch()
        elif args.case == 'interrupt-start':
            wake()
            launch()
            run('shell', 'input', 'keyevent', 'KEYCODE_SLEEP')
            event('sleep-during-start')
        else:
            wake()
            launch()
        if args.case != 'awake-start':
            time.sleep(15)
            event('before-wake-power', run('shell', 'dumpsys', 'power'))
            wake()
            event('wake')
            launch()  # Bring forward the same instance; do not restart it.

        deadline = time.monotonic() + 70
        best = 0
        while True:
            raw = run('exec-out', 'screencap', '-p').stdout
            frame = Image.open(io.BytesIO(raw)).convert('RGB')
            frame.thumbnail((160, 160))
            teal = sum(r < 12 and abs(g - 128) < 12 and abs(b - 128) < 12
                       for r, g, b in frame.getdata()) / (frame.width * frame.height)
            best = max(best, teal)
            if teal > .25:
                (args.output / 'desktop.png').write_bytes(raw)
                report.update(status='passed', teal_fraction=teal)
                break
            if time.monotonic() >= deadline:
                (args.output / 'failure.png').write_bytes(raw)
                report.update(status='failed', best_teal_fraction=best,
                              failure='The default desktop did not render after wake')
                break
            time.sleep(1)
        pid = run('shell', 'pidof', PACKAGE, check=False)
        event('pid', pid)
        report['final_pid'] = pid.stdout.decode().strip()
        if report['status'] == 'passed' and report['final_pid'] != report['initial_pid']:
            report.update(status='failed', failure='The application process changed during startup')
    except Exception as error:
        report.update(status='error', error=str(error))
    finally:
        for name in ['engine.log', 'startup-result.json', 'debian/installed.json']:
            try:
                result = run('exec-out', 'run-as', PACKAGE, 'cat', 'files/' + name, check=False)
                (args.output / name.replace('/', '-')).write_bytes(result.stdout + result.stderr)
            except Exception as error:
                report.setdefault('collection_errors', []).append(str(error))
        try:
            wake()
        except Exception as error:
            report.update(status='error', cleanup_error=str(error))
        report['finished'] = time.time()
        save()
    print(json.dumps({key: value for key, value in report.items() if key != 'events'}))
    return 0 if report.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
