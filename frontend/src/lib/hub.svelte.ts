// App-wide reactive state fed by the hub's event stream.
import { api, subscribe, type JobState, type LiveStatus } from './api';

export const hub = $state({
  live: { state: 'idle', detail: '', resolution: null, viewers: 0, running: false } as LiveStatus,
  job: { action: null, running: false, message: '', value: 0, total: 0 } as JobState,
  connected: false,
});

let started = false;

export function startHub() {
  if (started) return;
  started = true;
  api.get<LiveStatus>('/api/live').then((live) => { hub.live = live; }).catch(() => {});
  subscribe((event) => {
    hub.connected = true;
    switch (event.kind) {
      case 'live': {
        const { kind: _, ...live } = event;
        hub.live = live;
        break;
      }
      case 'state': {
        const { kind: _, ...job } = event;
        hub.job = job;
        break;
      }
      case 'message':
        hub.job.message = event.text;
        break;
      case 'progress':
        hub.job.value = event.value;
        hub.job.total = event.total;
        break;
    }
  });
}
