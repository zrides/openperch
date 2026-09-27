<script lang="ts">
  import DayCalendar, { type DayStats } from '../lib/DayCalendar.svelte';
  import Icon from '../lib/Icon.svelte';
  import Player from '../lib/Player.svelte';
  import { api, UNCERTAIN, type Recording } from '../lib/api';
  import { dayKey, dayLabel, duration, timeOfDay } from '../lib/format';
  import { hub } from '../lib/hub.svelte';

  let { id = null, startMs = 0 }: { id?: string | null; startMs?: number } = $props();

  type Filter = 'all' | 'birds' | 'uncertain' | 'unprocessed';
  const FILTERS: { value: Filter; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'birds', label: 'With birds' },
    { value: 'uncertain', label: 'Uncertain' },
    { value: 'unprocessed', label: 'Not processed' },
  ];
  const PAGE = 150;

  let recordings: Recording[] = $state([]);
  let loaded = $state(false);
  let error = $state('');
  let query = $state('');
  let filter: Filter = $state('all');
  let shownCount = $state(PAGE);
  let day: string | null = $state(null);

  // The calendar stays open unless it was closed on this device.
  const CALENDAR_KEY = 'kiwibit.recordings.calendar';
  let calendarOpen = $state(readCalendarOpen());
  function readCalendarOpen(): boolean {
    try {
      return localStorage.getItem(CALENDAR_KEY) !== 'closed';
    } catch {
      return true;
    }
  }
  function toggleCalendar() {
    calendarOpen = !calendarOpen;
    try {
      localStorage.setItem(CALENDAR_KEY, calendarOpen ? 'open' : 'closed');
    } catch {
      /* storage unavailable: remember for this visit only */
    }
  }

  async function load() {
    try {
      recordings = await api.get<Recording[]>('/api/recordings');
      error = '';
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loaded = true;
    }
  }
  load();

  // Refresh as processing finishes videos: at most every few seconds, but never
  // postponed indefinitely (processing reports changes continuously).
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let seenChange = hub.changed;
  $effect(() => {
    if (hub.changed === seenChange) return;
    seenChange = hub.changed;
    refreshTimer ??= setTimeout(() => {
      refreshTimer = undefined;
      load();
    }, 1500);
  });
  $effect(() => () => clearTimeout(refreshTimer));

  const identified = (r: Recording) => r.species.filter((name) => name !== UNCERTAIN);
  function matches(r: Recording): boolean {
    if (filter === 'birds' && !r.species.length) return false;
    if (filter === 'uncertain' && !r.species.includes(UNCERTAIN)) return false;
    if (filter === 'unprocessed' && r.status === 'ready') return false;
    const text = query.trim().toLowerCase();
    if (!text) return true;
    const haystack = [r.name, r.status, ...r.species, ...r.animals, dayLabel(r.recorded_at), timeOfDay(r.recorded_at),
      r.recorded_at ? new Date(r.recorded_at).toLocaleDateString([], { day: 'numeric', month: 'long' }) : '']
      .join(' ').toLowerCase();
    return text.split(/\s+/).every((word) => haystack.includes(word));
  }

  const filtered = $derived(recordings.filter(matches));
  const listed = $derived(day ? filtered.filter((r) => dayKey(r.recorded_at) === day) : filtered);
  const groups = $derived.by(() => {
    const result: { key: string; label: string; items: Recording[] }[] = [];
    for (const r of listed.slice(0, shownCount)) {
      const key = dayKey(r.recorded_at);
      let group = result[result.length - 1];
      if (!group || group.key !== key) {
        group = { key, label: dayLabel(r.recorded_at), items: [] };
        result.push(group);
      }
      group.items.push(r);
    }
    return result;
  });
  // Per-day totals under the current search and filter, for the calendar and the day headings.
  const days = $derived.by(() => {
    const result = new Map<string, DayStats>();
    for (const r of filtered) {
      const key = dayKey(r.recorded_at);
      const stats = result.get(key) ?? { recordings: 0, birds: 0 };
      stats.recordings += 1;
      if (identified(r).length) stats.birds += 1;
      result.set(key, stats);
    }
    return result;
  });

  let itemsElement: HTMLDivElement | undefined = $state();
  function pickDay(key: string | null) {
    day = key;
    shownCount = PAGE;
    itemsElement?.scrollTo({ top: 0 });
    // On a wide screen, open the day's newest recording beside the list.
    const firstOfDay = key && filtered.find((r) => dayKey(r.recorded_at) === key);
    if (firstOfDay && matchMedia('(min-width: 861px)').matches) location.hash = `#/recordings/${firstOfDay.id}`;
  }

  // Without a selection on a wide screen, open the newest recording.
  const selected = $derived(recordings.find((r) => r.id === id) ?? null);
  $effect(() => {
    if (!id && recordings.length && matchMedia('(min-width: 861px)').matches) {
      history.replaceState(null, '', `#/recordings/${recordings[0].id}`);
      dispatchEvent(new HashChangeEvent('hashchange'));
    }
  });

  function chips(r: Recording) {
    if (r.status === 'failed') return [{ text: 'Processing failed', kind: 'error' }];
    if (r.status !== 'ready') return [{ text: r.status === 'processing' ? 'Processing…' : 'Not processed', kind: 'plain' }];
    const animals = r.animals.map((text) => ({ text, kind: 'animal' }));
    if (!r.species.length) return animals.length ? animals : [{ text: 'No birds detected', kind: 'plain' }];
    const names = identified(r);
    const result = names.slice(0, 2).map((text) => ({ text, kind: 'ok' }));
    if (names.length > 2) result.push({ text: `+${names.length - 2}`, kind: 'ok' });
    if (r.species.includes(UNCERTAIN)) result.push({ text: names.length ? 'Uncertain' : UNCERTAIN, kind: 'warn' });
    return [...result, ...animals];
  }
