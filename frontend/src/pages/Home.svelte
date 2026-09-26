<script lang="ts">
  import Icon from '../lib/Icon.svelte';
  import { api, ApiError, UNCERTAIN, type FetchSchedule, type Recording, type SpeciesSummary } from '../lib/api';
  import { dateTime, dayLabel, duration, timeOfDay } from '../lib/format';
  import { hub } from '../lib/hub.svelte';

  let recordings: Recording[] = $state([]);
  let species: SpeciesSummary[] = $state([]);
  let loaded = $state(false);
  let notice = $state('');
  let schedule = $state<FetchSchedule | null>(null);

  async function load() {
    const [rows, summaries, fetchSchedule] = await Promise.allSettled([
      api.get<Recording[]>('/api/recordings'),
      api.get<SpeciesSummary[]>('/api/species'),
      api.get<FetchSchedule>('/api/settings/fetch'),
    ]);
    if (fetchSchedule.status === 'fulfilled') schedule = fetchSchedule.value;
    if (rows.status === 'fulfilled') recordings = rows.value;
    if (summaries.status === 'fulfilled') species = summaries.value;
    loaded = true;
  }
  load();

  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let seenChange = hub.changed;
  $effect(() => {
    if (hub.changed === seenChange) return;
    seenChange = hub.changed;
    refreshTimer ??= setTimeout(() => { refreshTimer = undefined; load(); }, 3000);
  });
  $effect(() => () => clearTimeout(refreshTimer));

  const DAY = 86_400_000;
  const startOfDay = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const now = new Date();
  const todayStart = startOfDay(now).getTime();

  const dated = $derived(recordings.filter((r) => r.recorded_at));
  const today = $derived(dated.filter((r) => new Date(r.recorded_at!).getTime() >= todayStart));
  const latest = $derived(recordings.slice(0, 4));
  const identifiedSpecies = $derived(species.filter((s) => s.species !== UNCERTAIN));
  const photos = $derived(species.reduce((sum, s) => sum + s.photos, 0));
  const needsReview = $derived(recordings.filter((r) => r.species.includes(UNCERTAIN)).length);
  const processing = $derived(recordings.filter((r) => r.status !== 'ready' && r.status !== 'failed').length);
  const range = $derived(dated.length
    ? `${new Date(dated[dated.length - 1].recorded_at!).toLocaleDateString([], { day: 'numeric', month: 'short' })} – ${new Date(dated[0].recorded_at!).toLocaleDateString([], { day: 'numeric', month: 'short' })}`
    : '');

  const week = $derived.by(() => {
    const days = Array.from({ length: 7 }, (_, index) => {
      const start = todayStart - (6 - index) * DAY;
      return { start, count: 0, label: index === 6 ? 'Today' : new Date(start).toLocaleDateString([], { weekday: 'short', day: 'numeric' }) };
    });
    for (const r of dated) {
      const time = new Date(r.recorded_at!).getTime();
      const index = Math.floor((startOfDay(new Date(time)).getTime() - days[0].start) / DAY);
      if (index >= 0 && index < 7) days[index].count++;
    }
    const max = Math.max(1, ...days.map((d) => d.count));
    const peak = Math.max(...days.map((d) => d.count));
    return days.map((d) => ({ ...d, height: (d.count / max) * 100, peak: d.count > 0 && d.count === peak }));
  });
  const weekTotal = $derived(week.reduce((sum, d) => sum + d.count, 0));

  const top = $derived.by(() => {
    const counts = new Map<string, number>();
    for (const r of recordings) for (const name of new Set(r.species)) if (name !== UNCERTAIN) counts.set(name, (counts.get(name) ?? 0) + 1);
    const rows = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 7);
    const max = Math.max(1, ...rows.map(([, count]) => count));
    return rows.map(([name, count]) => ({ name, count, width: (count / max) * 100 }));
  });

  const hours = $derived.by(() => {
    const counts = Array<number>(24).fill(0);
    for (const r of dated) counts[new Date(r.recorded_at!).getHours()]++;
    const active = counts.map((count, hour) => ({ count, hour })).filter((h) => h.count > 0);
    if (!active.length) return { bars: [], peak: null as number | null, first: 0, last: 0 };
    const first = active[0].hour;
    const last = active[active.length - 1].hour;
    const max = Math.max(...counts);
    const bars = counts.slice(first, last + 1).map((count, offset) => ({
      hour: first + offset, count, height: (count / max) * 100, peak: count === max,
    }));
    return { bars, peak: bars.find((b) => b.peak)?.hour ?? null, first, last };
  });
  const hourLabel = (hour: number) => new Date(2000, 0, 1, hour).toLocaleTimeString([], { hour: 'numeric' });

  const summary = $derived.by(() => {
    if (!loaded) return '';
    if (!today.length) {
      return dated.length ? `No visits yet today · last recording ${dateTime(dated[0].recorded_at)}` : 'No recordings yet';
    }
    const named = today.find((r) => r.species.some((name) => name !== UNCERTAIN));
    const latestBird = named?.species.find((name) => name !== UNCERTAIN);
    return `${today.length} recording${today.length === 1 ? '' : 's'} so far${latestBird ? ` · latest identified: ${latestBird} at ${timeOfDay(named!.recorded_at)}` : ''}`;
  });

  const clockTime = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const scheduleText = $derived.by(() => {
    if (!schedule) return '';
    const parts = [];
    if (schedule.last_finished) parts.push(`Cloud last checked ${dateTime(new Date(schedule.last_finished * 1000).toISOString())}`);
    if (schedule.auto && schedule.next_run) {
      parts.push(schedule.next_run * 1000 <= Date.now() ? 'next check when the hub is free' : `next at ${clockTime(schedule.next_run)}`);
    } else if (!schedule.auto) {
      parts.push('automatic fetching is off');
    }
    return parts.join(' · ');
  });

  async function fetchNow() {
    notice = '';
    try {
      await api.post('/api/jobs', { action: 'fetch' });
      notice = 'Checking the cloud for new recordings. New ones are downloaded and processed automatically.';
    } catch (e) {
      notice = e instanceof ApiError && e.status === 409
        ? 'The hub is busy with another job. Try again when it finishes.'
        : (e as Error).message;
    }
  }

  function chipsFor(r: Recording) {
    if (r.status !== 'ready') return [{ text: r.status === 'failed' ? 'Processing failed' : 'Not processed yet', kind: 'plain' }];
    if (!r.species.length) return [{ text: 'No birds detected', kind: 'plain' }];
    return r.species.slice(0, 3).map((name) => ({ text: name, kind: name === UNCERTAIN ? 'warn' : 'ok' }));
  }
