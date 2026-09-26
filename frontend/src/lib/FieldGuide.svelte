<script lang="ts">
  import { onMount } from 'svelte';
  import { fieldGuide, type GuideEntry } from './fieldGuide';

  let { species, examples, picture, box, onpick, onclose }: {
    species: string[];
    /** Answer-key moments you already identified, by species. */
    examples: Record<string, string[]>;
    /** The moment being answered, to compare against. */
    picture: string;
    box: string;
    onpick: (species: string) => void;
    onclose: () => void;
  } = $props();

  let dialog: HTMLDialogElement | undefined = $state();
  let entries: Record<string, GuideEntry | null> = $state({});
  let showBox = $state(true);

  onMount(() => {
    dialog?.showModal();
    for (const name of species) fieldGuide(name).then((entry) => { entries[name] = entry; });
  });

  function close() {
    dialog?.close();
    onclose();
  }

  function pick(name: string) {
    dialog?.close();
    onpick(name);
  }
</script>

<dialog bind:this={dialog} aria-labelledby="guide-title" oncancel={(event) => { event.preventDefault(); close(); }}>
  <div class="layout">
    <aside class="moment">
      <div class="frame">
        <img src={picture} alt="The moment you are identifying">
        {#if showBox}<div class="box" style={box}></div>{/if}
      </div>
      <button type="button" class="link" onclick={() => { showBox = !showBox; }}>{showBox ? 'Hide box' : 'Show box'}</button>
    </aside>

    <div class="guide">
      <header>
        <div>
          <h2 id="guide-title" class="serif">Field guide</h2>
          <p class="muted small">Reference photos from Wikipedia, and moments you've already identified.</p>
        </div>
        <button type="button" class="close" aria-label="Close the field guide" onclick={close}>×</button>
      </header>
      <div class="cards">
        {#each species as name (name)}
          {@const entry = entries[name]}
          {@const yours = examples[name] ?? []}
          <article class="card entry">
            <h3 class="serif">{name}</h3>
            <div class="photos">
              {#if !(name in entries)}
                <div class="photo loading" aria-busy="true"></div>
              {:else if entry === null}
                <p class="muted small">No reference photo found.</p>
              {:else if entry}
                {#each entry.photos as photo (photo.src)}
                  <figure class="photo">
                    <img src={photo.src} alt="{name}{photo.label ? `, ${photo.label.toLowerCase()}` : ''}" loading="lazy">
                    {#if photo.label}<figcaption>{photo.label}</figcaption>{/if}
                  </figure>
                {/each}
              {/if}
            </div>
            {#if yours.length}
              <p class="eyebrow">From your feeder</p>
              <div class="yours">
                {#each yours.slice(-4) as id (id)}
                  <img src="/api/review/{id}/image" alt="{name} at your feeder" loading="lazy">
                {/each}
              </div>
            {/if}
            <div class="actions">
              <button type="button" class="btn btn-primary" onclick={() => pick(name)}>It's this one</button>
              {#if entry?.page}<a href={entry.page} target="_blank" rel="noopener">Wikipedia</a>{/if}
            </div>
          </article>
        {/each}
      </div>
      <p class="muted small">Not listed? Close this and type the name under "Another species".</p>
    </div>
  </div>
</dialog>

<style>
  dialog {
    width: min(1180px, calc(100vw - 24px)); height: min(900px, calc(100dvh - 24px)); max-height: none; padding: 0;
    border: none; border-radius: 20px; background: var(--paper); color: var(--ink);
    box-shadow: 0 20px 60px rgba(27, 36, 32, 0.25);
  }
  dialog::backdrop { background: rgba(27, 36, 32, 0.45); }
  .layout { display: grid; grid-template-columns: 300px minmax(0, 1fr); height: 100%; }
  .moment {
    padding: 20px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start;
    background: var(--paper-deep); border-right: 1px solid var(--line);
  }
  .frame { position: relative; width: 100%; aspect-ratio: 1 / 1; border-radius: 12px; overflow: hidden; background: #D6CDB7; }
  .frame img { width: 100%; height: 100%; display: block; }
  .box {
    position: absolute; border: 2px solid var(--goldfinch-box); border-radius: 4px;
    box-shadow: 0 0 0 1px rgba(27, 36, 32, .55), inset 0 0 0 1px rgba(27, 36, 32, .55); pointer-events: none;
  }
  .guide { overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 14px; }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  h2 { margin: 0; font-size: 26px; font-weight: 600; }
  .small { margin: 4px 0 0; font-size: 13px; }
  .close {
    flex-shrink: 0; width: 44px; height: 44px; border-radius: 50%; border: 1px solid var(--line); background: var(--card);
    font-size: 24px; line-height: 1; cursor: pointer; color: var(--ink);
  }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
  .entry { padding: 14px; display: flex; flex-direction: column; gap: 10px; }
  h3 { margin: 0; font-size: 19px; font-weight: 600; }
  .photos { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
  .photo { margin: 0; position: relative; }
  .photo img, .photo.loading { width: 100%; aspect-ratio: 1 / 1; object-fit: cover; border-radius: 8px; display: block; background: #D6CDB7; }
  .photo figcaption {
    position: absolute; left: 4px; bottom: 4px; padding: 1px 6px; border-radius: 6px;
    background: rgba(27, 36, 32, .72); color: #FFFFFF; font-size: 11px; font-weight: 600;
  }
  .eyebrow { margin: 0; }
  .yours { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; }
  .yours img { width: 100%; aspect-ratio: 1 / 1; object-fit: cover; border-radius: 8px; display: block; background: #D6CDB7; }
  .actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: auto; }
  .actions a { font-weight: 600; font-size: 14px; }
  .link {
    border: none; background: none; padding: 6px 0; color: var(--spruce); font-weight: 600; font-size: 14px;
    cursor: pointer; text-decoration: underline;
  }

  @media (max-width: 860px) {
    dialog { width: 100vw; height: 100dvh; max-width: none; border-radius: 0; }
    .layout { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
    /* The moment stays in view as a small picture above the cards. */
    .moment { flex-direction: row; align-items: center; padding: 10px 16px; border-right: none; border-bottom: 1px solid var(--line); }
    .frame { width: 110px; flex-shrink: 0; }
    .guide { padding: 16px; }
    .cards { grid-template-columns: minmax(0, 1fr); }
  }
</style>
