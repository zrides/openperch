<script lang="ts">
  import Icon, { type IconName } from './Icon.svelte';
  import { api } from './api';
  import { hub, signOut } from './hub.svelte';

  let { route }: { route: string } = $props();

  const items: { route: string; label: string; short: string; icon: IconName }[] = [
    { route: 'home', label: 'Home', short: 'Home', icon: 'home' },
    { route: 'live', label: 'Live', short: 'Live', icon: 'live' },
    { route: 'recordings', label: 'Recordings', short: 'Clips', icon: 'recordings' },
    { route: 'birds', label: 'Birds', short: 'Birds', icon: 'bird' },
    { route: 'camera', label: 'Camera', short: 'Camera', icon: 'camera' },
    { route: 'review', label: 'Check the AI', short: 'Check', icon: 'review' },
  ];

  const percent = $derived(hub.job.total > 0 ? Math.round((hub.job.value / hub.job.total) * 100) : null);
</script>

<nav class="sidebar" aria-label="Main">
  <div class="brand">
    <span class="mark"><Icon name="bird" size={22} /></span>
    <span class="names">
      <span class="serif title">OpenPerch</span>
      <span class="muted small">Self-hosted</span>
    </span>
  </div>
  <div class="links">
    {#each items as item (item.route)}
      <a href="#/{item.route}" aria-current={route === item.route ? 'page' : undefined}>
        <Icon name={item.icon} />{item.label}
      </a>
    {/each}
  </div>
  <div class="spacer"></div>
  {#if hub.job.running}
    <section class="card job" aria-live="polite">
      <div class="eyebrow row"><span>Working</span>{#if percent !== null}<span class="mono">{percent}%</span>{/if}</div>
      <p class="job-message">{hub.job.message || 'Working…'}</p>
      <div class="bar"><div style:width={percent === null ? '30%' : `${percent}%`}></div></div>
      <button class="btn stop" type="button" onclick={() => api.post('/api/jobs/cancel')}>Stop</button>
    </section>
  {/if}
  {#if hub.session?.auth_required}
    <div class="account">
      <span class="muted small">Signed in as {hub.session.user}</span>
      <button type="button" class="link" onclick={signOut}>Sign out</button>
    </div>
  {/if}
</nav>

<nav class="tabs" aria-label="Main">
  {#each items as item (item.route)}
    <a href="#/{item.route}" aria-current={route === item.route ? 'page' : undefined}>
      <Icon name={item.icon} size={22} />{item.short}
    </a>
  {/each}
</nav>

<style>
  .sidebar {
    width: 232px; flex-shrink: 0; position: sticky; top: 0; height: 100vh; height: 100dvh;
    padding: 28px 16px 20px; display: flex; flex-direction: column; gap: 28px;
    background: var(--paper-deep); border-right: 1px solid var(--line);
  }
  .brand { display: flex; align-items: center; gap: 10px; padding: 0 8px; }
  .mark {
    width: 38px; height: 38px; border-radius: 11px; background: var(--spruce); color: var(--paper);
    display: flex; align-items: center; justify-content: center;
  }
  .names { display: flex; flex-direction: column; gap: 2px; }
  .title { font-size: 20px; font-weight: 600; line-height: 1.1; }
  .small { font-size: 12px; }
  .links { display: flex; flex-direction: column; gap: 4px; }
  .links a {
    display: flex; align-items: center; gap: 12px; min-height: 44px; padding: 0 12px; border-radius: 10px;
    text-decoration: none; font-size: 15px; font-weight: 500; color: var(--ink);
  }
  .links a:hover { background: #E1DBCC; }
  .links a[aria-current='page'] { background: var(--spruce); color: #FFFFFF; }
  .spacer { flex-grow: 1; }
  .job { padding: 14px; display: flex; flex-direction: column; gap: 8px; }
  .row { display: flex; justify-content: space-between; }
  .job-message { margin: 0; font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
  .bar { height: 6px; border-radius: 3px; background: #E3DED1; overflow: hidden; }
  .bar div { height: 100%; background: var(--spruce); }
  .stop { min-height: 36px; font-size: 13px; }
  .account { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 8px; }
  .link {
    border: none; background: none; padding: 8px 0; min-height: 36px; color: var(--spruce); font-weight: 600;
    font-size: 13px; cursor: pointer; text-decoration: underline;
  }

  .tabs { display: none; }

  @media (max-width: 860px) {
    .sidebar { display: none; }
    .tabs {
      display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); position: fixed; inset: auto 0 0 0; z-index: 10;
      padding: 8px 8px calc(8px + env(safe-area-inset-bottom)); background: var(--card); border-top: 1px solid var(--line);
    }
    .tabs a {
      display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; min-height: 52px;
      border-radius: 12px; text-decoration: none; font-size: 12px; font-weight: 600; color: var(--muted);
    }
    .tabs a[aria-current='page'] { color: var(--spruce); background: var(--spruce-soft); }
  }
</style>
