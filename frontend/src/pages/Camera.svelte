<script lang="ts">
  import Icon from '../lib/Icon.svelte';
  import { api, ApiError, type Camera, type CameraSetting, type CameraSettings, type FetchSchedule, type ProcessingSettings } from '../lib/api';
  import { hub, signOut } from '../lib/hub.svelte';

  let camera = $state<Camera | null>(null);
  let settings = $state<CameraSettings | null>(null);
  let processing = $state<ProcessingSettings | null>(null);
  let schedule = $state<FetchSchedule | null>(null);
  let error = $state('');
  let refreshing = $state(false);
  let saved = $state('');
  let recordings = $state<number | null>(null);
  // Recordings not yet analyzed (pending or failed), and SD-card clips in the library.
  let waiting = $state(0);
  let card = $state<{ recordings: number; waiting: number } | null>(null);
  let confirmAnalyze = $state(false);
  let cardMessage = $state('');
  let confirmRerun = $state(false);
  let rerunMessage = $state('');
  // Recordings analyzed under older tracking rules; updating them needs no AI models.
  let outdated = $state(0);
  let confirmUpgrade = $state(false);

  async function load(refresh = false) {
    refreshing = true;
    error = '';
    const query = refresh ? '?refresh=true' : '';
    const [cameraResult, settingsResult, processingResult, scheduleResult] = await Promise.allSettled([
      api.get<Camera>(`/api/camera${query}`),
      api.get<CameraSettings>(`/api/camera/settings${query}`),
      api.get<ProcessingSettings>('/api/settings/processing'),
      api.get<FetchSchedule>('/api/settings/fetch'),
    ]);
    if (scheduleResult.status === 'fulfilled') schedule = scheduleResult.value;
    if (cameraResult.status === 'fulfilled') camera = cameraResult.value;
    if (settingsResult.status === 'fulfilled') settings = settingsResult.value;
    else error = (settingsResult.reason as Error).message;
    if (processingResult.status === 'fulfilled') processing = processingResult.value;
    refreshing = false;
  }
  load();
  type Status = { recordings: number; by_status: Record<string, number>; sdcard: { recordings: number; waiting: number }; outdated: number };
  async function loadStatus() {
    try {
      const status = await api.get<Status>('/api/status');
      recordings = status.recordings;
      waiting = Object.entries(status.by_status).filter(([state]) => state !== 'ready').reduce((sum, [, n]) => sum + n, 0);
      card = status.sdcard;
      outdated = status.outdated;
    } catch {
      /* the counts are a convenience */
    }
  }
  loadStatus();
  // Counts change as a scan or analysis runs.
  let seenChange = hub.changed;
  $effect(() => {
    if (hub.changed !== seenChange) {
      seenChange = hub.changed;
      loadStatus();
    }
  });

  async function startJob(action: 'scan' | 'process', started: string) {
    cardMessage = '';
    try {
      await api.post('/api/jobs', { action });
      confirmAnalyze = false;
      cardMessage = started;
    } catch (e) {
      cardMessage = e instanceof ApiError && e.status === 409 ? 'Another job is running; try again when it finishes.' : (e as Error).message;
    }
  }

  async function startRerun(action: 'reanalyze_all' | 'upgrade' = 'reanalyze_all') {
    rerunMessage = '';
    try {
      await api.post('/api/jobs', { action });
      confirmRerun = false;
      confirmUpgrade = false;
      rerunMessage = 'Started. Follow it in the progress card; Stop keeps what has finished.';
    } catch (e) {
      rerunMessage = e instanceof ApiError && e.status === 409 ? 'Another job is running; try again when it finishes.' : (e as Error).message;
    }
  }

  async function saveProcessing(change: Partial<ProcessingSettings>) {
    if (!processing) return;
    saved = '';
    try {
      processing = await api.put<ProcessingSettings>('/api/settings/processing', {
        detector_batch: change.detector_batch ?? processing.detector_batch,
        classifier_batch: change.classifier_batch ?? processing.classifier_batch,
      });
      saved = 'Saved. Applies to the next analysis.';
    } catch (e) {
      saved = e instanceof ApiError && e.status === 409 ? 'The hub is processing; change this when it finishes.' : (e as Error).message;
      processing = await api.get<ProcessingSettings>('/api/settings/processing');
    }
  }

  async function saveSchedule(change: { auto?: boolean; interval_minutes?: number }) {
    try {
      schedule = await api.put<FetchSchedule>('/api/settings/fetch', change);
    } catch (e) {
      saved = (e as Error).message;
    }
  }
  const interval = (minutes: number) => (minutes < 60 ? `${minutes} minutes` : minutes === 60 ? '1 hour' : `${minutes / 60} hours`);

  const signal = $derived.by(() => {
    const dbm = settings?.status.wifi_dbm;
    if (dbm == null) return '—';
    const quality = dbm >= -60 ? 'Strong' : dbm >= -70 ? 'Good' : dbm >= -80 ? 'Fair' : 'Weak';
    return `${quality} · ${dbm} dBm`;
  });
  const isRange = (setting: CameraSetting) => setting.type === 'INT_RANGE' && typeof setting.value === 'number';
