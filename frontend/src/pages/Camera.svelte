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
