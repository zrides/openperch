import json
import tempfile
import threading
import unittest
from pathlib import Path

from kiwibit_hub.scheduler import FetchScheduler, run_forever


class FakeRunner:
    def __init__(self):
        self.running = False
        self.started = []
        self.listeners = []

    def subscribe(self, listener):
        self.listeners.append(listener)

    def start(self, action, ids=()):
        if self.running:
            raise RuntimeError('Another library job is running')
        self.started.append(action)
        self.running = True

    def finish(self, message):
        self.running = False
        for listener in self.listeners:
            listener('state', action='fetch', running=False, message=message)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'fetch-schedule.json'
        self.now = 1000.0
        self.runner = FakeRunner()
        self.scheduler = FetchScheduler(self.runner, self.path, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_fetches_on_the_interval_only_when_idle(self):
        self.assertTrue(self.scheduler.tick(), 'first check fetches right away')
        self.assertEqual(self.runner.started, ['fetch'])
        self.now += 60
        self.assertFalse(self.scheduler.tick(), 'busy')
        self.runner.finish('Finished · library saved.')
        self.assertFalse(self.scheduler.tick(), 'not due yet')
        self.now = 1000 + 30 * 60
        self.assertTrue(self.scheduler.tick())
        self.assertEqual(self.runner.started, ['fetch', 'fetch'])

    def test_skips_while_another_job_runs(self):
        self.runner.running = True  # e.g. processing the library
        self.assertFalse(self.scheduler.tick())
        self.assertEqual(self.runner.started, [])

    def test_manual_fetch_resets_the_schedule(self):
        self.assertTrue(self.scheduler.start('manual'))
        self.runner.finish('Finished · 2 failure(s).')
        self.now += 10 * 60
        self.assertFalse(self.scheduler.tick())
        status = self.scheduler.status()
        self.assertEqual(status['last_trigger'], 'manual')
        self.assertEqual(status['last_message'], 'Finished · 2 failure(s).')
        self.assertEqual(status['next_run'], 1000 + 30 * 60)

    def test_disabled_schedule_never_fetches_and_settings_persist(self):
        self.scheduler.configure(auto=False, interval_minutes=60)
        self.assertFalse(self.scheduler.tick())
        self.assertIsNone(self.scheduler.status()['next_run'])
        reloaded = FetchScheduler(FakeRunner(), self.path, clock=lambda: self.now)
        self.assertEqual((reloaded.settings['auto'], reloaded.settings['interval_minutes']), (False, 60))
        with self.assertRaises(ValueError):
            self.scheduler.configure(interval_minutes=7)

    def test_last_run_survives_restart(self):
        self.scheduler.tick()
        reloaded = FetchScheduler(FakeRunner(), self.path, clock=lambda: self.now + 60)
        self.assertFalse(reloaded.tick(), 'a restart must not trigger an extra fetch')

    def test_corrupt_settings_fall_back_to_defaults(self):
        self.path.write_text('{not json')
        scheduler = FetchScheduler(FakeRunner(), self.path)
        self.assertTrue(scheduler.settings['auto'])
        self.path.write_text(json.dumps({'auto': 'yes', 'interval_minutes': 999}))
        scheduler = FetchScheduler(FakeRunner(), self.path)
        self.assertEqual(scheduler.settings, {'auto': True, 'interval_minutes': 30})

    def test_background_loop_ticks_until_stopped(self):
        stop = threading.Event()
        thread = threading.Thread(target=run_forever, args=(self.scheduler, stop, 0.05))
        thread.start()
        deadline = threading.Event()
        deadline.wait(0.3)
        stop.set()
        thread.join(2)
        self.assertEqual(self.runner.started, ['fetch'])
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
