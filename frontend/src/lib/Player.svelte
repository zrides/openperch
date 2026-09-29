<script lang="ts">
  import { onDestroy } from 'svelte';
  import CorrectionDialog from './CorrectionDialog.svelte';
  import Icon from './Icon.svelte';
  import { api, ApiError, UNCERTAIN, type Analysis, type Animal, type Recording, type Sighting } from './api';
  import { clock, dateTime, duration } from './format';
  import { hub } from './hub.svelte';
  import { animalLabel, animalsAt, contentRect, dropouts } from './overlay';

  let { recording, startMs = 0 }: { recording: Recording; startMs?: number } = $props();

  let analysis = $state<Analysis | null>(null);
  let timestamps: number[] = [];
  let gaps = $state<{ start: number; end: number }[]>([]);
  let sightings: Sighting[] = $state([]);
  let loadError = $state('');
  let notice = $state('');

  let video: HTMLVideoElement | undefined = $state();
  let stage: HTMLDivElement | undefined = $state();
  let rect = $state({ left: 0, top: 0, width: 0, height: 0 });
  let animals: Animal[] = $state([]);
  let selectedId = $state<number | null>(null);
  let lastSelected = $state<Animal | null>(null);
  let current = $state(0);
  let total = $state(0);
  let playing = $state(false);
  let muted = $state(true);
  let rate = $state(1);
  let correcting = $state<Sighting | null>(null);
  let frameCallback = 0;
  // Frame-accurate overlays where supported; timeupdate (about 4 Hz) otherwise.
  const frameAccurate = 'requestVideoFrameCallback' in HTMLVideoElement.prototype;

  async function load(id: string) {
    loadError = '';
    const [analysisResult, sightingsResult] = await Promise.allSettled([
      api.get<Analysis>(`/api/recordings/${id}/analysis`),
      api.get<Sighting[]>(`/api/recordings/${id}/sightings`),
    ]);
    if (id !== recording.id) return; // a newer recording was chosen meanwhile
    if (analysisResult.status === 'fulfilled') {
      analysis = analysisResult.value;
      timestamps = analysis.frames.map((frame) => frame.ms);
      gaps = dropouts(timestamps, analysis.fps);
    } else {
      analysis = null;
      timestamps = [];
      gaps = [];
      const error = analysisResult.reason;
      if (!(error instanceof ApiError && error.status === 404)) loadError = String(error?.message ?? error);
    }
    sightings = sightingsResult.status === 'fulfilled' ? sightingsResult.value : [];
    update(current);
  }

  // A new recording: reset and load its analysis.
  $effect(() => {
    const id = recording.id;
    analysis = null;
    sightings = [];
    animals = [];
    selectedId = null;
    lastSelected = null;
    current = 0;
    notice = '';
    load(id);
  });

  // Corrections or reprocessing elsewhere: refresh labels without interrupting playback.
  let seenChange = hub.changed;
  $effect(() => {
    if (hub.changed !== seenChange) {
      seenChange = hub.changed;
      load(recording.id);
    }
  });

  // The camera's recording has a hole here: the picture is frozen.
  const dropout = $derived(gaps.find((gap) => current >= gap.start && current < gap.end) ?? null);

  // Boxes follow the frame on screen (`ms`); the clock follows the playhead, which
  // keeps moving while a camera dropout holds a frozen frame.
  function update(ms: number, playhead = ms) {
    current = playhead;
    animals = analysis ? animalsAt(analysis, timestamps, ms) : [];
    const selected = animals.find((animal) => animal.id === selectedId);
    if (selected) lastSelected = selected;
    else if (animals.length && (selectedId === null || !lastSelected)) {
      selectedId = animals[0].id;
      lastSelected = animals[0];
    }
  }

  function watchFrames() {
    if (!video || !frameAccurate) return;
    {
      const step = (_now: number, meta: VideoFrameCallbackMetadata) => {
        update(meta.mediaTime * 1000, video!.currentTime * 1000);
        frameCallback = video!.requestVideoFrameCallback(step);
      };
      frameCallback = video.requestVideoFrameCallback(step);
    }
  }
  onDestroy(() => {
    if (video && frameCallback && frameAccurate) video.cancelVideoFrameCallback(frameCallback);
  });

  function measure() {
    if (video) rect = contentRect(video, video.videoWidth, video.videoHeight);
  }
  $effect(() => {
    if (!stage) return;
    const observer = new ResizeObserver(measure);
    observer.observe(stage);
    return () => observer.disconnect();
  });

  function loaded() {
    if (!video) return;
    total = video.duration * 1000;
    measure();
    if (startMs > 0) seek(startMs);
    watchFrames();
    video.play().catch(() => {});
  }

  function seek(ms: number) {
    if (!video) return;
    video.currentTime = Math.max(0, Math.min(ms, total)) / 1000;
    update(ms);
  }

  function toggle() {
    if (!video) return;
    if (video.paused) video.play();
    else video.pause();
  }

  function cycleRate() {
    const rates = [0.5, 1, 1.5, 2];
    rate = rates[(rates.indexOf(rate) + 1) % rates.length];
    if (video) video.playbackRate = rate;
  }

  function fullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else stage?.requestFullscreen?.();
  }

  async function startJob(action: 'reanalyze' | 'selected') {
    notice = '';
    try {
      await api.post('/api/jobs', { action, ids: [recording.id] });
      notice = action === 'reanalyze'
        ? 'Reanalysis started. Overlays update when it finishes.'
        : 'Processing started. Overlays appear when it finishes.';
    } catch (e) {
      notice = e instanceof ApiError && e.status === 409
        ? 'The hub is busy with another job. Try again when it finishes.'
        : String((e as Error).message ?? e);
    }
  }

  // Whether a cloud clip the SD card also holds stays in the list. The hub announces the
  // change, which reloads the list and this recording.
  async function setAlwaysShow(value: boolean) {
    notice = '';
    try {
      await api.put(`/api/recordings/${recording.id}/always-show`, { value });
    } catch (e) {
      notice = String((e as Error).message ?? e);
    }
  }

  const ORIGINS: Record<string, string> = { cloud: 'cloud', sdcard: 'SD card', live: 'live view' };

  // A saved correction bumps hub.changed, which reloads the labels above.
  function closeCorrection() {
    correcting = null;
  }

  const meta = $derived([
    ORIGINS[recording.origin],
    duration(recording.duration),
    analysis && `${analysis.width} × ${analysis.height}`,
    analysis && `${analysis.fps.toFixed(2)} fps`,
    recording.status === 'failed' ? 'processing failed' : recording.status !== 'ready' ? 'not processed yet' : '',
  ].filter(Boolean).join(' · '));
  const shown = $derived(animals.find((animal) => animal.id === selectedId) ?? lastSelected);
  const bestSimilarity = $derived(shown?.ranked[0]?.similarity || 1);
  const label = (s: Sighting) => s.corrected ?? s.ai_species;
  const uncertain = (s: Sighting) => !s.corrected && s.ai_species === UNCERTAIN;
  const percent = (ms: number) => (total ? `${(ms / total) * 100}%` : '0%');
