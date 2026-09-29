<script lang="ts">
  import FieldGuide from '../lib/FieldGuide.svelte';
  import Icon from '../lib/Icon.svelte';
  import { fieldGuide } from '../lib/fieldGuide';
  import { api, ApiError, type ReviewItem, type ReviewScore, type SpeciesSummary, type Verdict } from '../lib/api';
  import { clock, dateTime } from '../lib/format';

  const SPECIAL: { verdict: Verdict; label: string; key: string }[] = [
    { verdict: 'not_bird', label: 'Not a bird', key: 'N' },
    { verdict: 'several', label: 'More than one animal', key: 'M' },
    { verdict: 'unsure', label: "Can't tell", key: 'U' },
  ];
  const QUICK = 9;
  // Fewer graded birds than this and one answer swings the score by several points.
  const MEANINGFUL = 20;

  let items: ReviewItem[] = $state([]);
  let loaded = $state(false);
  let index = $state(0);
  let tab = $state<'answer' | 'score'>('answer');
  let quick: string[] = $state([]);
  let labels: string[] = $state([]);
  let search = $state('');
  let showBox = $state(true);
  let building = $state(false);
  let saving = $state(false);
  let error = $state('');
  let score = $state<ReviewScore | null>(null);
  let scoreError = $state('');
  // Which batch the score covers: null for every moment.
  let scoreBatch = $state<number | null>(null);
  let confirmAdd = $state(false);
  let adding = $state(false);
  let picture: HTMLElement | undefined = $state();
  let guideOpen = $state(false);
  // Field-guide photo per quick species, once loaded; '' when Wikipedia has none.
  let thumbs: Record<string, string> = $state({});

  const item = $derived(items[index] ?? null);
  const answered = $derived(items.filter((i) => i.verdict !== null).length);
  const batches = $derived([...new Set(items.map((i) => i.batch))].sort((a, b) => a - b));
  const latest = $derived(batches[batches.length - 1] ?? 1);
  const latestItems = $derived(items.filter((i) => i.batch === latest));
  const latestAnswered = $derived(latestItems.filter((i) => i.verdict !== null).length);
  const allDone = $derived(items.length > 0 && answered === items.length);
  // Moments you already identified, by species: your own camera's examples for the field guide.
  const examples = $derived.by(() => {
    const bySpecies: Record<string, string[]> = {};
    for (const row of items) {
      if (row.verdict === 'species' && row.answer && row.id !== item?.id) (bySpecies[row.answer] ??= []).push(row.id);
    }
    return bySpecies;
  });

  const requested = new Set<string>();
  $effect(() => {
    for (const name of quick) {
      if (requested.has(name)) continue;
      requested.add(name);
      fieldGuide(name).then((entry) => { thumbs[name] = entry?.photos[0]?.src ?? ''; });
    }
  });

  const matches = $derived(search.trim().length >= 2
    ? labels.filter((label) => label.toLowerCase().includes(search.trim().toLowerCase())).slice(0, 6)
    : []);

  async function load() {
    try {
      const [rows, gallery] = await Promise.all([
        api.get<ReviewItem[]>('/api/review'),
        api.get<SpeciesSummary[]>('/api/species').catch(() => [] as SpeciesSummary[]),
      ]);
      items = rows;
      // Fixed for the visit so the number keys don't move: species already used, then the gallery's most seen.
      const used = new Map<string, number>();
      for (const row of rows) if (row.verdict === 'species' && row.answer) used.set(row.answer, (used.get(row.answer) ?? 0) + 1);
      const mine = [...used].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([name]) => name);
      const seen = gallery.filter((s) => !s.species.startsWith('Uncertain bird'))
        .sort((a, b) => b.videos - a.videos || a.species.localeCompare(b.species)).map((s) => s.species);
      quick = [...new Set([...mine, ...seen])].slice(0, QUICK);
      const first = rows.findIndex((row) => row.verdict === null);
      index = first >= 0 ? first : 0;
      error = '';
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loaded = true;
    }
  }

  load();
  api.get<string[]>('/api/labels').then((all) => { labels = all; }).catch(() => {});

  async function build() {
    building = true;
    error = '';
    try {
      await api.post('/api/review');
      await load();
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Could not pick the moments';
    } finally {
      building = false;
    }
  }

  async function addMoments() {
    adding = true;
    error = '';
    try {
      await api.post('/api/review/batch');
      confirmAdd = false;
      score = null;
      tab = 'answer';
      await load();
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Could not pick more moments';
    } finally {
      adding = false;
    }
  }

  async function loadScore() {
    try {
      score = await api.get<ReviewScore>(scoreBatch === null ? '/api/review/score' : `/api/review/score?batch=${scoreBatch}`);
      scoreError = '';
    } catch (e) {
      scoreError = (e as Error).message;
    }
  }

  $effect(() => {
    if (tab === 'score' && score === null) loadScore();
  });

  function advance() {
    for (let step = 1; step <= items.length; step++) {
      const next = (index + step) % items.length;
      if (items[next].verdict === null) {
        index = next;
        // On a phone the answers sit below the picture: bring the next picture back into view.
        if (picture && picture.getBoundingClientRect().top < 0) picture.scrollIntoView({ block: 'start' });
        return;
      }
    }
  }

  function go(step: number) {
    if (items.length) index = (index + step + items.length) % items.length;
  }

  async function save(verdict: Verdict | null, species: string | null = null) {
    const target = item;
    if (!target || saving) return;
    saving = true;
    error = '';
    try {
      await api.put(`/api/review/${target.id}`, { verdict, species });
      target.verdict = verdict;
      target.answer = verdict === 'species' ? species : null;
      score = null;
      if (verdict === 'species' && species && !quick.includes(species)) {
        quick = quick.length < QUICK ? [...quick, species] : [...quick.slice(0, QUICK - 1), species];
      }
      if (verdict !== null) {
        search = '';
        advance();
      }
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Could not save the answer';
    } finally {
      saving = false;
    }
  }

  function submitSearch(event: SubmitEvent) {
    event.preventDefault();
    const text = search.trim();
    if (!text) return;
    const exact = labels.find((label) => label.toLowerCase() === text.toLowerCase());
    save('species', exact ?? matches[0] ?? text);
  }

  function onKey(event: KeyboardEvent) {
    if (tab !== 'answer' || !item || event.ctrlKey || event.metaKey || event.altKey) return;
    if ((event.target as HTMLElement).closest('input, textarea, select, dialog')) return;
    const digit = Number(event.key);
    if (Number.isInteger(digit) && digit >= 1 && digit <= quick.length) {
      event.preventDefault();
      save('species', quick[digit - 1]);
      return;
    }
    const special = SPECIAL.find((s) => s.key === event.key.toUpperCase());
    if (event.key.toUpperCase() === 'G') {
      event.preventDefault();
      guideOpen = true;
    } else if (special) {
      event.preventDefault();
      save(special.verdict);
    } else if (event.key === 'ArrowLeft') {
      go(-1);
    } else if (event.key === 'ArrowRight') {
      go(1);
    }
  }

  // Warm the next pictures: each is decoded from the original 4K recording on first request.
  $effect(() => {
    let ahead = 0;
    for (let step = 1; step < items.length && ahead < 2; step++) {
      const next = items[(index + step) % items.length];
      if (next.verdict === null) {
        new Image().src = `/api/review/${next.id}/image`;
        ahead++;
      }
    }
  });

  function boxStyle(moment: ReviewItem) {
    const [vx1, vy1, vx2, vy2] = moment.view;
    const [x1, y1, x2, y2] = moment.box;
    const width = vx2 - vx1;
    const height = vy2 - vy1;
    return `left:${((x1 - vx1) / width) * 100}%;top:${((y1 - vy1) / height) * 100}%;`
      + `width:${((x2 - x1) / width) * 100}%;height:${((y2 - y1) / height) * 100}%`;
  }

  function answerText(moment: ReviewItem) {
    if (moment.verdict === 'species') return moment.answer ?? '';
    return SPECIAL.find((s) => s.verdict === moment.verdict)?.label ?? '';
  }

  const percent = (part: number, whole: number) => (whole > 0 ? Math.round((part / whole) * 100) : 0);

  const outcomes = $derived(score ? [
    { key: 'correct', label: 'Named correctly', value: score.birds.correct, color: 'var(--spruce)' },
    { key: 'uncertain', label: 'Left uncertain', value: score.birds.uncertain, color: 'var(--goldfinch)' },
    { key: 'wrong', label: 'Named wrongly', value: score.birds.wrong, color: 'var(--ember)' },
    { key: 'missed', label: 'Missed', value: score.birds.missed, color: '#A9A597' },
  ] : []);
