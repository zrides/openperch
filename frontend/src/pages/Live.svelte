<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import Icon from '../lib/Icon.svelte';
  import { api, ApiError, type Camera, type Recording } from '../lib/api';
  import { hub } from '../lib/hub.svelte';
  import { connectLive, type LiveConnection } from '../lib/liveConnection';
  import { clock } from '../lib/format';

  const RESOLUTIONS = [
    { value: '1280x720', label: '720p' },
    { value: '1920x1080', label: '1080p' },
    { value: '3840x2160', label: '4K' },
  ];
  const PROGRESS: Record<string, string> = {
    starting: 'Starting live view…',
    waking: 'Waking the camera…',
    signaling: 'Getting a live ticket…',
    joining: 'Joining the camera…',
    connecting: 'Connecting to the camera…',
    streaming: 'Receiving video…',
  };

  type Phase = 'idle' | 'starting' | 'watching';
  let phase = $state<Phase>('idle');
  let resolution = $state(hub.live.resolution ?? '1280x720');
  let connection: LiveConnection | null = null;
  let playing = $state(false);
  let error = $state('');
  let notice = $state('');
  let videoSize = $state('');
  let video: HTMLVideoElement | undefined = $state();
  let frame: HTMLDivElement | undefined = $state();

  let camera = $state<Camera | null>(null);
  let cameraError = $state('');
  let waking = $state(false);
  let recent: Recording[] = $state([]);

  const message = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

  async function loadCamera(refresh = false) {
    try {
      camera = await api.get<Camera>(`/api/camera${refresh ? '?refresh=true' : ''}`);
      cameraError = '';
    } catch (e) {
      cameraError = message(e);
    }
  }

  onMount(() => {
    loadCamera();
    api.get<Recording[]>('/api/recordings?limit=4').then((rows) => { recent = rows; }).catch(() => {});
  });

  function closeConnection() {
    connection?.close();
    connection = null;
    playing = false;
    videoSize = '';
    if (video) video.srcObject = null;
  }

  async function start() {
    error = '';
    notice = '';
    phase = 'starting';
    closeConnection();
    try {
      await api.post('/api/live/start', { resolution });
      connection = await connectLive((state) => {
        if (state === 'failed' && phase === 'watching') {
          closeConnection();
          phase = 'idle';
          error = 'Lost the video connection. If the hub runs in Docker, check that its UDP media port is published.';
        }
      });
      if (video) video.srcObject = connection.stream;
      phase = 'watching';
    } catch (e) {
      closeConnection();
      phase = 'idle';
      error = message(e);
    }
  }

  async function stop() {
    closeConnection();
    phase = 'idle';
    try {
      await api.post('/api/live/stop');
    } catch (e) {
      error = message(e);
    }
  }

  function chooseResolution(value: string) {
    if (value === resolution) return;
    resolution = value;
    if (phase !== 'idle') start();
  }

  // The relay ended on its own: camera left, ticket failed, idle timeout…
  $effect(() => {
    if (phase === 'watching' && hub.live.state === 'ended') {
      closeConnection();
      phase = 'idle';
      error = hub.live.detail ? `Live view ended: ${hub.live.detail}.` : 'Live view ended.';
    }
  });

  onDestroy(closeConnection);

  function stamp() {
    const d = new Date();
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }

  // Recording: the hub saves the clip into the library and identifies its birds.
  const recording = $derived(!!hub.live.recording?.active);
  let recordBusy = $state(false);
  let now = $state(Date.now());
  $effect(() => {
    if (!recording) return;
    const timer = setInterval(() => { now = Date.now(); }, 500);
    return () => clearInterval(timer);
  });
  const recordElapsed = $derived(recording && hub.live.recording?.started ? now - hub.live.recording.started * 1000 : 0);

  async function startRecording() {
    recordBusy = true;
    notice = '';
    try {
      await api.post('/api/live/record');
    } catch (e) {
      notice = message(e);
    } finally {
      recordBusy = false;
    }
  }

  async function stopRecording() {
    recordBusy = true;
    try {
      await api.post('/api/live/record/stop');
    } catch (e) {
      notice = message(e);
    } finally {
      recordBusy = false;
    }
  }

  // Report each finished clip once.
  let lastClip = $state<NonNullable<NonNullable<typeof hub.live.recording>['last']> | null>(null);
  let reportedClip = hub.live.recording?.last?.finished ?? 0;
  $effect(() => {
    const last = hub.live.recording?.last;
    if (!last || last.finished === reportedClip) return;
    reportedClip = last.finished;
    lastClip = last;
  });

  function snapshot() {
    if (!video?.videoWidth) return;
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d')?.drawImage(video, 0, 0);
    canvas.toBlob((blob) => {
      if (!blob) return;
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `kiwibit-snapshot-${stamp()}.jpg`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      notice = `Saved ${link.download}`;
    }, 'image/jpeg', 0.92);
  }

  function fullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else frame?.requestFullscreen?.();
  }

  async function wake() {
    waking = true;
    try {
      await api.post('/api/camera/wake');
      notice = 'Wake request sent.';
      setTimeout(() => loadCamera(true), 4000);
    } catch (e) {
      notice = message(e);
    } finally {
      waking = false;
    }
  }

  function when(recording: Recording) {
    if (!recording.recorded_at) return recording.name;
    const date = new Date(recording.recorded_at);
    const time = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return date.toDateString() === new Date().toDateString()
      ? time
      : `${date.toLocaleDateString([], { day: 'numeric', month: 'short' })}, ${time}`;
  }

  const live = $derived(phase === 'watching' && playing);
  const progress = $derived(phase === 'idle' ? '' : (PROGRESS[hub.live.state] ?? 'Connecting…'));