</script>

<div class="page">
  <header class="head">
    <div>
      <h1 class="serif">Camera</h1>
      <p class="muted">{camera ? [camera.name, camera.model].filter(Boolean).join(' · ') : 'Loading…'}</p>
    </div>
    <button type="button" class="btn" onclick={() => load(true)} disabled={refreshing}>
      <Icon name="refresh" size={18} />{refreshing ? 'Reading…' : 'Read again'}
    </button>
  </header>

  <p class="notice">
    Settings are read live from the camera. Changing them from the hub needs the phone app's settings
    calls, which haven't been captured yet, so the camera's controls are read-only for now.
  </p>
  {#if error}<p class="error">{error}</p>{/if}

  <div class="grid">
    <section class="card panel" aria-labelledby="status-title">
      <h2 id="status-title" class="serif">Status</h2>
      {#if settings}
        <dl>
          <div><dt>Battery</dt><dd class="mono">{settings.status.battery ?? '—'}{settings.status.battery != null ? '%' : ''}</dd></div>
          <div><dt>Wi-Fi signal</dt><dd class="mono">{signal}</dd></div>
          <div><dt>Firmware</dt><dd class="mono">{settings.status.firmware ?? '—'}</dd></div>
          <div><dt>Connection</dt><dd>{settings.status.online ? 'Online' : 'Offline'}{settings.status.online ? ` · ${settings.status.awake ? 'awake' : 'asleep'}` : ''}</dd></div>
          <div><dt>Activity zone</dt><dd>{settings.activity_zones === 0 ? 'None (whole view)' : settings.activity_zones ?? '—'}</dd></div>
        </dl>
      {:else}
        <p class="muted small">{error ? 'Unavailable' : 'Reading…'}</p>
      {/if}
    </section>

    {#each settings?.groups ?? [] as group (group.title)}
      <section class="card panel" aria-label={group.title}>
        <h2 class="serif">{group.title}</h2>
        {#each group.settings as setting (setting.name)}
          <div class="line">
            <span class="label">
              {setting.label}
              {#if setting.description}<span class="muted small">{setting.description}</span>{/if}
            </span>
            {#if setting.type === 'SWITCH'}
              <input type="checkbox" class="switch" role="switch" checked={setting.value === true} disabled aria-label={setting.label}>
            {:else if setting.type === 'ENUM' && setting.options.length > 1 && setting.options.length <= 4}
              <div class="segmented" role="group" aria-label={setting.label}>
                {#each setting.options as option (option.value)}
                  <button type="button" aria-pressed={option.value === setting.value} disabled>{option.label}</button>
                {/each}
              </div>
            {:else if setting.type === 'ENUM'}
              <span class="value">{setting.options.find((o) => o.value === setting.value)?.label ?? String(setting.value ?? '—')}</span>
            {:else if isRange(setting)}
              <span class="range">
                <meter min="0" max="100" value={setting.value as number} aria-label={setting.label}></meter>
                <span class="mono">{setting.value}</span>
              </span>
            {:else}
              <span class="value">{String(setting.value ?? '—')}</span>
            {/if}
          </div>
        {/each}
      </section>
    {/each}

    <section class="card panel" aria-labelledby="fetch-title">
      <h2 id="fetch-title" class="serif">Recordings from the cloud</h2>
      {#if schedule}
        <label class="line">
          <span class="label">Fetch new recordings automatically<span class="muted small">Downloads and identifies new clips. Doesn't wake the camera.</span></span>
          <input type="checkbox" class="switch" role="switch" checked={schedule.auto}
                 onchange={(event) => saveSchedule({ auto: event.currentTarget.checked })}>
        </label>
        <label class="line">
          <span class="label">Check every</span>
          <select value={schedule.interval_minutes} disabled={!schedule.auto}
                  onchange={(event) => saveSchedule({ interval_minutes: Number(event.currentTarget.value) })}>
            {#each schedule.intervals as minutes (minutes)}<option value={minutes}>{interval(minutes)}</option>{/each}
          </select>
        </label>
        {#if schedule.last_finished}
          <p class="muted small">Last check {new Date(schedule.last_finished * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}: {schedule.last_message}</p>
        {/if}
      {/if}
    </section>

    <section class="card panel" aria-labelledby="hub-ai-title">
      <h2 id="hub-ai-title" class="serif">Hub identification</h2>
      <p class="muted small">BioCLIP 2.5 on this PC's GPU identifies birds in every recording. Larger batches use more GPU memory.</p>
      {#if processing}
        <div class="batch">
          <label>Frames per GPU batch
            <select value={processing.detector_batch} disabled={hub.job.running}
                    onchange={(event) => saveProcessing({ detector_batch: Number(event.currentTarget.value) })}>
              {#each processing.choices as choice (choice)}<option value={choice}>{choice}</option>{/each}
            </select>
          </label>
          <label>Bird crops per GPU batch
            <select value={processing.classifier_batch} disabled={hub.job.running}
                    onchange={(event) => saveProcessing({ classifier_batch: Number(event.currentTarget.value) })}>
              {#each processing.choices as choice (choice)}<option value={choice}>{choice}</option>{/each}
            </select>
          </label>
        </div>
        <p class="muted small" aria-live="polite">{saved || (hub.job.running ? 'Batch sizes can change when the current job finishes.' : '')}</p>
      {/if}
      <div class="rerun">
        {#if confirmUpgrade}
          <p class="small">Applies the latest tracking and sighting rules to the saved results of {outdated.toLocaleString()}
            recordings, and cuts their bird photos again. No AI model runs, but every video is read once, so it can take a
            while. Corrections stay with their sightings.</p>
          <div class="rerun-actions">
            <button type="button" class="btn btn-primary" onclick={() => startRerun('upgrade')}>Apply to {outdated.toLocaleString()}</button>
            <button type="button" class="btn" onclick={() => { confirmUpgrade = false; }}>Cancel</button>
          </div>
        {:else if outdated && !confirmRerun}
          <button type="button" class="btn" disabled={hub.job.running} onclick={() => { confirmUpgrade = true; rerunMessage = ''; }}>
            <Icon name="refresh" size={18} />Apply tracking improvements ({outdated.toLocaleString()})…
          </button>
        {/if}
        {#if confirmRerun}
          <p class="small">Runs the detector and BioCLIP again on all {recordings ?? ''} recordings with the current
            settings and species list. It uses the GPU for about 12 minutes per hour of video (a full SD card is
            several hours). The hub stays usable, and each recording keeps
            its current results until its new ones are saved.</p>
          <div class="rerun-actions">
            <button type="button" class="btn btn-primary" onclick={() => startRerun()}>Start re-analysis</button>
            <button type="button" class="btn" onclick={() => { confirmRerun = false; }}>Cancel</button>
          </div>
        {:else if !confirmUpgrade}
          <button type="button" class="btn" disabled={hub.job.running} onclick={() => { confirmRerun = true; rerunMessage = ''; }}>
            <Icon name="refresh" size={18} />Re-analyze all recordings…
          </button>
        {/if}
        {#if rerunMessage}<p class="muted small" aria-live="polite">{rerunMessage}</p>{/if}
      </div>
    </section>

    <section class="card panel" aria-labelledby="sdcard-title">
      <h2 id="sdcard-title" class="serif">SD-card footage</h2>
      <p class="muted small">Move clips from the camera's SD card into the <span class="mono">sdcard</span> folder in the hub's
        data folder (next to <span class="mono">library</span> and <span class="mono">downloads</span>); the card's own folders
        are fine. Adding them only lists them. Analysis starts when you choose, never from a scheduled fetch.</p>
      {#if card}
        <p class="small">{card.recordings.toLocaleString()} card clips in the library · {card.waiting.toLocaleString()} not yet analyzed</p>
      {/if}
      <div class="rerun-actions">
        <button type="button" class="btn" disabled={hub.job.running}
                onclick={() => startJob('scan', 'Adding card clips. New ones appear as recordings waiting for analysis.')}>
          Add card clips
        </button>
        {#if confirmAnalyze}
          <button type="button" class="btn btn-primary" onclick={() => startJob('process', 'Started. Follow it in the progress card; Stop keeps what has finished.')}>
            Start analyzing {waiting.toLocaleString()}
          </button>
          <button type="button" class="btn" onclick={() => { confirmAnalyze = false; }}>Cancel</button>
        {:else}
          <button type="button" class="btn" disabled={hub.job.running || waiting === 0} onclick={() => { confirmAnalyze = true; cardMessage = ''; }}>
            Analyze waiting recordings ({waiting.toLocaleString()})…
          </button>
        {/if}
      </div>
      {#if confirmAnalyze}
        <p class="small">Runs the detector and BioCLIP on the {waiting.toLocaleString()} recordings that haven't been analyzed yet.
          A full SD card is hours of GPU time; the progress card shows the time left.</p>
      {/if}
      {#if cardMessage}<p class="muted small" aria-live="polite">{cardMessage}</p>{/if}
    </section>

    {#if hub.session?.auth_required}
      <section class="card panel" aria-labelledby="account-title">
        <h2 id="account-title" class="serif">Hub account</h2>
        <div class="line">
          <span class="label">Signed in as {hub.session.user}</span>
          <button type="button" class="btn" onclick={signOut}>Sign out</button>
        </div>
      </section>
    {/if}
  </div>
  <p class="muted small">Firmware updates, resets and removing the camera stay in the Kiwibit app.</p>
</div>

<style>
  .page { padding: 32px 36px; display: flex; flex-direction: column; gap: 20px; }
  .head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 34px; font-weight: 600; }
  .head p { margin: 4px 0 0; }
  .notice { margin: 0; padding: 12px 16px; border-radius: 12px; background: var(--spruce-soft); color: var(--spruce); font-size: 14px; }
  .error { margin: 0; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }
  .grid { columns: 2 420px; column-gap: 20px; }
  .panel {
    padding: 18px 20px; display: flex; flex-direction: column; gap: 4px; break-inside: avoid; margin-bottom: 20px;
  }
  h2 { margin: 0 0 6px; font-size: 21px; font-weight: 600; }
  dl { margin: 0; }
  dl div {
    display: flex; justify-content: space-between; align-items: center; gap: 12px; min-height: 44px;
    border-top: 1px solid var(--line-soft); font-size: 15px;
  }
  dt { color: var(--muted); }
  dd { margin: 0; text-align: right; }
  .line {
    display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 48px;
    border-top: 1px solid var(--line-soft); font-size: 15px; padding: 6px 0;
  }
  .label { display: flex; flex-direction: column; gap: 2px; }
  .value { text-align: right; font-weight: 600; }
  .small { font-size: 13px; margin: 0; }
  .segmented button { min-height: 34px; padding: 0 10px; font-size: 13px; }
  .segmented button:disabled[aria-pressed='true'] { opacity: 1; }
  .switch:disabled:checked { opacity: .85; }
  .range { display: flex; align-items: center; gap: 8px; }
  meter { width: 90px; height: 10px; }
  .rerun { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line-soft); }
  .rerun p { margin: 0; }
  .rerun-actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .batch { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 6px; }
  .batch label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; color: var(--muted); }
  select {
    height: 44px; padding: 0 10px; border-radius: 10px; border: 1px solid var(--control-line); background: #FFFFFF;
    font-size: 15px; color: var(--ink);
  }
  @media (max-width: 860px) {
    .page { padding: 20px 16px; }
    h1 { font-size: 28px; }
    .grid { columns: 1; }
    .line { flex-wrap: wrap; }
  }
</style>