</script>

<svelte:window onkeydown={onKey} />

<div class="page">
  <header class="head">
    <div>
      <h1 class="serif">Check the AI</h1>
      <p class="muted intro" class:has-key={items.length > 0}>Say what's really in each box. Your answers become the yardstick for every change to the AI.
        Its guess stays hidden so it can't sway you.</p>
    </div>
    {#if items.length}
      <div class="segmented" role="group" aria-label="View">
        <button type="button" aria-pressed={tab === 'answer'} onclick={() => { tab = 'answer'; }}>Answer</button>
        <button type="button" aria-pressed={tab === 'score'} onclick={() => { tab = 'score'; }}>Score</button>
      </div>
    {/if}
  </header>

  {#if error}<p class="error" role="alert">{error}</p>{/if}

  {#if !loaded}
    <div class="card placeholder" aria-busy="true"></div>
  {:else if !items.length}
    <section class="card empty">
      <h2 class="serif">Build the answer key</h2>
      <p>The hub picks about 150 moments from your recordings: birds of every species it names, plus animals it
        called something other than a bird and birds that never became a sighting. You say what each one really is.
        It takes about 10–15 minutes, and you can stop any time.</p>
      <p class="muted small">Answers here only measure the AI. They are never used to train it, so the score stays honest.</p>
      <button type="button" class="btn btn-primary" disabled={building} onclick={build}>
        {building ? 'Picking moments…' : 'Pick the moments'}
      </button>
    </section>
  {:else if tab === 'answer' && item}
    <div class="progress" aria-label="{latestAnswered} of {latestItems.length} answered">
      <div class="bar"><div style:width="{percent(latestAnswered, latestItems.length)}%"></div></div>
      <span class="mono small">{batches.length > 1 ? `Key ${latest}: ` : ''}{latestAnswered} / {latestItems.length} answered</span>
    </div>

    {#if allDone}
      <p class="done">All {items.length} moments answered. <button type="button" class="link" onclick={() => { tab = 'score'; }}>See the score</button>
        · <button type="button" class="link" onclick={() => { confirmAdd = true; tab = 'score'; }}>Add more moments</button></p>
    {/if}

    <div class="answer">
      <figure class="card picture" bind:this={picture}>
        <div class="frame">
          {#key item.id}
            <img src="/api/review/{item.id}/image" alt="Moment {index + 1}: the animal to identify, outlined">
          {/key}
          {#if showBox}<div class="box" style={boxStyle(item)}></div>{/if}
        </div>
        <figcaption>
          <span class="mono">#{index + 1}</span>
          <span>{dateTime(item.recorded_at) || item.name}</span>
          <span class="mono muted">{clock(item.ms)}</span>
          <span class="spacer"></span>
          <button type="button" class="link" onclick={() => { showBox = !showBox; }}>{showBox ? 'Hide box' : 'Show box'}</button>
          <a class="link" href="#/recordings/{item.video_id}?t={Math.round(item.ms)}">Open in recording</a>
        </figcaption>
      </figure>

      <section class="card choices" aria-labelledby="question">
        <div class="question">
          <p id="question" class="eyebrow">What is in the box?</p>
          <button type="button" class="btn guide-button" onclick={() => { guideOpen = true; }}>
            <Icon name="bird" size={18} />Field guide<span class="key mono" aria-hidden="true">G</span>
          </button>
        </div>
        {#if item.verdict}
          <p class="current">Your answer: <strong>{answerText(item)}</strong>
            <button type="button" class="link" disabled={saving} onclick={() => save(null)}>Clear</button></p>
        {/if}
        <div class="quick">
          {#each quick as species, i (species)}
            <button type="button" class="choice" disabled={saving}
                    aria-pressed={item.verdict === 'species' && item.answer === species}
                    onclick={() => save('species', species)}>
              <span class="key mono" aria-hidden="true">{i + 1}</span>
              {#if thumbs[species]}<img class="thumb" src={thumbs[species]} alt="" loading="lazy">{:else}<span class="thumb"></span>{/if}
              <span class="name">{species}</span>
            </button>
          {/each}
        </div>
        <form class="search" onsubmit={submitSearch}>
          <label for="species-search" class="small muted">Another species</label>
          <div class="field">
            <Icon name="search" size={18} />
            <input id="species-search" bind:value={search} placeholder="Type a name, then Enter" autocomplete="off" maxlength="120">
          </div>
          {#if matches.length}
            <div class="matches" aria-label="Matching species">
              {#each matches as match (match)}
                <button type="button" class="match" disabled={saving} onclick={() => save('species', match)}>{match}</button>
              {/each}
            </div>
          {/if}
        </form>
        <div class="special">
          {#each SPECIAL as option (option.verdict)}
            <button type="button" class="btn" disabled={saving} aria-pressed={item.verdict === option.verdict}
                    onclick={() => save(option.verdict)}>
              {option.label}<span class="key mono" aria-hidden="true">{option.key}</span>
            </button>
          {/each}
        </div>
        <div class="nav">
          <button type="button" class="btn" onclick={() => go(-1)}><Icon name="back" size={18} />Previous</button>
          <button type="button" class="btn" onclick={() => go(1)}>Skip<Icon name="forward" size={18} /></button>
        </div>
        <p class="muted small hint">Keys: 1–{quick.length} pick a species · N, M, U the answers above · G field guide · ← → move.</p>
      </section>
    </div>
  {:else if tab === 'score'}
    {#if scoreError}<p class="error" role="alert">{scoreError}</p>{/if}
    {#if !score}
      <div class="card placeholder" aria-busy="true"></div>
    {:else}
      <section class="score" aria-labelledby="score-title">
        <h2 id="score-title" class="visually-hidden">Score</h2>
        {#if score.batches.length > 1}
          <div class="segmented batches" role="group" aria-label="Moments scored">
            <button type="button" aria-pressed={scoreBatch === null} onclick={() => { scoreBatch = null; loadScore(); }}>All</button>
            {#each score.batches as batch (batch)}
              <button type="button" aria-pressed={scoreBatch === batch} onclick={() => { scoreBatch = batch; loadScore(); }}>Key {batch}</button>
            {/each}
          </div>
        {/if}
        {#if score.birds.graded < MEANINGFUL}
          <p class="note">Answer at least {MEANINGFUL} birds for a score worth comparing. So far {score.birds.graded}.</p>
        {/if}
        <p class="eyebrow">Birds you identified · {score.birds.graded}</p>
        <div class="tiles">
          {#each outcomes as outcome (outcome.key)}
            <div class="card tile">
              <span class="tile-label"><span class="dot" style:background={outcome.color}></span>{outcome.label}</span>
              <span class="tile-value">{percent(outcome.value, score.birds.graded)}%</span>
              <span class="muted small">{outcome.value} of {score.birds.graded}</span>
            </div>
          {/each}
        </div>
        {#if score.birds.graded}
          <div class="stack" role="img"
               aria-label={outcomes.map((o) => `${o.label} ${percent(o.value, score?.birds.graded ?? 0)}%`).join(', ')}>
            {#each outcomes as outcome (outcome.key)}
              {#if outcome.value}
                <div style:flex-grow={outcome.value} style:background={outcome.color}
                     title="{outcome.label}: {outcome.value} of {score.birds.graded}"></div>
              {/if}
            {/each}
          </div>
        {/if}
        <dl class="facts">
          <div>
            <dt>Best guess right, when the AI showed a bird</dt>
            <dd><span class="mono">{score.birds.guess_correct} of {score.birds.guessed}</span>
              ({percent(score.birds.guess_correct, score.birds.guessed)}%)</dd>
          </div>
          <div>
            <dt>Not birds, left out of the gallery</dt>
            <dd><span class="mono">{score.not_birds.left_out} of {score.not_birds.graded}</span></dd>
          </div>
          <div>
            <dt>Answers that can't grade the AI</dt>
            <dd><span class="mono">{score.excluded}</span> (can't tell, more than one animal)</dd>
          </div>
        </dl>
        <p class="muted small">If the best guess is right far more often than birds are named correctly, the AI is
          being too cautious. "Missed" means it showed no sighting for a bird you saw.</p>

        <div class="card add">
          {#if confirmAdd}
            <p>Picks about 150 new moments from recordings not yet in the key, as the library is now: SD-card clips
              included (one copy of each moment), about a third from uncertain sightings, and only moments where no
              other animal overlaps the box. Your earlier answers stay, and each key keeps its own score.
              The pictures are prepared in the background over the next few minutes.</p>
            <div class="add-actions">
              <button type="button" class="btn btn-primary" disabled={adding} onclick={addMoments}>
                {adding ? 'Picking moments…' : 'Pick 150 more'}
              </button>
              <button type="button" class="btn" onclick={() => { confirmAdd = false; }}>Cancel</button>
            </div>
          {:else}
            <p class="muted small">More answered birds make every score steadier: with {score.birds.graded}, one bird
              moves it by {score.birds.graded ? Math.round(100 / score.birds.graded * 10) / 10 : 0} points.</p>
            <button type="button" class="btn" disabled={latestAnswered < latestItems.length}
                    onclick={() => { confirmAdd = true; }}>Add more moments…</button>
            {#if latestAnswered < latestItems.length}
              <p class="muted small">Finish the moments already picked first.</p>
            {/if}
          {/if}
        </div>

        {#if score.confusions.length}
          <h3 class="serif">Where its best guess differs from you</h3>
          <div class="card table-wrap">
            <table>
              <thead><tr><th>You said</th><th>AI's best guess</th><th class="num">Times</th></tr></thead>
              <tbody>
                {#each score.confusions as row (row.answer + row.guess)}
                  <tr><td>{row.answer}</td><td>{row.guess}</td><td class="num mono">{row.count}</td></tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}

        {#if score.species.length}
          <h3 class="serif">By species</h3>
          <div class="card table-wrap">
            <table>
              <thead>
                <tr><th>Species</th><th class="num">Answered</th><th class="num">Correct</th><th class="num">Uncertain</th>
                  <th class="num">Wrong</th><th class="num">Missed</th></tr>
              </thead>
              <tbody>
                {#each score.species as row (row.species)}
                  <tr><td>{row.species}</td><td class="num mono">{row.graded}</td><td class="num mono">{row.correct}</td>
                    <td class="num mono">{row.uncertain}</td><td class="num mono">{row.wrong}</td><td class="num mono">{row.missed}</td></tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
    {/if}
  {/if}
</div>

{#if guideOpen && item}
  <FieldGuide species={quick} {examples} picture="/api/review/{item.id}/image" box={boxStyle(item)}
              onpick={(name) => { guideOpen = false; save('species', name); }} onclose={() => { guideOpen = false; }} />
{/if}

<style>
  .batches { align-self: flex-start; margin-bottom: 4px; }
  .add { padding: 16px 18px; display: flex; flex-direction: column; gap: 10px; align-items: flex-start; }
  .add p { margin: 0; }
  .add-actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .page { padding: 28px 36px; display: flex; flex-direction: column; gap: 18px; max-width: 1180px; }
  .head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 34px; font-weight: 600; line-height: 1.1; }
  .intro { margin: 6px 0 0; max-width: 62ch; }
  .small { font-size: 13px; margin: 0; }
  .error { margin: 0; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }
  .placeholder { min-height: 320px; }
  .link {
    border: none; background: none; padding: 6px 0; color: var(--spruce); font-weight: 600; font-size: 14px;
    cursor: pointer; text-decoration: underline;
  }
  .link:disabled { opacity: .55; cursor: not-allowed; }

  .empty { padding: 24px; display: flex; flex-direction: column; gap: 12px; align-items: flex-start; max-width: 680px; }
  .empty h2 { margin: 0; font-size: 26px; font-weight: 600; }
  .empty p { margin: 0; }

  .progress { display: flex; align-items: center; gap: 12px; }
  .progress .bar { flex-grow: 1; height: 8px; border-radius: 4px; background: #E3DED1; overflow: hidden; }
  .progress .bar div { height: 100%; background: var(--spruce); }
  .done { margin: 0; padding: 10px 14px; border-radius: 12px; background: var(--spruce-soft); color: var(--spruce); font-weight: 600; }

  .answer { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(300px, 1fr); gap: 20px; align-items: start; }
  .picture { margin: 0; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
  .frame { position: relative; aspect-ratio: 1 / 1; border-radius: 12px; overflow: hidden; background: #D6CDB7; }
  .frame img { width: 100%; height: 100%; display: block; }
  .box {
    position: absolute; border: 2px solid var(--goldfinch-box); border-radius: 4px;
    box-shadow: 0 0 0 1px rgba(27, 36, 32, .55), inset 0 0 0 1px rgba(27, 36, 32, .55); pointer-events: none;
  }
  figcaption { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 14px; }
  .spacer { flex-grow: 1; }

  .choices { padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .eyebrow { margin: 0; }
  .current { margin: 0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .quick { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .choice {
    display: flex; align-items: center; gap: 10px; min-height: 48px; padding: 0 12px; border-radius: 12px; text-align: left;
    border: 1px solid var(--line); background: var(--card); font-size: 15px; font-weight: 500; cursor: pointer;
  }
  .choice:hover:not(:disabled) { background: #F7F4EC; }
  .choice[aria-pressed='true'], .special .btn[aria-pressed='true'] { border-color: var(--spruce); background: var(--spruce-soft); }
  .choice:disabled { cursor: progress; }
  .thumb { width: 36px; height: 36px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: var(--segment); }
  .name { min-width: 0; }
  .question { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .guide-button { min-height: 38px; padding: 0 12px; font-size: 14px; }
  .key {
    display: inline-flex; align-items: center; justify-content: center; min-width: 22px; height: 22px; padding: 0 5px;
    border-radius: 6px; background: var(--segment); color: var(--muted); font-size: 12px; font-weight: 600;
  }
  .search { display: flex; flex-direction: column; gap: 6px; }
  .field {
    display: flex; align-items: center; gap: 8px; height: 46px; padding: 0 12px; border-radius: 12px;
    border: 1px solid var(--control-line); background: #FFFFFF; color: var(--muted);
  }
  .field input { flex-grow: 1; min-width: 0; border: none; outline: none; background: none; font-size: 15px; color: var(--ink); }
  .field:focus-within { outline: 3px solid var(--goldfinch); outline-offset: 2px; }
  .matches { display: flex; flex-wrap: wrap; gap: 6px; }
  .match {
    min-height: 36px; padding: 0 12px; border-radius: 999px; border: 1px solid var(--control-line); background: #FFFFFF;
    font-size: 13px; cursor: pointer;
  }
  .special { display: flex; flex-wrap: wrap; gap: 8px; }
  .special .btn { font-weight: 600; }
  .nav { display: flex; justify-content: space-between; gap: 8px; padding-top: 4px; border-top: 1px solid var(--line-soft); }
  .nav .btn { margin-top: 10px; }
  .hint { margin: 0; }

  .score { display: flex; flex-direction: column; gap: 14px; }
  .note { margin: 0; padding: 10px 14px; border-radius: 12px; background: var(--warn-bg); color: var(--warn-ink); font-size: 14px; }
  .tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
  .tile { padding: 14px 16px; display: flex; flex-direction: column; gap: 4px; }
  .tile-label { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .tile-value { font-size: 34px; font-weight: 600; line-height: 1.1; font-variant-numeric: tabular-nums; }
  .stack { display: flex; gap: 2px; height: 14px; border-radius: 4px; overflow: hidden; }
  .stack div { min-width: 4px; }
  .facts { margin: 0; display: flex; flex-direction: column; gap: 8px; font-size: 14px; max-width: 640px; }
  .facts div { display: flex; justify-content: space-between; gap: 16px; padding-bottom: 8px; border-bottom: 1px solid var(--line-soft); }
  .facts dt { color: var(--muted); }
  .facts dd { margin: 0; text-align: right; }
  h3 { margin: 10px 0 0; font-size: 22px; font-weight: 600; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--line-soft); }
  th { font-size: 12px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
  tbody tr:last-child td { border-bottom: none; }
  .num { text-align: right; }

  @media (max-width: 1000px) {
    .answer { grid-template-columns: minmax(0, 1fr); }
    .picture { max-width: 620px; }
  }
  @media (max-width: 860px) {
    .page { padding: 16px; }
    h1 { font-size: 28px; }
    .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .hint { display: none; }
    .key { display: none; }
    .intro.has-key { display: none; }
    /* Picture and the first answers on one screen. */
    .frame { width: min(100%, 46dvh); margin: 0 auto; }
    .picture { scroll-margin-top: 12px; }
  }
</style>