</script>

<div class="page">
  <section class="stage" aria-labelledby="live-title">
    <header class="heading">
      <div class="title-row">
        <h1 id="live-title" class="serif">Live view</h1>
        {#if live}
          <span class="chip chip-live"><span class="dot"></span>Live</span>
          {#if videoSize}<span class="chip chip-plain mono">{videoSize}</span>{/if}
        {/if}
      </div>
      {#if phase !== 'idle'}
        <button type="button" class="btn" onclick={stop}>End live view</button>
      {/if}
    </header>

    <div class="frame" bind:this={frame}>
      <!-- svelte-ignore a11y_media_has_caption -->
      <video bind:this={video} autoplay muted playsinline class:hidden={!live}
             onplaying={() => { playing = true; }}
             onresize={() => { if (video) videoSize = `${video.videoWidth} × ${video.videoHeight}`; }}></video>
      {#if !live}
        <div class="overlay" aria-live="polite">
          {#if phase === 'idle'}
            <p>{camera && !camera.awake ? 'The camera is asleep to save battery.' : 'Watch the feeder live.'}</p>
            <button type="button" class="btn btn-light" onclick={start}
                    disabled={camera?.can_watch_live === false}>
              <Icon name="wake" size={18} />Wake and watch live
            </button>
          {:else}
            <span class="spinner" aria-hidden="true"></span>
            <p>{progress}</p>
            <p class="hint">A sleeping camera can take up to a minute.</p>
          {/if}
        </div>
      {/if}
    </div>

    {#if error}<p class="error" role="alert">{error}</p>{/if}

    <div class="controls">
      <div class="segmented" role="group" aria-label="Resolution">
        {#each RESOLUTIONS as option (option.value)}
          <button type="button" aria-pressed={resolution === option.value}
                  disabled={phase === 'starting'}
                  onclick={() => chooseResolution(option.value)}>{option.label}</button>
        {/each}
      </div>
      <button type="button" class="btn" onclick={snapshot} disabled={!live}>
        <Icon name="snapshot" size={18} />Snapshot
      </button>
      {#if recording}
        <button type="button" class="btn recording" onclick={stopRecording} disabled={recordBusy}>
          <span class="rec-stop"></span>{recordBusy ? 'Saving…' : `Stop · ${clock(recordElapsed)}`}
        </button>
      {:else}
        <button type="button" class="btn" onclick={startRecording} disabled={!live || recordBusy}>
          <span class="rec"></span>Record clip
        </button>
      {/if}
      <button type="button" class="btn" onclick={fullscreen} disabled={!live} aria-label="Full screen">
        <Icon name="fullscreen" size={18} />
      </button>
      <label class="boxes" title="Coming next: identify birds on the live picture">
        Live bird boxes <input type="checkbox" class="switch" role="switch" disabled>
      </label>
    </div>
    {#if lastClip}
      <p class="saved" aria-live="polite">
        {#if lastClip.error}
          {lastClip.error}
        {:else if lastClip.recording_id}
          Clip saved to the library. Its birds are identified when the hub is free.
          <a href="#/recordings/{lastClip.recording_id}">Open the clip</a>
        {:else}
          Clip saved as {lastClip.file}.
        {/if}
      </p>
    {/if}
    {#if notice}<p class="muted small" aria-live="polite">{notice}</p>{/if}
    <p class="muted small">Live view is video only. Recorded clips (up to 10 minutes) go into the library. The session closes itself about 30 seconds after everyone stops watching, so the camera can sleep.</p>
  </section>

  <aside class="side">
    <section class="card panel" aria-labelledby="camera-title">
      <div class="panel-head">
        <div>
          <h2 id="camera-title" class="serif">{camera?.name ?? 'Camera'}</h2>
          {#if camera?.model}<span class="muted small">{camera.model}</span>{/if}
        </div>
        {#if live}
          <span class="chip">Streaming</span>
        {:else if camera}
          <span class="chip" class:chip-plain={!camera.awake}>{camera.awake ? 'Awake' : 'Asleep'}</span>
        {/if}
      </div>
      {#if cameraError}
        <p class="error small">{cameraError}</p>
      {:else if camera}
        <dl>
          {#if camera.battery !== null}
            <div><dt><Icon name="battery" size={18} />Battery</dt><dd class="mono">{camera.battery}%</dd></div>
          {/if}
          <div><dt><Icon name="live" size={18} />Connection</dt><dd>{camera.online ? 'Online' : 'Offline'}</dd></div>
          {#if camera.firmware}
            <div><dt><Icon name="chip" size={18} />Firmware</dt><dd class="mono">{camera.firmware}</dd></div>
          {/if}
          {#if camera.recording_resolution}
            <div><dt><Icon name="film" size={18} />Records at</dt><dd class="mono">{camera.recording_resolution.replace('x', ' × ')}</dd></div>
          {/if}
        </dl>
        <button type="button" class="btn" onclick={wake} disabled={waking || phase !== 'idle'}>
          <Icon name="wake" size={18} />{waking ? 'Waking…' : 'Wake camera'}
        </button>
      {:else}
        <p class="muted small">Loading camera…</p>
      {/if}
    </section>

    <section class="card panel" aria-labelledby="recent-title">
      <h2 id="recent-title" class="serif">Recent visits</h2>
      {#each recent as recording (recording.id)}
        <div class="visit">
          <span class="visit-name">{recording.species[0] ?? recording.animals[0] ?? (recording.status === 'ready' ? 'No birds identified' : 'Not processed yet')}</span>
          <span class="mono muted small">{when(recording)}</span>
        </div>
      {:else}
        <p class="muted small">No recordings in the library yet.</p>
      {/each}
    </section>
  </aside>
</div>

<style>
  .page {
    padding: 32px 36px; display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 24px; align-items: start;
  }
  .stage { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 48px; }
  .title-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 34px; font-weight: 600; }
  h2 { margin: 0; font-size: 22px; font-weight: 600; }
  .small { font-size: 13px; margin: 0; }

  .frame {
    position: relative; aspect-ratio: 16 / 9; border-radius: 16px; overflow: hidden;
    background: radial-gradient(circle at 50% 38%, #2E3D35 0, var(--frame) 72%); color: #C9D3CC;
  }
  .frame:fullscreen { border-radius: 0; background: #000000; }
  video { width: 100%; height: 100%; object-fit: contain; display: block; background: #000000; }
  video.hidden { visibility: hidden; }
  .overlay {
    position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 14px; text-align: center; padding: 24px;
  }
  .overlay p { margin: 0; font-size: 15px; }
  .overlay .hint { font-size: 13px; color: #9FB0A6; }
  .btn-light { background: var(--paper); border-color: var(--paper); color: var(--spruce); font-weight: 700; }
  .spinner {
    width: 28px; height: 28px; border-radius: 50%; border: 3px solid rgba(201, 211, 204, .3); border-top-color: var(--goldfinch);
    animation: spin 0.9s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .error { margin: 0; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }
  .controls { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .rec { width: 12px; height: 12px; border-radius: 50%; background: var(--ember); }
  .rec-stop { width: 12px; height: 12px; border-radius: 2px; background: #FFFFFF; }
  .btn.recording { background: var(--ember); border-color: var(--ember); color: #FFFFFF; font-variant-numeric: tabular-nums; }
  .saved { margin: 0; padding: 10px 14px; border-radius: 12px; background: var(--spruce-soft); color: var(--spruce); font-size: 14px; }
  .boxes { margin-left: auto; display: flex; align-items: center; gap: 10px; font-weight: 600; color: var(--muted); }

  .side { display: flex; flex-direction: column; gap: 20px; }
  .panel { padding: 18px; display: flex; flex-direction: column; gap: 10px; }
  .panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  dl { margin: 0; }
  dl div {
    display: flex; align-items: center; justify-content: space-between; min-height: 40px; border-top: 1px solid var(--line-soft);
  }
  dt { display: flex; align-items: center; gap: 10px; color: var(--muted); }
  dt :global(svg) { color: var(--muted); }
  dd { margin: 0; }
  .visit {
    display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 44px; border-top: 1px solid var(--line-soft);
  }
  .visit-name { font-weight: 600; }

  @media (max-width: 1100px) {
    .page { grid-template-columns: minmax(0, 1fr); }
    .side { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 860px) {
    .page { padding: 18px 0 16px; gap: 16px; }
    .heading, .controls, .stage > p, .error { margin-inline: 16px; }
    .frame { border-radius: 0; }
    .side { grid-template-columns: minmax(0, 1fr); padding: 0 16px; }
    .boxes { margin-left: 0; }
    h1 { font-size: 28px; }
  }
</style>
