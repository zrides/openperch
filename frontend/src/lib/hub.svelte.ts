// App-wide reactive state: the session, and the hub's event stream once signed in.
import { api, setUnauthorizedHandler, subscribe, type JobState, type LiveStatus, type Session } from './api';

const idleLive: LiveStatus = { state: 'idle', detail: '', resolution: null, viewers: 0, running: false };
const idleJob: JobState = { action: null, running: false, message: '', value: 0, total: 0 };

export const hub = $state({
  session: null as Session | null, // null until the first check completes
  live: { ...idleLive },
  job: { ...idleJob },
});

let unsubscribe: (() => void) | null = null;

function startEvents() {
  if (unsubscribe) return;
  api.get<LiveStatus>('/api/live').then((live) => { hub.live = live; }).catch(() => {});
  unsubscribe = subscribe((event) => {
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

function stopEvents() {
  unsubscribe?.();
  unsubscribe = null;
  hub.live = { ...idleLive };
  hub.job = { ...idleJob };
}

function signedOut() {
  stopEvents();
  hub.session = { authenticated: false, user: null, auth_required: true };
}

export async function checkSession() {
  try {
    const response = await fetch('/api/session');
    hub.session = (await response.json()) as Session;
  } catch {
    hub.session = { authenticated: false, user: null, auth_required: true };
  }
  if (hub.session.authenticated) startEvents();
  else stopEvents();
}

export async function signIn(username: string, password: string, remember: boolean) {
  await api.post('/api/login', { username, password, remember });
  await checkSession();
}

export async function signOut() {
  try {
    await api.post('/api/logout');
  } finally {
    signedOut();
  }
}

setUnauthorizedHandler(() => {
  if (hub.session?.authenticated) signedOut();
});