</script>

<div class="page">
  <header class="head">
    <div>
      <p class="eyebrow">{now.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' })}</p>
      <h1 class="serif">Today at the feeder</h1>
      <p class="muted lead">{summary}</p>
    </div>
    <div class="actions">
      <a class="btn" href="#/live"><Icon name="live" size={18} />Live view</a>
      <button type="button" class="btn btn-primary" onclick={fetchNow} disabled={hub.job.running}>
        <Icon name="refresh" size={18} />{hub.job.running ? 'Hub is working…' : 'Fetch new recordings'}
      </button>
    </div>
  </header>
  {#if notice}<p class="notice" aria-live="polite">{notice}</p>{/if}
  {#if scheduleText}<p class="muted small schedule">{scheduleText}</p>{/if}

  <a class="card live-card" href="#/live">
    <span>Watch the feeder live</span>
    <span class="live-cta"><Icon name="wake" size={18} />Wake and watch</span>
  </a>

  <section class="tiles" aria-label="Library totals">
    <div class="card tile">
      <span class="eyebrow">Recordings</span>
      <span class="serif value">{recordings.length.toLocaleString()}</span>
      <span class="muted">{today.length} today{range ? ` · ${range}` : ''}</span>
    </div>
    <div class="card tile">
      <span class="eyebrow">Species</span>
      <span class="serif value">{identifiedSpecies.length}</span>
      <span class="muted">identified with confidence</span>
    </div>
    <div class="card tile">
      <span class="eyebrow">Bird photos</span>
      <span class="serif value">{photos.toLocaleString()}</span>
      <span class="muted">{processing ? `${processing} recordings still processing` : 'best 3 featured per sighting'}</span>
    </div>
    <a class="card tile review" href="#/birds/{encodeURIComponent(UNCERTAIN)}">
      <span class="eyebrow">Needs review</span>
      <span class="serif value">{needsReview.toLocaleString()}</span>
      <span>recordings with an uncertain bird →</span>
    </a>
  </section>

  <div class="columns">
    <div class="column">
      <section class="card panel" aria-labelledby="latest-title">
        <div class="panel-head">
          <h2 id="latest-title" class="serif">Latest visitors</h2>
          <a href="#/recordings">All recordings</a>
        </div>
        {#each latest as r (r.id)}
          <a class="visit" href="#/recordings/{r.id}">
            {#if r.cover}
              <img src="/api/photos/{r.cover}?size=thumb" alt="" loading="lazy">
            {:else}
              <span class="cover-placeholder"><Icon name="play" size={18} /></span>
            {/if}
            <span class="visit-when">
              <span class="mono">{timeOfDay(r.recorded_at) || r.name}</span>
              <span class="mono muted small">{dayLabel(r.recorded_at)}{r.duration ? ` · ${duration(r.duration)}` : ''}</span>
            </span>
            <span class="chips">
              {#each chipsFor(r) as chip (chip.text)}
                <span class="chip" class:chip-warn={chip.kind === 'warn'} class:chip-plain={chip.kind === 'plain'}>{chip.text}</span>
              {/each}
            </span>
          </a>
        {:else}
          <p class="muted">{loaded ? 'No recordings yet. Use Fetch new recordings to download them from the cloud.' : 'Loading…'}</p>
        {/each}
      </section>

      <section class="card panel" aria-labelledby="week-title">
        <div class="panel-head">
          <h2 id="week-title" class="serif">Recordings, last 7 days</h2>
          <span class="muted small">{weekTotal} total</span>
        </div>
        <div class="week" role="list">
          {#each week as day (day.start)}
            <div class="day" role="listitem" aria-label="{day.label}: {day.count} recordings">
              <span class="mono small">{day.count}</span>
              <span class="bar" class:peak={day.peak} style:height="{Math.max(day.count ? 3 : 0, day.height)}%"></span>
              <span class="small muted" class:today={day.label === 'Today'}>{day.label}</span>
            </div>
          {/each}
        </div>
      </section>
    </div>

    <div class="column">
      <section class="card panel" aria-labelledby="top-title">
        <div class="panel-head">
          <h2 id="top-title" class="serif">Top visitors</h2>
          <span class="muted small">recordings</span>
        </div>
        {#each top as row (row.name)}
          <a class="top-row" href="#/birds/{encodeURIComponent(row.name)}">
            <span>{row.name}</span>
            <span class="track"><span style:width="{row.width}%"></span></span>
            <span class="mono">{row.count}</span>
          </a>
        {:else}
          <p class="muted small">Identified birds appear here as recordings are processed.</p>
        {/each}
      </section>

      <section class="card panel" aria-labelledby="hours-title">
        <div class="panel-head">
          <h2 id="hours-title" class="serif">Busiest hours</h2>
          {#if hours.peak !== null}<span class="muted small">peak {hourLabel(hours.peak)}</span>{/if}
        </div>
        {#if hours.bars.length}
          <div class="hours" role="list">
            {#each hours.bars as bar (bar.hour)}
              <span class="hour" class:peak={bar.peak} role="listitem" aria-label="{hourLabel(bar.hour)}: {bar.count} recordings"
                    title="{hourLabel(bar.hour)}: {bar.count}" style:height="{Math.max(3, bar.height)}%"></span>
            {/each}
          </div>
          <div class="hour-labels small muted"><span>{hourLabel(hours.first)}</span><span>{hourLabel(hours.last)}</span></div>
        {:else}
          <p class="muted small">Not enough recordings yet.</p>
        {/if}
      </section>
    </div>
  </div>
</div>

<style>
  .page { padding: 36px 40px; display: flex; flex-direction: column; gap: 24px; }
  .head { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
  .eyebrow { margin: 0; }
  h1 { margin: 6px 0; font-size: 40px; font-weight: 600; line-height: 1.1; }
  .lead { margin: 0; font-size: 16px; }
  .actions { display: flex; gap: 12px; flex-wrap: wrap; }
  .notice { margin: 0; padding: 10px 14px; border-radius: 12px; background: var(--spruce-soft); color: var(--spruce); font-size: 14px; }
  .small { font-size: 13px; }
  .schedule { margin: -12px 0 0; }
  .live-card { display: none; }

  .tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
  .tile { padding: 18px 20px; display: flex; flex-direction: column; gap: 6px; text-decoration: none; color: var(--ink); }
  .value { font-size: 36px; font-weight: 600; line-height: 1; }
  .review { background: var(--warn-bg); border-color: #E8D08A; color: var(--warn-ink); }
  .review .eyebrow { color: var(--warn-ink); }
  .review .value { color: var(--ink); }

  .columns { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 24px; }
  .column { display: flex; flex-direction: column; gap: 24px; min-width: 0; }
  .panel { padding: 16px 20px; display: flex; flex-direction: column; gap: 10px; }
  .panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  h2 { margin: 0; font-size: 22px; font-weight: 600; }
  .panel-head a { font-size: 14px; font-weight: 600; }

  .visit {
    display: flex; align-items: center; gap: 16px; padding: 10px 0; border-top: 1px solid var(--line-soft);
    text-decoration: none; color: var(--ink);
  }
  .visit img, .cover-placeholder {
    width: 96px; height: 60px; border-radius: 8px; object-fit: cover; flex-shrink: 0; background: #D6CDB7;
  }
  .cover-placeholder { display: flex; align-items: center; justify-content: center; color: #4A4536; }
  .visit-when { display: flex; flex-direction: column; gap: 2px; width: 120px; flex-shrink: 0; }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; }

  .week { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 14px; height: 170px; }
  .day { display: flex; flex-direction: column; align-items: center; justify-content: flex-end; gap: 6px; min-height: 0; }
  .bar { width: 100%; border-radius: 6px 6px 2px 2px; background: var(--spruce); max-height: 110px; }
  .bar.peak { background: var(--goldfinch); }
  .today { color: var(--ink); font-weight: 700; }

  .top-row {
    display: grid; grid-template-columns: minmax(0, 170px) minmax(0, 1fr) 40px; align-items: center; gap: 12px;
    min-height: 32px; font-size: 14px; text-decoration: none; color: var(--ink);
  }
  .top-row span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .track { height: 10px; border-radius: 5px; background: #ECE8DD; overflow: hidden; }
  .track span { display: block; height: 100%; background: var(--spruce); border-radius: 5px; }
  .top-row .mono { text-align: right; }

  .hours { display: flex; align-items: flex-end; gap: 5px; height: 110px; }
  .hour { flex: 1; border-radius: 4px 4px 1px 1px; background: var(--spruce); }
  .hour.peak { background: var(--goldfinch); }
  .hour-labels { display: flex; justify-content: space-between; }

  @media (max-width: 1100px) {
    .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .columns { grid-template-columns: minmax(0, 1fr); }
  }
  @media (max-width: 860px) {
    .page { padding: 24px 16px; gap: 16px; }
    h1 { font-size: 30px; }
    .actions .btn:first-child { display: none; }
    .live-card {
      display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px; height: 180px;
      background: radial-gradient(circle at 50% 38%, #2E3D35 0, var(--frame) 72%); color: #C9D3CC; border: none; text-decoration: none;
    }
    .live-cta {
      display: inline-flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 18px; border-radius: 12px;
      background: var(--paper); color: var(--spruce); font-weight: 700;
    }
    .tile:nth-child(2), .tile:nth-child(3) { display: none; }
    .value { font-size: 30px; }
    .visit img, .cover-placeholder { width: 64px; height: 44px; }
    .visit-when { width: auto; }
    .visit { flex-wrap: wrap; gap: 10px; }
  }
</style>
