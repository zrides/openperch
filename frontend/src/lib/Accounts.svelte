<script lang="ts">
  import { api, type HubAccount, type Role } from './api';
  import { hub, signOut } from './hub.svelte';

  // Who is signed in, the accounts that can sign in (admins manage them), and one's own password.
  const ROLE_LABELS: Record<Role, string> = { admin: 'Admin', member: 'Member' };
  const isAdmin = $derived(hub.session?.role === 'admin');
  const me = $derived(hub.session?.user ?? '');

  let accounts = $state<HubAccount[]>([]);
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);

  let name = $state('');
  let password = $state('');
  let role = $state<Role>('member');
  let resetting = $state<string | null>(null);
  let resetPassword = $state('');
  let removing = $state<string | null>(null);
  let current = $state('');
  let fresh = $state('');

  $effect(() => {
    if (isAdmin) {
      api.get<HubAccount[]>('/api/users').then((rows) => { accounts = rows; }).catch((e) => { error = (e as Error).message; });
    }
  });

  // Runs one change; the reply is the updated list for account changes.
  async function change(action: () => Promise<unknown>, done: string) {
    busy = true;
    error = '';
    notice = '';
    try {
      const rows = await action();
      if (Array.isArray(rows)) accounts = rows as HubAccount[];
      notice = done;
      return true;
    } catch (e) {
      error = (e as Error).message;
      return false;
    } finally {
      busy = false;
    }
  }

  async function add(event: SubmitEvent) {
    event.preventDefault();
    const added = name.trim();
    if (await change(() => api.post('/api/users', { name: added, password, role }),
                     `${added} can now sign in as ${ROLE_LABELS[role].toLowerCase()}.`)) {
      name = '';
      password = '';
      role = 'member';
    }
  }

  function setRole(account: HubAccount, next: Role) {
    change(() => api.put(`/api/users/${encodeURIComponent(account.name)}`, { role: next }),
           `${account.name} is now ${ROLE_LABELS[next].toLowerCase()}.`);
  }

  async function savePassword(event: SubmitEvent, account: string) {
    event.preventDefault();
    if (await change(() => api.put(`/api/users/${encodeURIComponent(account)}`, { password: resetPassword }),
                     `New password set for ${account}. They were signed out everywhere.`)) {
      resetting = null;
      resetPassword = '';
    }
  }

  async function remove(account: string) {
    if (await change(() => api.del(`/api/users/${encodeURIComponent(account)}`), `${account} can no longer sign in.`)) {
      removing = null;
    }
  }

  async function changeOwn(event: SubmitEvent) {
    event.preventDefault();
    if (await change(() => api.put('/api/account/password', { current, new: fresh }),
                     'Password changed. Your other devices were signed out.')) {
      current = '';
      fresh = '';
    }
  }
</script>

<div class="line">
  <span class="label">Signed in as {me}{hub.session?.role ? ` · ${ROLE_LABELS[hub.session.role]}` : ''}</span>
  <button type="button" class="btn" onclick={signOut}>Sign out</button>
</div>