</script>

<section class="player" aria-label="Recording player">
  <header class="head">
    <div>
      <h2 class="serif">{dateTime(recording.recorded_at) || recording.name}</h2>
      <p class="muted meta">{meta}</p>
    </div>
    <div class="head-actions">
      {#if recording.status === 'ready'}
        <button type="button" class="btn" onclick={() => startJob('reanalyze')}>Reanalyze with fresh AI</button>
      {:else if recording.status !== 'processing'}
        <button type="button" class="btn btn-primary" onclick={() => startJob('selected')}>Process now</button>
      {/if}
      <a class="btn" href="/api/recordings/{recording.id}/video?download=true" aria-label="Download original video">
        <Icon name="download" size={18} />Original
      </a>
    </div>
  </header>
  {#if notice}<p class="notice" aria-live="polite">{notice}</p>{/if}
  {#if recording.covered_by}
    <p class="copy">
      <span>{recording.always_show ? 'The SD card has this moment too. You chose to keep this cloud copy in the list.'
        : 'The SD card has this moment too, so this cloud copy is left out of the list.'}</span>
      <a href="#/recordings/{recording.covered_by.id}?t={recording.covered_by.offset_ms}">Watch the SD card copy</a>
      <button type="button" class="link" onclick={() => setAlwaysShow(!recording.always_show)}>
        {recording.always_show ? 'Leave it out of the list' : 'Keep it in the list'}
      </button>
    </p>
  {/if}
  {#if recording.status === 'failed' && recording.error}<p class="error">{recording.error}</p>{/if}
  {#if loadError}<p class="error">{loadError}</p>{/if}

  <div class="stage" bind:this={stage}>
    <!-- svelte-ignore a11y_media_has_caption -->
    <video bind:this={video} src="/api/recordings/{recording.id}/video" playsinline preload="metadata" muted={muted}
           onloadedmetadata={loaded} onresize={measure}
           onplay={() => { playing = true; }} onpause={() => { playing = false; }}
           ontimeupdate={() => {
             if (!video) return;
             // Frame callbacks stop while the picture is frozen; the clock should keep going.
             if (frameAccurate) current = video.currentTime * 1000;
             else update(video.currentTime * 1000);
           }}
           onseeked={() => { if (video) update(video.currentTime * 1000); }}
           onclick={toggle}></video>
    {#if dropout}
      <span class="dropout" role="status">Camera dropped out · picture frozen for {((dropout.end - dropout.start) / 1000).toFixed(1)} s</span>
    {/if}
    <div class="boxes" style:left="{rect.left}px" style:top="{rect.top}px" style:width="{rect.width}px" style:height="{rect.height}px">
      {#each animals as animal (animal.id)}
        {@const box = animal.box}
        <button type="button" class="box" class:selected={animal.id === selectedId} class:weak={animal.tracking_state === 'weak'}
                style:left="{box[0] * 100}%" style:top="{box[1] * 100}%"
                style:width="{(box[2] - box[0]) * 100}%" style:height="{(box[3] - box[1]) * 100}%"
                aria-label="Bird {animal.id}: {animalLabel(animal)}"
                onclick={() => { selectedId = animal.id; lastSelected = animal; }}>
          <span class="tag">#{animal.id} {animalLabel(animal)}{#if !animal.display_label && animal.ranked[0]}<span class="mono"> {animal.ranked[0].similarity.toFixed(3)}</span>{/if}</span>
        </button>
      {/each}
    </div>
  </div>

  <div class="controls">
    <button type="button" class="play" onclick={toggle} aria-label={playing ? 'Pause' : 'Play'}>
      {#if playing}
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/></svg>
      {:else}
        <Icon name="play" size={20} />
      {/if}
    </button>
    <span class="mono time">{clock(current)} / {clock(total)}</span>
    <div class="timeline">
      {#each gaps as gap (gap.start)}
        <span class="gap" style:left={percent(gap.start)} style:width="max(3px, calc({percent(gap.end - gap.start)}))"
              title="Camera dropped out for {((gap.end - gap.start) / 1000).toFixed(1)} s at {clock(gap.start)}"></span>
      {/each}
      {#each sightings as s (s.id)}
        <span class="span" class:uncertain={uncertain(s)} style:left={percent(s.start_ms)}
              style:width="max(4px, calc({percent(s.end_ms - s.start_ms)}))" title="{label(s)} {clock(s.start_ms)}–{clock(s.end_ms)}"></span>
      {/each}
      <input type="range" min="0" max={Math.max(total, 1)} step="10" value={current} aria-label="Seek"
             oninput={(event) => seek(Number(event.currentTarget.value))}>
    </div>
    <button type="button" class="icon mono" onclick={cycleRate} aria-label="Playback speed">{rate}×</button>
    <button type="button" class="icon" onclick={() => { muted = !muted; }} aria-label={muted ? 'Unmute' : 'Mute'} aria-pressed={!muted}>
      <Icon name={muted ? 'muted' : 'sound'} size={18} />
    </button>
    <button type="button" class="icon" onclick={fullscreen} aria-label="Full screen"><Icon name="fullscreen" size={18} /></button>
  </div>

  <div class="cards">
    <section class="card panel" aria-labelledby="sightings-title">
      <h3 id="sightings-title" class="eyebrow">Sightings in this clip</h3>
      {#each sightings as s (s.id)}
        <div class="sighting" class:highlight={!uncertain(s)}>
          <span class="bar" class:uncertain={uncertain(s)}></span>
          <button type="button" class="sighting-main" onclick={() => seek(s.start_ms)}>
            <span class="name">{label(s)}</span>
            <span class="muted small">
              <span class="mono">{clock(s.start_ms)}–{clock(s.end_ms)}</span> ·
              {#if s.corrected}your correction
              {:else if uncertain(s)}looks like {s.evidence.suggestion || 'an unknown bird'}, {s.evidence.reason.toLowerCase()}
              {:else}{s.evidence.support} of {s.evidence.samples} sampled views agree{/if}
            </span>
          </button>
          <button type="button" class="btn small-btn" class:btn-primary={uncertain(s)} onclick={() => { correcting = s; }}>
            {uncertain(s) ? 'Review' : 'Correct…'}
          </button>
        </div>
      {:else}
        <p class="muted small">{analysis ? 'No bird sightings in this clip.' : recording.status === 'ready' ? 'Loading…' : 'Process this recording to find its sightings.'}</p>
      {/each}
    </section>

    <section class="card panel" aria-labelledby="ranked-title">
      <div class="panel-head">
        <h3 id="ranked-title" class="eyebrow">{shown ? `Bird #${shown.id} at ${clock(current)}` : 'Selected bird'}</h3>
        {#if shown}<span class="muted small">detector {Math.round(shown.detection * 100)}%</span>{/if}
      </div>
      {#if shown}
        {#if shown.identification_note}<p class="muted small note">{shown.identification_note}</p>{/if}
        {#each shown.ranked as match, index (match.label)}
          <div class="rank" class:top={index === 0} title="Relative weight among candidates: {(match.score * 100).toFixed(1)}%">
            <span>{match.label}</span>
            <span class="track"><span style:width="{Math.max(0, match.similarity / bestSimilarity) * 100}%"></span></span>
            <span class="mono">{match.similarity.toFixed(3)}</span>
          </div>
        {:else}
          <p class="muted small">Not classified in this frame.</p>
        {/each}
        <p class="muted small">Match is image-to-name similarity, not a probability.</p>
      {:else}
        <p class="muted small">{analysis ? 'Click a box on the video to inspect a bird.' : 'No AI overlays for this recording yet.'}</p>
      {/if}
    </section>
  </div>
</section>

{#if correcting}
  <CorrectionDialog sighting={correcting} context={dateTime(recording.recorded_at)} onclose={closeCorrection} />
{/if}

<style>
  .player { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  h2 { margin: 0; font-size: 26px; font-weight: 600; }
  .meta { margin: 4px 0 0; font-size: 14px; }
  .head-actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .copy {
    margin: 0; padding: 10px 14px; border-radius: 12px; border: 1px solid var(--line-soft); background: var(--card); font-size: 14px;
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px;
  }
  .copy span { flex-basis: 100%; }
  .copy a, .copy .link { font-weight: 600; }
  .link { padding: 0; border: none; background: none; color: var(--spruce); font: inherit; cursor: pointer; text-decoration: underline; }
  .notice { margin: 0; padding: 10px 14px; border-radius: 12px; background: var(--spruce-soft); color: var(--spruce); font-size: 14px; }
  .error { margin: 0; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }

  .stage {
    position: relative; aspect-ratio: 16 / 9; border-radius: 16px; overflow: hidden; background: var(--frame);
  }
  .stage:fullscreen { border-radius: 0; background: #000000; }
  video { width: 100%; height: 100%; object-fit: contain; display: block; cursor: pointer; }
  .boxes { position: absolute; pointer-events: none; }
  .box {
    position: absolute; padding: 0; border: 3px solid var(--goldfinch-box); border-radius: 4px; background: transparent;
    pointer-events: auto; cursor: pointer;
  }
  .box.weak { border-style: dashed; border-width: 2px; }
  .box.selected { border-color: #FFFFFF; box-shadow: 0 0 0 2px var(--goldfinch-box); }
  .tag {
    position: absolute; left: -3px; bottom: 100%; margin-bottom: 4px; padding: 3px 8px; border-radius: 6px;
    background: var(--goldfinch-box); color: var(--ink); font-size: 13px; font-weight: 700; white-space: nowrap;
  }
  .tag .mono { font-weight: 500; }

  .controls { display: flex; align-items: center; gap: 12px; }
  .play {
    width: 48px; height: 48px; border-radius: 50%; border: none; background: var(--spruce); color: #FFFFFF;
    display: inline-flex; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0;
  }
  .time { font-size: 14px; min-width: 96px; }
  .timeline { position: relative; flex-grow: 1; height: 28px; }
  .timeline::before {
    content: ''; position: absolute; left: 0; right: 0; top: 11px; height: 6px; border-radius: 3px; background: var(--line);
  }
  .span { position: absolute; top: 11px; height: 6px; border-radius: 3px; background: #7FA393; }
  .span.uncertain { background: var(--goldfinch); }
  /* A camera dropout: hatched, behind the sightings, so it reads as "no picture here". */
  .gap {
    position: absolute; top: 9px; height: 10px; border-radius: 3px;
    background: repeating-linear-gradient(135deg, #8E978F 0 3px, transparent 3px 6px); opacity: .8;
  }
  .dropout {
    position: absolute; left: 12px; top: 12px; z-index: 2; padding: 6px 10px; border-radius: 8px;
    background: rgba(27, 36, 32, .78); color: #FFFFFF; font-size: 13px; font-weight: 600; pointer-events: none;
  }
  input[type='range'] {
    position: absolute; inset: 0; width: 100%; margin: 0; background: transparent; appearance: none; -webkit-appearance: none; cursor: pointer;
  }
  input[type='range']::-webkit-slider-thumb {
    -webkit-appearance: none; width: 18px; height: 18px; border-radius: 50%; background: var(--card); border: 3px solid var(--spruce);
  }
  input[type='range']::-moz-range-thumb {
    width: 12px; height: 12px; border-radius: 50%; background: var(--card); border: 3px solid var(--spruce);
  }
  input[type='range']::-moz-range-track { background: transparent; }
  .icon {
    min-width: 44px; height: 44px; padding: 0 8px; border-radius: 12px; border: 1px solid var(--control-line); background: var(--card);
    display: inline-flex; align-items: center; justify-content: center; cursor: pointer; font-weight: 600;
  }
  .icon[aria-pressed='true'] { background: var(--spruce-soft); }

  .cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
  .panel { padding: 14px 18px; display: flex; flex-direction: column; gap: 10px; }
  .panel h3 { margin: 0; }
  .panel-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .small { font-size: 13px; margin: 0; }
  .note { margin-top: -4px; }
  .sighting {
    display: flex; align-items: center; gap: 12px; padding: 8px 10px; border-radius: 12px; border: 1px solid var(--line-soft);
  }
  .sighting.highlight { background: var(--spruce-soft); border-color: transparent; }
  .bar { width: 8px; align-self: stretch; min-height: 32px; border-radius: 3px; background: #7FA393; flex-shrink: 0; }
  .bar.uncertain { background: var(--goldfinch); }
  .sighting-main {
    flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; text-align: left; border: none;
    background: none; padding: 0; cursor: pointer; color: var(--ink);
  }
  .name { font-size: 16px; font-weight: 700; }
  .small-btn { min-height: 36px; padding: 0 12px; font-size: 13px; flex-shrink: 0; }
  .rank { display: grid; grid-template-columns: minmax(0, 1fr) minmax(60px, 1.2fr) 48px; align-items: center; gap: 10px; font-size: 14px; }
  .rank.top { font-weight: 700; }
  .rank span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .track { height: 8px; border-radius: 4px; background: #ECE8DD; overflow: hidden; }
  .track span { display: block; height: 100%; background: #9DB4A8; }
  .rank.top .track span { background: var(--spruce); }
  .rank .mono { text-align: right; }

  @media (max-width: 1100px) { .cards { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 860px) {
    .stage { border-radius: 0; margin-inline: -16px; }
    .controls { gap: 8px; }
    .time { min-width: auto; font-size: 13px; }
  }
</style>
