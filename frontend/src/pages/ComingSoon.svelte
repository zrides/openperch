<script lang="ts">
  let { title, summary }: { title: string; summary: string } = $props();
</script>

<section class="page">
  <h1 class="serif">{title}</h1>
  <div class="card note">
    <p class="eyebrow">Coming next</p>
    <p>{summary}</p>
    <a class="btn btn-primary" href="#/live">Open live view</a>
  </div>
</section>

<style>
  .page { padding: 36px 40px; display: flex; flex-direction: column; gap: 20px; max-width: 760px; }
  h1 { margin: 0; font-size: 36px; font-weight: 600; }
  .note { padding: 22px 24px; display: flex; flex-direction: column; gap: 10px; align-items: flex-start; }
  .note p { margin: 0; }
  @media (max-width: 860px) { .page { padding: 24px 18px; } }
</style>