{#if error}<p class="error" role="alert">{error}</p>{/if}
{#if notice}<p class="notice" aria-live="polite">{notice}</p>{/if}

{#if isAdmin}
  <h3>People who can sign in</h3>
  <ul class="accounts">
    {#each accounts as account (account.name)}
      <li>
        <div class="row">
          <span class="who">
            <span class="name">{account.name}{account.name === me ? ' (you)' : ''}</span>
            <span class="muted small">{account.owner ? 'Owner · password set in the .env file' : ''}</span>
          </span>
          {#if account.owner || account.name === me}
            <span class="role">{ROLE_LABELS[account.role]}</span>
          {:else}
            <select aria-label="Role for {account.name}" value={account.role} disabled={busy}
                    onchange={(event) => setRole(account, event.currentTarget.value as Role)}>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
          {/if}
        </div>
        {#if !account.owner && account.name !== me}
          {#if resetting === account.name}
            <form class="inline" onsubmit={(event) => savePassword(event, account.name)}>
              <input type="password" bind:value={resetPassword} minlength="8" required autocomplete="new-password"
                     aria-label="New password for {account.name}" placeholder="New password (8+ characters)">
              <button class="btn btn-primary" type="submit" disabled={busy}>Save</button>
              <button class="btn" type="button" onclick={() => { resetting = null; resetPassword = ''; }}>Cancel</button>
            </form>
          {:else if removing === account.name}
            <div class="inline">
              <span class="small">Remove {account.name}? They are signed out at once.</span>
              <button class="btn danger" type="button" disabled={busy} onclick={() => remove(account.name)}>Remove</button>
              <button class="btn" type="button" onclick={() => { removing = null; }}>Cancel</button>
            </div>
          {:else}
            <div class="inline">
              <button class="btn small-btn" type="button" onclick={() => { resetting = account.name; removing = null; }}>New password…</button>
              <button class="btn small-btn" type="button" onclick={() => { removing = account.name; resetting = null; }}>Remove…</button>
            </div>
          {/if}
        {/if}
      </li>
    {/each}
  </ul>

  <form class="add" onsubmit={add}>
    <h3>Add someone</h3>
    <label>Name
      <input bind:value={name} required maxlength="40" pattern={'[A-Za-z0-9._@\\-]+'} autocomplete="off"
             autocapitalize="none" spellcheck="false" title="Letters, digits, dots, dashes, underscores or @">
    </label>
    <label>Password
      <input type="password" bind:value={password} minlength="8" required autocomplete="new-password">
    </label>
    <label>Role
      <select bind:value={role}>
        <option value="member">Member: uses the hub</option>
        <option value="admin">Admin: also manages who can sign in</option>
      </select>
    </label>
    <button class="btn btn-primary" type="submit" disabled={busy}>Add</button>
  </form>
{/if}

{#if hub.session?.owner}
  <p class="muted small">Your password is set in the hub's <span class="mono">.env</span> file.</p>
{:else}
  <form class="add" onsubmit={changeOwn}>
    <h3>Change your password</h3>
    <label>Current password
      <input type="password" bind:value={current} required autocomplete="current-password">
    </label>
    <label>New password
      <input type="password" bind:value={fresh} minlength="8" required autocomplete="new-password">
    </label>
    <button class="btn btn-primary" type="submit" disabled={busy}>Change password</button>
  </form>
{/if}

<style>
  .line {
    display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 48px;
    border-top: 1px solid var(--line-soft); font-size: 15px; padding: 6px 0;
  }
  h3 { margin: 14px 0 4px; font-size: 16px; font-weight: 600; }
  .accounts { list-style: none; margin: 0; padding: 0; }
  .accounts li { border-top: 1px solid var(--line-soft); padding: 8px 0; display: flex; flex-direction: column; gap: 6px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 40px; }
  .who { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .name { font-weight: 600; overflow-wrap: anywhere; }
  .role { font-size: 14px; color: var(--muted); }
  .inline { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .inline input { flex: 1 1 180px; }
  .small { font-size: 13px; margin: 0; }
  .small-btn { min-height: 36px; padding: 0 12px; font-size: 13px; }
  .danger { background: #7A2A18; border-color: #7A2A18; color: #FFFFFF; }
  .add { display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--line-soft); padding-top: 4px; margin-top: 8px; }
  .add .btn { align-self: flex-start; }
  label { display: flex; flex-direction: column; gap: 6px; font-size: 14px; font-weight: 600; color: var(--muted); }
  input, select {
    min-height: 44px; padding: 0 12px; border-radius: 10px; border: 1px solid var(--control-line); background: #FFFFFF;
    font-size: 16px; font-weight: 400; color: var(--ink); min-width: 0;
  }
  .error { margin: 6px 0; padding: 10px 14px; border-radius: 12px; background: #F7E1DA; color: #7A2A18; font-size: 14px; }
  .notice { margin: 6px 0; padding: 10px 14px; border-radius: 12px; background: var(--spruce-soft); color: var(--spruce); font-size: 14px; }
</style>
