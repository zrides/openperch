<script lang="ts">
  import Icon from '../lib/Icon.svelte';
  import { ApiError } from '../lib/api';
  import { signIn } from '../lib/hub.svelte';

  let username = $state('');
  let password = $state('');
  let remember = $state(true);
  let busy = $state(false);
  let error = $state('');

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    error = '';
    try {
      await signIn(username.trim(), password, remember);
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Could not reach the hub. Is it running?';
      password = '';
    } finally {
      busy = false;
    }
  }
</script>

<main class="page">
  <form class="card" onsubmit={submit} aria-labelledby="sign-in-title">
    <div class="brand">
      <span class="mark"><Icon name="bird" size={26} /></span>
      <span class="serif name">Kiwibit Hub</span>
    </div>
    <h1 id="sign-in-title" class="serif">Sign in</h1>
    <label>
      Username
      <input bind:value={username} name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required>
    </label>
    <label>
      Password
      <input bind:value={password} name="password" type="password" autocomplete="current-password" required>
    </label>
    <label class="remember">
      <input type="checkbox" bind:checked={remember}>
      Keep me signed in on this device
    </label>
    {#if error}<p class="error" role="alert">{error}</p>{/if}
    <button class="btn btn-primary" type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
    <p class="muted hint">Use the owner account from the hub's <span class="mono">.env</span> file, or one an admin added for you.</p>
  </form>
</main>

<style>
  .page {
    min-height: 100vh; min-height: 100dvh; display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  form { width: min(400px, 100%); padding: 32px 28px; display: flex; flex-direction: column; gap: 16px; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .mark {
    width: 44px; height: 44px; border-radius: 12px; background: var(--spruce); color: var(--paper);
    display: flex; align-items: center; justify-content: center;
  }
  .name { font-size: 22px; font-weight: 600; }
  h1 { margin: 8px 0 0; font-size: 30px; font-weight: 600; }
  label { display: flex; flex-direction: column; gap: 6px; font-size: 14px; font-weight: 600; color: var(--muted); }
  input:not([type='checkbox']) {
    height: 48px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--control-line); background: #FFFFFF;
    font-size: 16px; font-weight: 400; color: var(--ink);
  }
  .remember { flex-direction: row; align-items: center; gap: 10px; font-weight: 500; color: var(--ink); min-height: 44px; }
  .remember input { width: 20px; height: 20px; margin: 0; accent-color: var(--spruce); }
  .error { margin: 0; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }
  button { min-height: 48px; }
  .hint { margin: 0; font-size: 13px; }
</style>
