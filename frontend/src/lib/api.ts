// Thin client for the hub API. The browser handles HTTP Basic credentials.

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (typeof data?.detail === 'string') message = data.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body: unknown = {}) => request<T>('POST', path, body),
};

export type LiveState = 'idle' | 'starting' | 'waking' | 'signaling' | 'joining' | 'connecting' | 'streaming' | 'ended';

export interface LiveStatus {
  state: LiveState;
  detail: string;
  resolution: string | null;
  viewers: number;
  running: boolean;
}

export interface Camera {
  serial: string;
  name: string;
  model: string | null;
  model_number: string | null;
  online: boolean;
  awake: boolean;
  battery: number | null;
  firmware: string | null;
  recording_resolution: string | null;
  can_watch_live: boolean;
}

export interface Recording {
  id: string;
  name: string;
  status: string;
  recorded_at: string | null;
  species: string[];
}

export interface JobState {
  action: string | null;
  running: boolean;
  message: string;
  value: number;
  total: number;
}

export type HubEvent =
  | ({ kind: 'live' } & LiveStatus)
  | ({ kind: 'state' } & JobState)
  | { kind: 'message'; text: string }
  | { kind: 'progress'; value: number; total: number }
  | { kind: 'changed' };

/** Subscribe to hub events; reconnects with backoff. Returns an unsubscribe function. */
export function subscribe(onEvent: (event: HubEvent) => void): () => void {
  let socket: WebSocket | null = null;
  let closed = false;
  let delay = 1000;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const connect = () => {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${scheme}://${location.host}/api/events`);
    socket.onopen = () => { delay = 1000; };
    socket.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as HubEvent);
      } catch {
        /* ignore malformed events */
      }
    };
    socket.onclose = () => {
      if (closed) return;
      timer = setTimeout(connect, delay);
      delay = Math.min(delay * 2, 15000);
    };
  };
  connect();
  return () => {
    closed = true;
    clearTimeout(timer);
    socket?.close();
  };
}
