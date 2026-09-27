<script lang="ts" module>
  /** What happened on one day, keyed by `dayKey` (local date). */
  export interface DayStats {
    recordings: number;
    /** Recordings with at least one identified species. */
    birds: number;
  }
</script>

<script lang="ts">
  import Icon from './Icon.svelte';

  let { days, selected = null, onselect }: {
    days: Map<string, DayStats>;
    selected?: string | null;
    onselect: (key: string | null) => void;
  } = $props();

  const WEEKDAYS = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];
  const monthIndex = (date: Date) => date.getFullYear() * 12 + date.getMonth();

  const dated = $derived([...days.keys()].map((key) => new Date(key)).filter((date) => !isNaN(date.getTime())));
  const first = $derived(dated.length ? Math.min(...dated.map(monthIndex)) : monthIndex(new Date()));
  const last = $derived(dated.length ? Math.max(...dated.map(monthIndex)) : monthIndex(new Date()));
  const busiest = $derived(Math.max(1, ...[...days.values()].map((day) => day.recordings)));

  // The month on show: the selected day's, else the newest month with recordings.
  let shown: number | null = $state(null);
  const month = $derived(shown ?? (selected ? monthIndex(new Date(selected)) : last));
  $effect(() => {
    if (selected) shown = monthIndex(new Date(selected));
  });

  const title = $derived(new Date(Math.floor(month / 12), month % 12, 1)
    .toLocaleDateString([], { month: 'long', year: 'numeric' }));
  const cells = $derived.by(() => {
    const year = Math.floor(month / 12), m = month % 12;
    const lead = new Date(year, m, 1).getDay();
    const count = new Date(year, m + 1, 0).getDate();
    const result: ({ day: number; key: string; stats: DayStats | undefined } | null)[] = Array(lead).fill(null);
    for (let day = 1; day <= count; day++) {
      const key = new Date(year, m, day).toDateString();
      result.push({ day, key, stats: days.get(key) });
    }
    return result;
  });
  const monthTotal = $derived(cells.reduce((sum, cell) => sum + (cell?.stats?.recordings ?? 0), 0));
  const today = new Date().toDateString();

  // Four shades, spread so a quiet day still shows against a busy month.
  const level = (count: number) => Math.max(1, Math.ceil(4 * Math.sqrt(count / busiest)));

  function describe(key: string, stats: DayStats | undefined): string {
    const date = new Date(key).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
    if (!stats) return `${date}: no recordings`;
    const recordings = `${stats.recordings} recording${stats.recordings === 1 ? '' : 's'}`;
    return stats.birds ? `${date}: ${recordings}, ${stats.birds} with birds` : `${date}: ${recordings}`;
  }
</script>

<div class="calendar">
  <div class="head">
    <button type="button" class="nav" aria-label="Previous month" disabled={month <= first} onclick={() => { shown = month - 1; }}>
      <Icon name="back" size={18} />
    </button>
    <p class="month"><span class="serif">{title}</span> <span class="mono muted">{monthTotal}</span></p>
    <button type="button" class="nav" aria-label="Next month" disabled={month >= last} onclick={() => { shown = month + 1; }}>
      <Icon name="forward" size={18} />
    </button>
  </div>
  <div class="grid" role="group" aria-label="Recordings by day, {title}">
    {#each WEEKDAYS as weekday, i (i)}<span class="weekday" aria-hidden="true">{weekday}</span>{/each}
    {#each cells as cell, i (cell?.key ?? `blank-${i}`)}
      {#if cell}
        <button
          type="button"
          class="day level-{cell.stats ? level(cell.stats.recordings) : 0}"
          class:today={cell.key === today}
          aria-pressed={cell.key === selected}
          aria-label={describe(cell.key, cell.stats)}
          title={describe(cell.key, cell.stats)}
          disabled={!cell.stats}
          onclick={() => onselect(cell.key === selected ? null : cell.key)}
        >
          <span class="number">{cell.day}</span>
          {#if cell.stats?.birds}<span class="bird" aria-hidden="true"></span>{/if}
        </button>
      {:else}
        <span></span>
      {/if}
    {/each}
  </div>
  <p class="legend muted">
    <span class="scale" aria-hidden="true">{#each [1, 2, 3, 4] as l (l)}<span class="swatch level-{l}"></span>{/each}</span>
    More recordings
    <span class="bird" aria-hidden="true"></span> Birds identified
  </p>
</div>

<style>
  .calendar { display: flex; flex-direction: column; gap: 6px; }
  .head { display: flex; align-items: center; justify-content: space-between; gap: 4px; }
  .month { margin: 0; font-size: 16px; font-weight: 600; display: flex; align-items: baseline; gap: 8px; }
  .month .mono { font-size: 12px; font-weight: 400; }
  .nav {
    width: 36px; height: 36px; display: grid; place-items: center; border-radius: 10px;
    border: 1px solid transparent; background: transparent; color: var(--spruce); cursor: pointer;
  }
  .nav:hover:not(:disabled) { background: var(--paper); }
  .nav:disabled { color: var(--line); cursor: default; }
  .grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 3px; }
  .weekday { text-align: center; font-size: 11px; font-weight: 700; color: var(--muted); padding-bottom: 2px; }
  .day {
    position: relative; height: 36px; border-radius: 8px; border: 1px solid transparent;
    font-family: var(--font-mono); font-size: 13px; color: var(--ink); cursor: pointer;
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
  }
  .day:disabled { color: #A7ADA5; cursor: default; background: transparent; }
  .day:hover:not(:disabled) { border-color: var(--spruce); }
  .day.today { border-color: var(--goldfinch); }
  .day[aria-pressed='true'] { background: var(--spruce); color: #FFFFFF; border-color: var(--spruce); }
  .day .bird { position: absolute; bottom: 4px; }
  .level-1 { background: color-mix(in srgb, var(--spruce) 10%, var(--card)); }
  .level-2 { background: color-mix(in srgb, var(--spruce) 22%, var(--card)); }
  .level-3 { background: color-mix(in srgb, var(--spruce) 36%, var(--card)); }
  .level-4 { background: color-mix(in srgb, var(--spruce) 52%, var(--card)); }
  .bird { display: inline-block; width: 5px; height: 5px; border-radius: 50%; background: var(--goldfinch); }
  .day[aria-pressed='true'] .bird { background: var(--goldfinch-box); }
  .legend { margin: 2px 0 0; font-size: 12px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .legend .bird { width: 7px; height: 7px; margin-left: 8px; }
  .scale { display: inline-flex; gap: 2px; }
  .swatch { width: 10px; height: 10px; border-radius: 3px; }
</style>
