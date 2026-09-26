// Thin client for the hub API. Authentication is the session cookie set by /api/login.

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

// Called when the hub says the session is gone (signed out, expired, password changed).
let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401 && !path.startsWith('/api/login')) onUnauthorized();
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
  status: 'pending' | 'processing' | 'ready' | 'failed' | string;
  error: string;
  photos: number;
  recorded_at: string | null;
  species: string[];
  duration: number | null;
  /** Best photo of the recording (results path), if any. */
  cover: string | null;
}

export interface Evidence {
  species: string;
  suggestion: string;
  samples: number;
  support: number;
  agreement: number;
  similarity: number;
  margin: number;
  reason: string;
}

export interface Sighting {
  id: string;
  track: number;
  start_ms: number;
  end_ms: number;
  ai_species: string;
  corrected: string | null;
  evidence: Evidence;
  photo: string | null;
  photos: number;
}

export interface Ranked {
  label: string;
  similarity: number;
  score: number;
}

export interface Animal {
  id: number;
  box: [number, number, number, number];
  category: string;
  detection: number;
  ranked: Ranked[];
  tracking_state?: string;
  display_label?: string;
  sighting_id?: string;
  identification_note?: string;
}

export interface Analysis {
  width: number;
  height: number;
  fps: number;
  frames: { ms: number; animals: Animal[] }[];
}

export const UNCERTAIN = 'Uncertain bird';

export interface SpeciesSummary {
  species: string;
  photos: number;
  videos: number;
  featured: number;
}

export interface Photo {
  video_id: string;
  name: string;
  species: string;
  ms: number;
  track: number;
  similarity: number;
  path: string;
  sighting_id: string | null;
  quality: number;
  featured: number;
  start_ms: number | null;
  end_ms: number | null;
  ai_species: string | null;
  corrected: string | null;
  evidence: Evidence | null;
  recorded_at: string | null;
}

/** The sighting a gallery photo belongs to, in the shape the correction dialog uses. */
export function photoSighting(photo: Photo): Sighting | null {
  if (!photo.sighting_id || !photo.evidence) return null;
  return {
    id: photo.sighting_id, track: photo.track, start_ms: photo.start_ms ?? photo.ms, end_ms: photo.end_ms ?? photo.ms,
    ai_species: photo.ai_species ?? photo.species, corrected: photo.corrected, evidence: photo.evidence,
    photo: photo.path, photos: 0,
  };
}

export interface Session {
  authenticated: boolean;
  user: string | null;
  auth_required: boolean;
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

  const retry = () => {
    if (closed) return;
    timer = setTimeout(connect, delay);
    delay = Math.min(delay * 2, 15000);
  };
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
      // A refused handshake looks like any other drop (code 1006), so ask
      // whether we are still signed in before retrying.
      fetch('/api/session')
        .then((response) => response.json() as Promise<Session>)
        .then((session) => {
          if (!session.authenticated) onUnauthorized();
          else retry();
        })
        .catch(retry);
    };
  };
  connect();
  return () => {
    closed = true;
    clearTimeout(timer);
    socket?.close();
  };
}
