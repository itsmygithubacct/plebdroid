#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check a debug build's default desktop across cold startup and sleep/wake.

Requires adb and host Pillow. Stops only Plebdroid, preserves application data,
and leaves the device awake. The display must leave the awake state before the
wake step, and the original process must report that the desktop opened.
Store the output outside the source checkout.
"""
import argparse
import io
import json
from pathlib import Path
import subprocess
import time

from PIL import Image

PACKAGE = 'io.github.itsmygithubacct.plebdroid'
DESKTOP = 'kilix-95'


def display_left_awake(text: str) -> bool:
    """True when every recorded wakefulness value is something other than Awake."""
    values = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('mWakefulness='):
            values.append(stripped.split('=', 1)[1].split()[0].rstrip(','))
    return bool(values) and all(value.lower() != 'awake' for value in values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case', choices=['awake-start', 'asleep-start', 'interrupt-start'],
                        default='asleep-start')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
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

    def pidof():
        return run('shell', 'pidof', PACKAGE, check=False).stdout.decode().strip()

    def remember_pid(pid):
        if 'initial_pid' not in report:
            if not pid:
                raise RuntimeError('Application process did not start')
            report['initial_pid'] = pid
        elif pid != report['initial_pid']:
            raise RuntimeError('Waking or refocusing restarted the application process')

    def launch(wait=True):
        command = ['shell', 'am', 'start']
        if wait:
            command.append('-W')
        command += ['-n', PACKAGE + '/.PlebdroidActivity']
        event('launch', run(*command))
        remember_pid(pidof())

    def require_asleep(label):
        entry = next(item for item in reversed(report['events']) if item['label'] == label)
        if not display_left_awake(entry.get('output', '')):
            report.update(status='failed', failure='Display did not leave the awake state (' + label + ')')
            return False
        return True

    def sleep_until_off(label):
        run('shell', 'input', 'keyevent', 'KEYCODE_SLEEP')
        time.sleep(2)
        event(label, run('shell', 'dumpsys', 'power'))
        return require_asleep(label)

    try:
        event('force-stop-owned-app', run('shell', 'am', 'force-stop', PACKAGE))
        asleep = True
        if args.case == 'asleep-start':
            asleep = sleep_until_off('sleep-power')
            if asleep:
                launch()
        elif args.case == 'interrupt-start':
            wake()
            # Sleep as soon as the process exists. am start -W returns only
            # after startup has finished, which misses a sleep during launch.
            event('launch', run('shell', 'am', 'start', '-n', PACKAGE + '/.PlebdroidActivity'))
            deadline = time.monotonic() + 20
            pid = ''
            while time.monotonic() < deadline and not pid:
                pid = pidof()
                if not pid:
                    time.sleep(0.05)
            remember_pid(pid)
            asleep = sleep_until_off('sleep-power')
        else:
            wake()
            launch()
        if asleep and args.case != 'awake-start':
            time.sleep(15)
            event('before-wake-power', run('shell', 'dumpsys', 'power'))
            asleep = require_asleep('before-wake-power')
            if asleep:
                wake()
                event('wake')
                launch()  # Bring forward the same instance; do not restart it.

        if asleep and report.get('status') != 'failed':
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
        def collect(name):
            try:
                result = run('exec-out', 'run-as', PACKAGE, 'cat', 'files/' + name, check=False)
            except Exception as error:
                report.setdefault('collection_errors', []).append(f'{name}: {error}')
                return None
            destination = args.output / name.replace('/', '-')
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).decode(errors='replace').strip()
                report.setdefault('collection_errors', []).append(
                    f'{name}: exit {result.returncode}: {detail}')
                destination.write_bytes(result.stdout + result.stderr)
                return None
            destination.write_bytes(result.stdout)
            return result.stdout

        engine = collect('engine.log')
        startup_bytes = collect('startup-result.json')
        collect('debian/installed.json')
        if report.get('status') == 'passed':
            if engine is None:
                report.update(status='failed', failure='Engine log was not collected')
            elif not startup_bytes:
                report.update(status='failed', failure='startup-result.json was not collected')
            else:
                try:
                    startup = json.loads(startup_bytes.decode())
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    report.update(status='failed', failure=f'startup-result.json is not JSON: {error}')
                else:
                    report['startup'] = startup
                    recorded = startup.get('pid')
                    opened = (startup.get('status') == 'opened' and startup.get('program') == DESKTOP
                              and report.get('final_pid') == report.get('initial_pid')
                              and (recorded is None or str(recorded) == report.get('initial_pid')))
                    if not opened:
                        report.update(status='failed',
                                      failure='Desktop did not open in the original process: '
                                      + json.dumps(startup))
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
