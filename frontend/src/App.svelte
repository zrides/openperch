<script lang="ts">
  import Nav from './lib/Nav.svelte';
  import { checkSession, hub } from './lib/hub.svelte';
  import Live from './pages/Live.svelte';
  import Login from './pages/Login.svelte';
  import Recordings from './pages/Recordings.svelte';
  import Birds from './pages/Birds.svelte';
  import Home from './pages/Home.svelte';
  import ComingSoon from './pages/ComingSoon.svelte';

  const pages: Record<string, { title: string; summary: string }> = {
    camera: { title: 'Camera', summary: 'Camera status, motion and recording settings, activity zone and notifications.' },
  };
  const BUILT = ['home', 'live', 'recordings', 'birds'];

  // #/<page>[/<id>][?t=<ms>]
  function parse() {
    const [path, search = ''] = location.hash.replace(/^#\/?/, '').split('?');
    const [page, id = null] = path.split('/');
    const known = BUILT.includes(page) || page in pages;
    return {
      route: known ? page : 'home',
      id: known && id ? decodeURIComponent(id) : null,
      startMs: Number(new URLSearchParams(search).get('t')) || 0,
    };
  }

  let current = $state(parse());
  const route = $derived(current.route);
  $effect(() => {
    const update = () => { current = parse(); };
    addEventListener('hashchange', update);
    return () => removeEventListener('hashchange', update);
  });
  checkSession();
</script>

{#if hub.session === null}
  <div class="loading" aria-busy="true"></div>
{:else if !hub.session.authenticated}
  <Login />
{:else}
<div class="shell">
  <Nav {route} />
  <main>
    {#if route === 'home'}
      <Home />
    {:else if route === 'live'}
      <Live />
    {:else if route === 'recordings'}
      <Recordings id={current.id} startMs={current.startMs} />
    {:else if route === 'birds'}
      <Birds species={current.id} />
    {:else}
      {#key route}<ComingSoon {...pages[route]} />{/key}
    {/if}
  </main>
</div>
{/if}

<style>
  .loading { min-height: 100vh; }
  .shell { display: flex; min-height: 100vh; min-height: 100dvh; }
  main { flex-grow: 1; min-width: 0; }
  @media (max-width: 860px) {
    main { padding-bottom: calc(84px + env(safe-area-inset-bottom)); }
  }
</style>