</script>

<div class="page" class:has-selection={!!id}>
  <aside class="card list" aria-label="Recordings">
    <div class="list-head">
      <div class="title-row">
        <h1 class="serif">Recordings</h1>
        <span class="mono muted">{recordings.length}</span>
      </div>
      <label class="search">
        <span class="visually-hidden">Search recordings</span>
        <Icon name="search" size={18} />
        <input type="search" bind:value={query} placeholder="Search species, date or time" oninput={() => { shownCount = PAGE; }}>
      </label>
      <div class="filters" role="group" aria-label="Filter recordings">
        {#each FILTERS as option (option.value)}
          <button type="button" aria-pressed={filter === option.value} onclick={() => { filter = option.value; shownCount = PAGE; }}>{option.label}</button>
        {/each}
        <button type="button" class="calendar-toggle" aria-expanded={calendarOpen} aria-controls="recordings-calendar" onclick={toggleCalendar}>
          <Icon name="calendar" size={16} />Calendar
        </button>
      </div>
      {#if calendarOpen}
        <div id="recordings-calendar">
          <DayCalendar {days} selected={day} onselect={pickDay} />
        </div>
      {/if}
      {#if day}
        <div class="day-filter">
          <span>Showing <strong>{dayLabel(new Date(day).toISOString())}</strong></span>
          <button type="button" onclick={() => pickDay(null)}><Icon name="close" size={16} />All days</button>
        </div>
      {/if}
    </div>
    <div class="items" bind:this={itemsElement}>
      {#if error}<p class="error">{error}</p>{/if}
      {#each groups as group (group.key)}
        <p class="eyebrow day">{group.label} · {days.get(group.key)?.recordings}</p>
        {#each group.items as r (r.id)}
          <a class="item" href="#/recordings/{r.id}" aria-current={r.id === id ? 'true' : undefined}>
            <span class="item-row">
              <span class="mono when">{timeOfDay(r.recorded_at) || r.name}</span>
              <span class="mono muted small">{duration(r.duration)}</span>
            </span>
            <span class="chips">
              {#each chips(r) as chip (chip.text)}
                <span class="chip" class:chip-warn={chip.kind === 'warn'} class:chip-plain={chip.kind === 'plain'} class:chip-error={chip.kind === 'error'} class:chip-animal={chip.kind === 'animal'}>{chip.text}</span>
              {/each}
            </span>
          </a>
        {/each}
      {:else}
        {#if loaded && !error}<p class="muted empty">{recordings.length ? (day ? 'No recordings on this day match.' : 'No recordings match.') : 'No recordings yet. Fetch them from the cloud on the Home page.'}</p>{/if}
      {/each}
      {#if listed.length > shownCount}
        <button type="button" class="btn more" onclick={() => { shownCount += PAGE; }}>Show {Math.min(PAGE, listed.length - shownCount)} more</button>
      {/if}
    </div>
  </aside>

  <div class="detail">
    {#if selected}
      <a class="back" href="#/recordings"><Icon name="back" size={20} />All recordings</a>
      {#key selected.id}
        <Player recording={selected} {startMs} />
      {/key}
    {:else if id && loaded}
      <p class="muted">This recording is not in the library.</p>
    {/if}
  </div>
</div>

<style>
  .page {
    padding: 28px 36px; display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 24px; align-items: start;
  }
  .list {
    position: sticky; top: 28px; max-height: calc(100vh - 56px); max-height: calc(100dvh - 56px);
    display: flex; flex-direction: column; overflow: hidden;
  }
  .list-head { padding: 18px 16px 12px; display: flex; flex-direction: column; gap: 12px; border-bottom: 1px solid var(--line-soft); }
  .title-row { display: flex; align-items: baseline; justify-content: space-between; }
  h1 { margin: 0; font-size: 26px; font-weight: 600; }
  .filters .calendar-toggle { margin-left: auto; color: var(--spruce); }
  .filters .calendar-toggle[aria-expanded='true'] { background: var(--spruce-soft); border-color: var(--spruce-soft); }
  .calendar-toggle, .day-filter button {
    display: inline-flex; align-items: center; gap: 6px; min-height: 36px; padding: 0 12px; border-radius: 999px;
    border: 1px solid var(--control-line); background: var(--card); color: var(--spruce);
    font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .day-filter {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 6px 6px 6px 12px; border-radius: 12px; background: var(--spruce-soft); font-size: 14px;
  }
  .search {
    display: flex; align-items: center; gap: 8px; height: 42px; padding: 0 12px; border-radius: 10px;
    border: 1px solid var(--control-line); background: #FFFFFF; color: var(--muted);
  }
  .search input { flex-grow: 1; min-width: 0; border: none; outline: none; background: transparent; font-size: 14px; color: var(--ink); }
  .search:focus-within { outline: 3px solid var(--goldfinch); outline-offset: 1px; }
  .filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .filters button {
    min-height: 36px; padding: 0 12px; border-radius: 999px; border: 1px solid var(--control-line); background: var(--card);
    font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .filters button[aria-pressed='true'] { background: var(--spruce); border-color: var(--spruce); color: #FFFFFF; }
  .items { padding: 8px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
  .day { margin: 0; padding: 10px 12px 4px; }
  .item {
    display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border-radius: 12px;
    text-decoration: none; color: var(--ink);
  }
  .item:hover { background: var(--paper); color: var(--ink); }
  .item[aria-current='true'] { background: var(--spruce-soft); }
  .item-row { display: flex; justify-content: space-between; align-items: baseline; }
  .when { font-size: 15px; font-weight: 500; }
  .small { font-size: 13px; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .item[aria-current='true'] .chip:not(.chip-warn):not(.chip-plain):not(.chip-error):not(.chip-animal) { background: var(--card); }
  :global(.chip-error) { background: #F7E1DA !important; color: #7A2A18 !important; }
  .more { margin: 8px; }
  .empty { padding: 12px; margin: 0; }
  .error { margin: 8px; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }
  .detail { min-width: 0; }
  .back { display: none; }

  @media (max-width: 860px) {
    .page { grid-template-columns: minmax(0, 1fr); padding: 16px; }
    .list { position: static; max-height: none; }
    .has-selection .list { display: none; }
    .page:not(.has-selection) .detail { display: none; }
    .back {
      display: inline-flex; align-items: center; gap: 6px; min-height: 44px; margin-bottom: 4px;
      font-weight: 600; text-decoration: none;
    }
  }
</style>
