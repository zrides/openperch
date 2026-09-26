<script lang="ts">
  import Nav from './lib/Nav.svelte';
  import { startHub } from './lib/hub.svelte';
  import Live from './pages/Live.svelte';
  import ComingSoon from './pages/ComingSoon.svelte';

  const pages: Record<string, { title: string; summary: string }> = {
    home: { title: 'Home', summary: "Today's visitors, top species and busiest hours from your library." },
    recordings: { title: 'Recordings', summary: 'Every clip with its AI overlays, sightings and top matches, ready to play and correct.' },
    birds: { title: 'Birds', summary: 'Species gallery with the best photos of each sighting and one-click corrections.' },
    camera: { title: 'Camera', summary: 'Camera status, motion and recording settings, activity zone and notifications.' },
  };

  function currentRoute() {
    const route = location.hash.replace(/^#\/?/, '').split('/')[0];
    return route === 'live' || route in pages ? route : 'live';
  }

  let route = $state(currentRoute());
  $effect(() => {
    const update = () => { route = currentRoute(); };
    addEventListener('hashchange', update);
    return () => removeEventListener('hashchange', update);
  });
  startHub();
</script>

<div class="shell">
  <Nav {route} />
  <main>
    {#if route === 'live'}
      <Live />
    {:else}
      {#key route}<ComingSoon {...pages[route]} />{/key}
    {/if}
  </main>
</div>

<style>
  .shell { display: flex; min-height: 100vh; min-height: 100dvh; }
  main { flex-grow: 1; min-width: 0; }
  @media (max-width: 860px) {
    main { padding-bottom: calc(84px + env(safe-area-inset-bottom)); }
  }
</style>
