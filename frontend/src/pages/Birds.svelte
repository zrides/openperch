<script lang="ts">
  import CorrectionDialog from '../lib/CorrectionDialog.svelte';
  import Icon from '../lib/Icon.svelte';
  import { api, photoSighting, UNCERTAIN, type Photo, type Sighting, type SpeciesSummary } from '../lib/api';
  import { clock, dateTime } from '../lib/format';
  import { hub } from '../lib/hub.svelte';

  let { species = null }: { species?: string | null } = $props();

  const PAGE = 100;
  let summaries: SpeciesSummary[] = $state([]);
  let photos: Photo[] = $state([]);
  let hasMore = $state(false);
  let best = $state(true);
  let selectedPath = $state<string | null>(null);
  let correcting = $state<Sighting | null>(null);
  let loading = $state(false);
  let error = $state('');

  const identified = $derived(summaries.filter((s) => s.species !== UNCERTAIN)
    .sort((a, b) => b.videos - a.videos || a.species.localeCompare(b.species)));
  const uncertain = $derived(summaries.find((s) => s.species === UNCERTAIN));
  const current = $derived(summaries.find((s) => s.species === species));
  const selected = $derived(photos.find((p) => p.path === selectedPath) ?? photos[0] ?? null);
  const totals = $derived({
    species: identified.length,
    photos: summaries.reduce((sum, s) => sum + s.photos, 0),
  });

  async function loadSummaries() {
    try {
      summaries = await api.get<SpeciesSummary[]>('/api/species');
      error = '';
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function loadPhotos(reset: boolean) {
    if (!species) return;
    const name = species;
    loading = true;
    try {
      const offset = reset ? 0 : photos.length;
      const rows = await api.get<Photo[]>(
        `/api/species/${encodeURIComponent(name)}/photos?offset=${offset}&limit=${PAGE}&include_duplicates=${!best}`);
      if (name !== species) return;
      photos = reset ? rows : [...photos, ...rows];
      hasMore = rows.length === PAGE;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  loadSummaries();

  // A new species or photo filter: start its gallery from the top.
  $effect(() => {
    void species;
    void best;
    photos = [];
    selectedPath = null;
    loadPhotos(true);
  });

  // Default to the most-visited species on wide screens.
  $effect(() => {
    if (!species && identified.length && matchMedia('(min-width: 861px)').matches) {
      history.replaceState(null, '', `#/birds/${encodeURIComponent(identified[0].species)}`);
      dispatchEvent(new HashChangeEvent('hashchange'));
    }
  });

  // Library changes (processing, corrections): refresh counts and the visible gallery.
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let seenChange = hub.changed;
  $effect(() => {
    if (hub.changed === seenChange) return;
    seenChange = hub.changed;
    refreshTimer ??= setTimeout(async () => {
      refreshTimer = undefined;
      await loadSummaries();
      const keep = selectedPath;
      await loadPhotos(true);
      if (keep && photos.some((p) => p.path === keep)) selectedPath = keep;
    }, 1500);
  });
  $effect(() => () => clearTimeout(refreshTimer));

  function note(photo: Photo) {
    if (photo.corrected) return 'Your correction';
    if (!photo.evidence) return 'Earlier analysis';
    return photo.evidence.reason;
  }

  function open(photo: Photo) {
    selectedPath = photo.path;
  }
</script>

<div class="page" class:has-species={!!species} class:has-photo={!!selectedPath}>
  <aside class="card species" aria-label="Species">
    <div class="species-head">
      <h1 class="serif">Birds</h1>
      <span class="muted small">{totals.species} species · {totals.photos.toLocaleString()} photos</span>
    </div>
    {#if error}<p class="error">{error}</p>{/if}
    {#if uncertain}
      <p class="eyebrow group">Needs review</p>
      <a class="sp review" href="#/birds/{encodeURIComponent(UNCERTAIN)}" aria-current={species === UNCERTAIN ? 'page' : undefined}>
        <span>{UNCERTAIN}</span><span class="mono count">{uncertain.videos}</span>
      </a>
    {/if}
    <p class="eyebrow group">Identified · recordings</p>
    {#each identified as s (s.species)}
      <a class="sp" href="#/birds/{encodeURIComponent(s.species)}" aria-current={species === s.species ? 'page' : undefined}>
        <span>{s.species}</span><span class="mono count">{s.videos}</span>
      </a>
    {:else}
      <p class="muted small empty">No identified birds yet. Photos appear as recordings are processed.</p>
    {/each}
  </aside>

  <section class="gallery" aria-labelledby="species-title">
    {#if species}
      <a class="back" href="#/birds"><Icon name="back" size={20} />All species</a>
      <header class="gallery-head">
        <div>
          <h2 id="species-title" class="serif">{species}</h2>
          {#if current}
            <p class="muted small">{current.videos} recordings · {current.photos.toLocaleString()} photos · {current.featured} featured</p>
          {/if}
        </div>
        <div class="segmented" role="group" aria-label="Photos to show">
          <button type="button" aria-pressed={best} onclick={() => { best = true; }}>Best photos</button>
          <button type="button" aria-pressed={!best} onclick={() => { best = false; }}>All photos</button>
        </div>
      </header>
      <div class="grid">
        {#each photos as photo (photo.path)}
          <button type="button" class="tile" aria-pressed={selected?.path === photo.path} onclick={() => open(photo)}>
            <img src="/api/photos/{photo.path}?size=thumb" alt="{photo.species}, {dateTime(photo.recorded_at)}" loading="lazy">
            <span class="caption">{dateTime(photo.recorded_at) || photo.name} <span class="mono muted">{clock(photo.ms)}</span></span>
          </button>
        {:else}
          {#if !loading}<p class="muted">No photos for this species{best ? ' among the best photos' : ''}.</p>{/if}
        {/each}
      </div>
      {#if hasMore}
        <button type="button" class="btn more" disabled={loading} onclick={() => loadPhotos(false)}>{loading ? 'Loading…' : 'Load more photos'}</button>
      {/if}
    {/if}
  </section>

  {#if selected}
    <aside class="card detail" aria-labelledby="detail-title">
      <button type="button" class="close" aria-label="Close photo" onclick={() => { selectedPath = null; }}>×</button>
      <a href="/api/photos/{selected.path}" target="_blank" rel="noopener" class="full" aria-label="Open the full photo">
        <img src="/api/photos/{selected.path}" alt="{selected.species}, {dateTime(selected.recorded_at)}">
      </a>
      <div>
        <p class="eyebrow">{selected.corrected ? 'Your correction' : 'AI sighting'}</p>
        <h3 id="detail-title" class="serif">{selected.species}</h3>
        <p class="muted small">
          {dateTime(selected.recorded_at)}{#if selected.start_ms !== null}&nbsp;·&nbsp;<span class="mono">{clock(selected.start_ms)}–{clock(selected.end_ms ?? selected.ms)}</span>{/if}
        </p>
      </div>
      {#if selected.evidence}
        <dl>
          <div><dt>Sampled views agreeing</dt><dd class="mono">{selected.evidence.support} of {selected.evidence.samples}</dd></div>
          <div><dt>Average match</dt><dd class="mono">{selected.evidence.similarity.toFixed(3)}</dd></div>
          <div><dt>Lead over next species</dt><dd class="mono">{selected.evidence.margin.toFixed(3)}</dd></div>
          {#if selected.species === UNCERTAIN && selected.evidence.suggestion}
            <div><dt>AI suggestion</dt><dd>{selected.evidence.suggestion}</dd></div>
          {/if}
        </dl>
      {/if}
      <p class="muted small">{note(selected)}. Agreement is not a probability. Corrections relabel this sighting only.</p>
      <div class="actions">
        <a class="btn btn-primary" href="#/recordings/{selected.video_id}?t={Math.round(selected.ms)}"><Icon name="play" size={16} />Play this moment</a>
        {#if photoSighting(selected)}
          <button type="button" class="btn" onclick={() => { correcting = photoSighting(selected); }}>
            <Icon name="pencil" size={16} />{selected.species === UNCERTAIN ? 'Identify this bird…' : 'Correct this sighting…'}
          </button>
        {/if}
      </div>
    </aside>
  {/if}
</div>

{#if correcting}
  <CorrectionDialog sighting={correcting} context={dateTime(selected?.recorded_at ?? null)} onclose={() => { correcting = null; }} />
{/if}

<style>
  .page {
    padding: 28px 36px; display: grid; grid-template-columns: 260px minmax(0, 1fr) 320px; gap: 20px; align-items: start;
  }
  .species {
    position: sticky; top: 28px; max-height: calc(100dvh - 56px); overflow-y: auto; padding: 16px 8px;
    display: flex; flex-direction: column; gap: 2px;
  }
  .species-head { padding: 0 12px 8px; display: flex; flex-direction: column; gap: 2px; }
  h1 { margin: 0; font-size: 26px; font-weight: 600; }
  .small { font-size: 13px; margin: 0; }
  .group { margin: 0; padding: 12px 12px 4px; }
  .empty { padding: 4px 12px; }
  .sp {
    display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 40px; padding: 0 12px;
    border-radius: 10px; text-decoration: none; color: var(--ink); font-size: 15px;
  }
  .sp:hover { background: var(--paper); color: var(--ink); }
  .sp.review { background: var(--warn-bg); font-weight: 600; }
  .sp[aria-current='page'] { background: var(--spruce); color: #FFFFFF; font-weight: 600; }
  .count { font-size: 13px; color: var(--muted); }
  .sp.review .count { color: var(--warn-ink); }
  .sp[aria-current='page'] .count { color: #FFFFFF; }

  .gallery { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
  .gallery-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  h2 { margin: 0; font-size: 34px; font-weight: 600; line-height: 1.1; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }
  .tile {
    display: flex; flex-direction: column; gap: 6px; padding: 0; border: none; background: none; text-align: left;
    cursor: pointer; color: var(--ink);
  }
  .tile img {
    width: 100%; aspect-ratio: 4 / 3; object-fit: cover; border-radius: 12px; background: #D6CDB7; display: block;
  }
  .tile[aria-pressed='true'] img { outline: 3px solid var(--spruce); outline-offset: 2px; }
  .caption { font-size: 13px; font-weight: 600; }
  .more { align-self: center; }
  .back { display: none; }

  .detail { position: sticky; top: 28px; padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .close { display: none; }
  .full img { width: 100%; max-height: 260px; object-fit: contain; border-radius: 12px; background: #D6CDB7; display: block; }
  h3 { margin: 0; font-size: 26px; font-weight: 600; }
  dl {
    margin: 0; padding: 10px 0; display: flex; flex-direction: column; gap: 8px; font-size: 14px;
    border-top: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft);
  }
  dl div { display: flex; justify-content: space-between; gap: 12px; }
  dt { color: var(--muted); }
  dd { margin: 0; }
  .actions { display: flex; flex-direction: column; gap: 10px; }
  .error { margin: 8px; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }

  @media (max-width: 1200px) {
    .page { grid-template-columns: 240px minmax(0, 1fr); }
    .detail {
      position: fixed; right: 16px; bottom: 16px; width: 340px; max-height: calc(100dvh - 32px); overflow-y: auto; z-index: 20;
      box-shadow: 0 12px 40px rgba(27, 36, 32, 0.22);
    }
    .page:not(.has-photo) .detail { display: none; }
    .close {
      display: flex; align-items: center; justify-content: center; position: absolute; top: 8px; right: 8px; z-index: 1;
      width: 40px; height: 40px; border-radius: 50%; border: none; background: rgba(27, 36, 32, 0.6); color: #FFFFFF;
      font-size: 24px; cursor: pointer;
    }
  }
  @media (max-width: 860px) {
    .page { grid-template-columns: minmax(0, 1fr); padding: 16px; }
    .species { position: static; max-height: none; }
    .has-species .species { display: none; }
    .page:not(.has-species) .gallery { display: none; }
    .back { display: inline-flex; align-items: center; gap: 6px; min-height: 44px; font-weight: 600; text-decoration: none; }
    h2 { font-size: 28px; }
    .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .detail {
      left: 0; right: 0; bottom: 0; width: auto; border-radius: 22px 22px 0 0; max-height: 85dvh;
      padding-bottom: calc(16px + env(safe-area-inset-bottom));
    }
  }
</style>
