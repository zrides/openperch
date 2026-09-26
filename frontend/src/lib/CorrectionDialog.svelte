<script lang="ts" module>
  import { api } from './api';

  let labelsPromise: Promise<string[]> | null = null;
  function loadLabels() {
    labelsPromise ??= api.get<string[]>('/api/labels').catch((error) => {
      labelsPromise = null;
      throw error;
    });
    return labelsPromise;
  }
</script>

<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { ApiError, UNCERTAIN, type Sighting } from './api';
  import { clock } from './format';
  import { libraryChanged } from './hub.svelte';

  let { sighting, context = '', onclose }: {
    sighting: Sighting;
    context?: string;
    onclose: (changed: boolean) => void;
  } = $props();

  const current = $derived(sighting.corrected ?? sighting.ai_species);
  const suggestion = $derived(sighting.evidence.suggestion || sighting.ai_species);
  let choice = $state(untrack(() => sighting.corrected ?? sighting.ai_species));
  let other = $state('');
  let labels: string[] = $state([]);
  let busy = $state(false);
  let error = $state('');
  let dialog: HTMLDialogElement | undefined = $state();

  const options = $derived([...new Set([suggestion, current, UNCERTAIN].filter(Boolean))]);
  const matches = $derived(other.trim().length >= 2
    ? labels.filter((label) => label.toLowerCase().includes(other.trim().toLowerCase())).slice(0, 6)
    : []);

  onMount(() => {
    dialog?.showModal();
    loadLabels().then((all) => { labels = all; }).catch(() => {});
  });

  async function save(label: string | null) {
    busy = true;
    error = '';
    try {
      await api.post(`/api/sightings/${sighting.id}/correction`, { label });
      libraryChanged();
      dialog?.close();
      onclose(true);
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Could not save the correction';
    } finally {
      busy = false;
    }
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const label = other.trim() || choice;
    if (label) save(label);
  }

  function cancel() {
    dialog?.close();
    onclose(false);
  }
</script>

<dialog bind:this={dialog} aria-labelledby="correct-title" oncancel={(event) => { event.preventDefault(); cancel(); }}>
  <form onsubmit={submit}>
    <header>
      {#if sighting.photo}<img src="/api/photos/{sighting.photo}" alt="" class="thumb">{/if}
      <div>
        <h2 id="correct-title" class="serif">Correct this sighting</h2>
        <p class="muted small">{context}{context ? ' · ' : ''}<span class="mono">{clock(sighting.start_ms)}–{clock(sighting.end_ms)}</span> · all {sighting.photos} photo{sighting.photos === 1 ? '' : 's'}</p>
      </div>
    </header>

    <fieldset>
      <legend class="eyebrow">Species</legend>
      {#each options as option (option)}
        <label class="option" class:selected={choice === option && !other.trim()}>
          <input type="radio" name="species" value={option} bind:group={choice} onchange={() => { other = ''; }}>
          <span>{option}</span>
          {#if option === suggestion && !sighting.corrected}
            <span class="tag">AI · {sighting.evidence.support} of {sighting.evidence.samples} views</span>
          {:else if option === sighting.corrected}
            <span class="tag">Your correction</span>
          {/if}
        </label>
      {/each}
    </fieldset>

    <label class="other">
      Or type another species
      <input bind:value={other} placeholder="Species name" autocomplete="off" maxlength="120">
    </label>
    {#if matches.length}
      <div class="matches" aria-label="Matching species">
        {#each matches as match (match)}
          <button type="button" class="match" onclick={() => { other = match; }}>{match}</button>
        {/each}
      </div>
    {/if}

    <p class="muted small">This relabels this sighting's photos and overlays only. The AI model is not retrained.</p>
    {#if error}<p class="error" role="alert">{error}</p>{/if}

    <div class="actions">
      {#if sighting.corrected}
        <button type="button" class="btn" disabled={busy} onclick={() => save(null)}>Use AI identification</button>
      {/if}
      <span class="spacer"></span>
      <button type="button" class="btn" onclick={cancel}>Cancel</button>
      <button type="submit" class="btn btn-primary" disabled={busy}>{busy ? 'Saving…' : 'Save correction'}</button>
    </div>
  </form>
</dialog>

<style>
  dialog {
    width: min(480px, calc(100vw - 24px)); max-height: calc(100dvh - 24px); padding: 0; border: none; border-radius: 20px;
    background: var(--card); color: var(--ink); box-shadow: 0 20px 60px rgba(27, 36, 32, 0.25);
  }
  dialog::backdrop { background: rgba(27, 36, 32, 0.45); }
  form { padding: 22px; display: flex; flex-direction: column; gap: 14px; }
  header { display: flex; gap: 14px; align-items: center; }
  .thumb { width: 72px; height: 72px; object-fit: cover; border-radius: 12px; flex-shrink: 0; background: #D6CDB7; }
  h2 { margin: 0; font-size: 22px; font-weight: 600; }
  .small { margin: 4px 0 0; font-size: 13px; }
  fieldset { margin: 0; padding: 0; border: none; display: flex; flex-direction: column; gap: 8px; }
  legend { padding: 0 0 8px; }
  .option {
    display: flex; align-items: center; gap: 12px; min-height: 48px; padding: 0 14px; border-radius: 12px;
    border: 1px solid var(--line); cursor: pointer; font-size: 15px;
  }
  .option.selected { border-color: var(--spruce); background: var(--spruce-soft); }
  .option input { width: 20px; height: 20px; margin: 0; accent-color: var(--spruce); }
  .tag { margin-left: auto; font-size: 12px; font-weight: 700; color: var(--spruce); }
  .other { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; color: var(--muted); }
  .other input {
    height: 48px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--control-line); background: #FFFFFF;
    font-size: 15px; font-weight: 400; color: var(--ink);
  }
  .matches { display: flex; flex-wrap: wrap; gap: 6px; margin-top: -6px; }
  .match {
    min-height: 36px; padding: 0 12px; border-radius: 999px; border: 1px solid var(--control-line); background: #FFFFFF;
    font-size: 13px; cursor: pointer;
  }
  .error { margin: 0; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .spacer { flex-grow: 1; }
</style>
